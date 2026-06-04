"""Outbound-only AgentMail mailbox platform adapter for Hermes.

This experimental adapter keeps Hermes behind NAT/no public ingress.  It opens an
outbound WebSocket to AgentMail, subscribes to inbound mail events, and injects a
minimal notification into one stable Hermes gateway session.  The notification
intentionally omits the email body; the agent should inspect the mailbox through
AgentMail MCP tools when it decides that is appropriate.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import re
import shutil
import time
import urllib.parse
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    import websockets
    WEBSOCKETS_AVAILABLE = True
except ImportError:  # pragma: no cover - dependency probe
    websockets = None  # type: ignore[assignment]
    WEBSOCKETS_AVAILABLE = False

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import BasePlatformAdapter, MessageEvent, MessageType, SendResult, build_session_key

logger = logging.getLogger(__name__)

PLATFORM_NAME = "agentmail_mailbox"
DEFAULT_WS_URL = "wss://ws.agentmail.to/"
DEFAULT_EVENT_TYPES = [
    "message.received",
    "message.received.spam",
    "message.received.unauthenticated",
    "message.received.blocked",
]
RECONNECT_BACKOFF = [2, 5, 10, 30, 60]
DEDUP_MAX_SIZE = 2000
DEFAULT_NOTIFICATION_MIN_INTERVAL_SECONDS = 10.0
DEFAULT_NOTIFICATION_BATCH_WINDOW_SECONDS = 1.0
SAFE_BUSY_INPUT_MODES = {"queue", "steer"}


class AgentMailMailboxAdapter(BasePlatformAdapter):
    """AgentMail WebSocket receiver that maps all mail to one Hermes session."""

    MAX_MESSAGE_LENGTH = 4096

    def __init__(self, config: PlatformConfig):
        super().__init__(config, Platform(PLATFORM_NAME))
        extra = config.extra or {}
        self._api_key = str(extra.get("api_key") or os.getenv("AGENTMAIL_API_KEY", "")).strip()
        self._ws_url = str(extra.get("ws_url") or os.getenv("AGENTMAIL_WS_URL", DEFAULT_WS_URL)).strip()
        self._inbox_ids = _csv_or_list(
            extra.get("inbox_ids")
            or extra.get("inboxes")
            or os.getenv("AGENTMAIL_INBOX", "")
            or os.getenv("AGENTMAIL_INBOX_IDS", "")
        )
        self._event_types = _csv_or_list(extra.get("event_types")) or list(DEFAULT_EVENT_TYPES)
        self._session_chat_id = str(
            extra.get("session_chat_id")
            or _default_session_chat_id(self._inbox_ids)
        )
        self._session_name = str(extra.get("session_name") or "AgentMail mailbox")
        self._auto_skill = extra.get("auto_skill") or "agentmail-mailbox-operator"
        self._channel_prompt = str(extra.get("channel_prompt") or _default_channel_prompt()).strip()
        self._noop_send = str(extra.get("noop_send", "true")).lower() not in {"0", "false", "no", "off"}
        self._notification_min_interval_seconds = _float_config(
            extra.get("notification_min_interval_seconds")
            or os.getenv("AGENTMAIL_MAILBOX_NOTIFICATION_MIN_INTERVAL_SECONDS"),
            DEFAULT_NOTIFICATION_MIN_INTERVAL_SECONDS,
        )
        self._notification_batch_window_seconds = _float_config(
            extra.get("notification_batch_window_seconds")
            or os.getenv("AGENTMAIL_MAILBOX_NOTIFICATION_BATCH_WINDOW_SECONDS"),
            DEFAULT_NOTIFICATION_BATCH_WINDOW_SECONDS,
        )
        self._wait_for_idle_when_unsafe_busy = _bool_config(extra.get("wait_for_idle_when_unsafe_busy"), True)
        self._require_safe_busy_input_mode = _bool_config(extra.get("require_safe_busy_input_mode"), False)
        self._last_dispatch_at = 0.0
        self._dispatch_lock = asyncio.Lock()
        self._pending_lock = asyncio.Lock()
        self._pending_notifications: List[Dict[str, Any]] = []
        self._flush_task: Optional[asyncio.Task] = None
        self._stream_task: Optional[asyncio.Task] = None
        self._seen_event_ids: Dict[str, float] = {}
        self._subscription_generation = 0

    @property
    def enforces_own_access_policy(self) -> bool:
        # Events arrive over an authenticated AgentMail WebSocket opened by this
        # adapter.  There is no end-user identity for gateway allowlists to check.
        return True

    async def connect(self) -> bool:
        if not WEBSOCKETS_AVAILABLE:
            self._set_fatal_error(
                "agentmail_websockets_missing",
                "Python package 'websockets' is not installed in the Hermes runtime.",
                retryable=False,
            )
            return False
        if not self._api_key:
            self._set_fatal_error(
                "agentmail_api_key_missing",
                "AGENTMAIL_API_KEY is not configured for AgentMail mailbox adapter.",
                retryable=False,
            )
            return False
        if not self._inbox_ids:
            self._set_fatal_error(
                "agentmail_inbox_missing",
                "No AgentMail inbox IDs configured; set AGENTMAIL_INBOX or platform extra.inbox_ids.",
                retryable=False,
            )
            return False

        busy_mode = _current_busy_input_mode()
        if self._require_safe_busy_input_mode and busy_mode not in SAFE_BUSY_INPUT_MODES:
            self._set_fatal_error(
                "agentmail_unsafe_busy_input_mode",
                (
                    "AgentMail mailbox requires display.busy_input_mode to be 'queue' or 'steer' "
                    f"when require_safe_busy_input_mode=true; current mode is {busy_mode!r}."
                ),
                retryable=False,
            )
            return False

        self._running = True
        self._stream_task = asyncio.create_task(self._run_forever(), name="agentmail-mailbox-ws")
        self._mark_connected()
        logger.info(
            "[%s] Started outbound AgentMail WebSocket task for %d inbox(es), event_types=%s, session_chat_id=%s, busy_input_mode=%s, notification_min_interval=%.1fs, batch_window=%.1fs, wait_for_idle_when_unsafe_busy=%s",
            self.name,
            len(self._inbox_ids),
            ",".join(self._event_types),
            self._session_chat_id,
            busy_mode,
            self._notification_min_interval_seconds,
            self._notification_batch_window_seconds,
            self._wait_for_idle_when_unsafe_busy,
        )
        return True

    async def disconnect(self) -> None:
        self._running = False
        self._mark_disconnected()
        if self._flush_task:
            self._flush_task.cancel()
            try:
                await self._flush_task
            except asyncio.CancelledError:
                pass
            self._flush_task = None
        async with self._pending_lock:
            self._pending_notifications.clear()
        if self._stream_task:
            self._stream_task.cancel()
            try:
                await self._stream_task
            except asyncio.CancelledError:
                pass
            self._stream_task = None
        self._seen_event_ids.clear()
        logger.info("[%s] Disconnected", self.name)

    async def _run_forever(self) -> None:
        attempt = 0
        while self._running:
            try:
                await self._connect_and_consume()
                attempt = 0
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if not self._running:
                    break
                delay = RECONNECT_BACKOFF[min(attempt, len(RECONNECT_BACKOFF) - 1)]
                delay = delay + random.random()
                attempt += 1
                logger.warning("[%s] WebSocket loop failed: %s; reconnecting in %.1fs", self.name, exc, delay)
                await asyncio.sleep(delay)

    async def _connect_and_consume(self) -> None:
        assert websockets is not None
        url = self._ws_url
        sep = "&" if "?" in url else "?"
        url = f"{url}{sep}api_key={urllib.parse.quote(self._api_key)}"
        self._subscription_generation += 1
        generation = self._subscription_generation
        logger.info("[%s] Connecting outbound WebSocket to AgentMail (generation=%d)", self.name, generation)
        async with websockets.connect(url, open_timeout=20, ping_interval=30, ping_timeout=20) as ws:
            subscribe = {
                "type": "subscribe",
                "event_types": self._event_types,
                "inbox_ids": self._inbox_ids,
            }
            await ws.send(json.dumps(subscribe))
            async for raw in ws:
                if not self._running:
                    return
                await self._handle_ws_message(raw)

    async def _handle_ws_message(self, raw: Any) -> None:
        try:
            data = json.loads(raw if isinstance(raw, str) else raw.decode("utf-8"))
        except Exception:
            logger.debug("[%s] Ignoring non-JSON WebSocket frame", self.name)
            return

        msg_type = data.get("type")
        if msg_type == "subscribed":
            logger.info(
                "[%s] Subscribed to AgentMail events: inbox_ids=%s event_types=%s",
                self.name,
                data.get("inbox_ids") or self._inbox_ids,
                data.get("event_types") or self._event_types,
            )
            return
        if msg_type == "error":
            safe_error = _redacted_ws_error(data)
            logger.warning("[%s] AgentMail WebSocket error event: %s", self.name, safe_error)
            # Treat server-side error frames as connection failures. If the
            # error is transient this reconnects; if it is configuration/auth,
            # the bounded retry loop keeps the failure operator-visible instead
            # of silently leaving the mailbox unwatched.
            raise RuntimeError(f"AgentMail WebSocket error event: {safe_error}")
        if msg_type != "event":
            logger.debug("[%s] Ignoring AgentMail WebSocket message type=%r", self.name, msg_type)
            return

        event_type = str(data.get("event_type") or "")
        if not event_type.startswith("message.received"):
            logger.debug("[%s] Ignoring AgentMail event_type=%s", self.name, event_type)
            return
        event_id = str(data.get("event_id") or "") or uuid.uuid4().hex
        if self._is_duplicate(event_id):
            logger.debug("[%s] Duplicate AgentMail event %s skipped", self.name, event_id)
            return
        await self._queue_mail_notification(data)

    async def _queue_mail_notification(self, data: Dict[str, Any]) -> None:
        notification = _mail_notification_from_event(data)
        async with self._pending_lock:
            self._pending_notifications.append(notification)
            pending_count = len(self._pending_notifications)
            self._ensure_flush_task_locked()
        logger.info(
            "[%s] Queued AgentMail %s notification for mailbox batch: inbox=%s thread=%s message=%s pending=%d",
            self.name,
            notification["kind"],
            notification["inbox_id"],
            notification["thread_id"],
            notification["message_id"],
            pending_count,
        )

    def _ensure_flush_task_locked(self) -> None:
        if self._flush_task is None or self._flush_task.done():
            self._flush_task = asyncio.create_task(
                self._flush_pending_notifications(),
                name="agentmail-mailbox-notification-flush",
            )

    async def _flush_pending_notifications(self) -> None:
        """Coalesce queued mail notifications and inject one mailbox turn.

        Multiple AgentMail events can arrive while the adapter is pacing or while
        the mailbox session is active.  There is no value in steering one Hermes
        event per email: a single batch prompt tells the same long-running
        mailbox agent which message/thread IDs changed, and the agent can inspect
        the live mailbox state through MCP before deciding what to do.
        """
        try:
            while self._running:
                batch_window = max(0.0, self._notification_batch_window_seconds)
                if batch_window > 0:
                    await asyncio.sleep(batch_window)
                source = self._build_mailbox_source()
                notifications: List[Dict[str, Any]] = []
                try:
                    async with self._dispatch_lock:
                        await self._respect_notification_min_interval()
                        await self._wait_for_safe_busy_slot(source)
                        notifications = await self._drain_pending_notifications()
                        if not notifications:
                            return
                        event = self._build_batch_event(notifications, source)
                        self._last_dispatch_at = time.monotonic()
                        logger.info(
                            "[%s] Dispatching AgentMail mailbox batch with %d notification(s): kinds=%s threads=%s",
                            self.name,
                            len(notifications),
                            sorted({n["kind"] for n in notifications}),
                            sorted({n["thread_id"] for n in notifications if n.get("thread_id")}),
                        )
                        await self.handle_message(event)
                except asyncio.CancelledError:
                    if notifications:
                        await self._requeue_notifications_front(notifications)
                    raise
                except Exception as exc:
                    if notifications:
                        await self._requeue_notifications_front(notifications)
                    logger.warning("[%s] Failed to dispatch AgentMail notification batch: %s", self.name, exc, exc_info=True)
                    await asyncio.sleep(1.0)

                async with self._pending_lock:
                    if not self._pending_notifications:
                        return
        finally:
            current = asyncio.current_task()
            restart = False
            async with self._pending_lock:
                if self._flush_task is current:
                    self._flush_task = None
                restart = self._running and bool(self._pending_notifications)
                if restart:
                    self._ensure_flush_task_locked()

    def _build_mailbox_source(self):
        return self.build_source(
            chat_id=self._session_chat_id,
            chat_name=self._session_name,
            chat_type="dm",
            user_id="agentmail-mailbox",
            user_name="AgentMail Mailbox",
        )

    async def _drain_pending_notifications(self) -> List[Dict[str, Any]]:
        async with self._pending_lock:
            notifications = list(self._pending_notifications)
            self._pending_notifications.clear()
            return notifications

    async def _requeue_notifications_front(self, notifications: List[Dict[str, Any]]) -> None:
        async with self._pending_lock:
            self._pending_notifications = list(notifications) + self._pending_notifications
            self._ensure_flush_task_locked()

    def _build_batch_event(self, notifications: List[Dict[str, Any]], source) -> MessageEvent:
        text = _build_mail_batch_prompt(notifications)
        latest_timestamp = max((n["timestamp"] for n in notifications), default=datetime.now(tz=timezone.utc))
        event_ids = [str(n.get("event_id") or "") for n in notifications if n.get("event_id")]
        message_ids = [str(n.get("message_id") or "") for n in notifications if n.get("message_id")]
        batch_id = event_ids[0] if len(event_ids) == 1 else f"agentmail-batch-{uuid.uuid4().hex[:12]}"
        return MessageEvent(
            text=text,
            message_type=MessageType.TEXT,
            source=source,
            raw_message={
                "event_type": "agentmail.mailbox.batch",
                "event_ids": event_ids,
                "message_ids": message_ids,
                "notifications": [n["raw"] for n in notifications],
            },
            message_id=batch_id,
            timestamp=latest_timestamp,
            auto_skill=self._auto_skill,
            channel_prompt=self._channel_prompt,
            internal=True,
        )

    async def _respect_notification_min_interval(self) -> None:
        interval = max(0.0, self._notification_min_interval_seconds)
        if interval <= 0 or self._last_dispatch_at <= 0:
            return
        elapsed = time.monotonic() - self._last_dispatch_at
        delay = interval - elapsed
        if delay > 0:
            logger.info("[%s] Pacing AgentMail notification dispatch for %.1fs", self.name, delay)
            await asyncio.sleep(delay)

    async def _wait_for_safe_busy_slot(self, source) -> None:
        busy_mode = _current_busy_input_mode()
        if busy_mode in SAFE_BUSY_INPUT_MODES or not self._wait_for_idle_when_unsafe_busy:
            return

        session_key = build_session_key(
            source,
            group_sessions_per_user=self.config.extra.get("group_sessions_per_user", True),
            thread_sessions_per_user=self.config.extra.get("thread_sessions_per_user", False),
        )
        last_log = 0.0
        while self._running and session_key in self._active_sessions:
            try:
                self._heal_stale_session_lock(session_key)
            except Exception:
                pass
            if session_key not in self._active_sessions:
                break
            now = time.monotonic()
            if now - last_log >= 60 or last_log == 0:
                logger.info(
                    "[%s] Waiting for mailbox session to become idle before dispatching next AgentMail notification because busy_input_mode=%s is not queue/steer",
                    self.name,
                    busy_mode,
                )
                last_log = now
            await asyncio.sleep(1.0)

    def _is_duplicate(self, event_id: str) -> bool:
        now = time.time()
        if len(self._seen_event_ids) > DEDUP_MAX_SIZE:
            cutoff = now - 3600
            self._seen_event_ids = {k: v for k, v in self._seen_event_ids.items() if v > cutoff}
        if event_id in self._seen_event_ids:
            return True
        self._seen_event_ids[event_id] = now
        return False

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """Suppress Hermes chat replies; outbound email must use AgentMail MCP tools."""
        if self._noop_send:
            logger.info(
                "[%s] Suppressed platform send (%d chars). Mailbox agent should use AgentMail MCP tools for outbound email.",
                self.name,
                len(content or ""),
            )
            return SendResult(success=True, message_id=f"agentmail-noop-{uuid.uuid4().hex[:12]}")
        logger.warning("[%s] noop_send=false has no implemented delivery path; suppressing anyway", self.name)
        return SendResult(success=True, message_id=f"agentmail-noop-{uuid.uuid4().hex[:12]}")

    async def send_typing(self, chat_id: str, metadata=None) -> None:
        return None

    async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
        return {"name": self._session_name, "type": "dm"}


def _csv_or_list(value: Any) -> List[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [part.strip() for part in value.split(",") if part.strip()]
    if isinstance(value, (list, tuple, set)):
        return [str(part).strip() for part in value if str(part).strip()]
    return [str(value).strip()] if str(value).strip() else []


def _bool_config(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() not in {"0", "false", "no", "off", ""}


def _float_config(value: Any, default: float) -> float:
    if value is None or value == "":
        return default
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        return default


def _current_busy_input_mode() -> str:
    mode = os.getenv("HERMES_GATEWAY_BUSY_INPUT_MODE", "").strip().lower()
    if mode in {"queue", "steer", "interrupt"}:
        return mode
    return "interrupt"


def _event_kind(event_type: str, labels: Any) -> str:
    label_set = {str(x).lower() for x in (labels or [])}
    if event_type.endswith(".spam") or "spam" in label_set:
        return "spam"
    if event_type.endswith(".unauthenticated") or "unauthenticated" in label_set:
        return "unauthenticated"
    if event_type.endswith(".blocked") or "blocked" in label_set:
        return "blocked"
    return "regular"


def _parse_ts(raw: Any) -> datetime:
    if isinstance(raw, str) and raw:
        try:
            return datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except Exception:
            pass
    return datetime.now(tz=timezone.utc)


def _default_session_chat_id(inboxes: List[str]) -> str:
    explicit = os.getenv("AGENTMAIL_MAILBOX_SESSION", "").strip()
    if explicit:
        return explicit
    if inboxes:
        local = str(inboxes[0]).split("@", 1)[0]
        suffix = re.sub(r"[^a-z0-9_.-]+", "-", local.lower()).strip("-._")
        if suffix:
            return f"agentmail-mailbox:{suffix}"
    fly_app = os.getenv("FLY_APP_NAME", "").strip().lower()
    if fly_app:
        suffix = re.sub(r"[^a-z0-9_.-]+", "-", fly_app).strip("-._")
        if suffix:
            return f"agentmail-mailbox:{suffix}"
    return "agentmail-mailbox"


def _redacted_ws_error(data: Dict[str, Any]) -> Dict[str, Any]:
    redacted: Dict[str, Any] = {}
    for key, value in data.items():
        key_s = str(key)
        if any(token in key_s.lower() for token in ("api_key", "apikey", "token", "secret", "password")):
            redacted[key_s] = "[REDACTED]"
        else:
            redacted[key_s] = value
    return redacted


def _redacted_message_metadata(message: Dict[str, Any]) -> Dict[str, Any]:
    allowed = ("inbox_id", "thread_id", "message_id", "labels", "timestamp", "created_at", "updated_at", "size")
    return {k: message.get(k) for k in allowed if k in message}


def _mail_notification_from_event(data: Dict[str, Any]) -> Dict[str, Any]:
    message = data.get("message") or {}
    thread = data.get("thread") or {}
    event_type = str(data.get("event_type") or "message.received")
    event_id = str(data.get("event_id") or "")
    labels = message.get("labels") or []
    inbox_id = str(message.get("inbox_id") or thread.get("inbox_id") or "")
    thread_id = str(message.get("thread_id") or thread.get("thread_id") or "")
    message_id = str(message.get("message_id") or "")
    kind = _event_kind(event_type, labels)
    timestamp = _parse_ts(message.get("timestamp") or message.get("created_at"))
    return {
        "event_type": event_type,
        "kind": kind,
        "event_id": event_id,
        "inbox_id": inbox_id,
        "thread_id": thread_id,
        "message_id": message_id,
        "labels": labels,
        "timestamp": timestamp,
        "raw": {
            "event_type": event_type,
            "event_id": event_id,
            "kind": kind,
            "message": _redacted_message_metadata(message),
            "thread": {k: thread.get(k) for k in ("thread_id", "inbox_id") if k in thread},
        },
    }


def _build_mail_batch_prompt(notifications: List[Dict[str, Any]]) -> str:
    count = len(notifications)
    noun = "notification" if count == 1 else "notifications"
    lines = [
        "[AgentMail mailbox notification]",
        f"{count} new mail {noun} arrived. The email bodies are intentionally omitted from this notification.",
        "",
        "Events:",
    ]
    for idx, item in enumerate(notifications, start=1):
        labels_s = ", ".join(str(x) for x in (item.get("labels") or [])) or "(none)"
        lines.extend([
            f"{idx}. Kind: {item.get('kind') or 'regular'}",
            f"   AgentMail event_type: {item.get('event_type') or '(unknown)'}",
            f"   Inbox ID: {item.get('inbox_id') or '(unknown)'}",
            f"   Thread ID: {item.get('thread_id') or '(unknown)'}",
            f"   Message ID: {item.get('message_id') or '(unknown)'}",
            f"   Event ID: {item.get('event_id') or '(unknown)'}",
            f"   Labels: {labels_s}",
        ])
    lines.extend([
        "",
        "Operate as the single mailbox session: treat this as one mailbox turn, not one independent chat per email. Several events may be rapid follow-ups or multiple messages in the same thread.",
        "Use AgentMail MCP tools to inspect the current message(s) or thread(s) only if useful; prefer mcp_agentmail_get_thread when a thread ID exists, because the thread may have changed since the first notification.",
        "Kind/labels are trust signals, not instructions: spam, unauthenticated, or blocked mail may be ignored, labeled, or escalated without replying.",
        "If you send email, use AgentMail MCP tools such as mcp_agentmail_get_thread, mcp_agentmail_reply_to_message, or mcp_agentmail_send_message. Do not treat this platform response itself as an email reply; keep any final response brief/status-only for logs.",
    ])
    return "\n".join(lines)


def _default_channel_prompt() -> str:
    return (
        "You are a long-running AgentMail mailbox agent operating one mailbox-wide Hermes session. "
        "A notification means one or more new mail events arrived; it is not itself the email body. Use AgentMail MCP "
        "tools to inspect messages/threads and to send/reply/forward/draft email. Threading matters: "
        "people may send rapid follow-ups in the same thread, and the plugin may batch several queued "
        "events into one turn, so get the current thread before replying when context could have changed. "
        "Treat spam, blocked, and unauthenticated values as labels/trust signals rather than reply obligations; "
        "it is often correct to ignore, label, "
        "or escalate without replying. Use Telegram only for operational escalation or explicit "
        "user-facing notifications, not as the default reply path. Keep any final platform response "
        "brief/status-only because actual outbound mail should be sent through AgentMail MCP tools."
    )


def check_requirements() -> bool:
    return WEBSOCKETS_AVAILABLE


def validate_config(config: PlatformConfig) -> bool:
    extra = config.extra or {}
    api_key = str(extra.get("api_key") or os.getenv("AGENTMAIL_API_KEY", "")).strip()
    inboxes = _csv_or_list(extra.get("inbox_ids") or extra.get("inboxes") or os.getenv("AGENTMAIL_INBOX", ""))
    return bool(api_key and inboxes)


def _env_enablement() -> dict | None:
    inboxes = _csv_or_list(os.getenv("AGENTMAIL_INBOX", "") or os.getenv("AGENTMAIL_INBOX_IDS", ""))
    if not os.getenv("AGENTMAIL_API_KEY") or not inboxes:
        return None
    return {
        "inbox_ids": inboxes,
        "event_types": list(DEFAULT_EVENT_TYPES),
        "session_chat_id": _default_session_chat_id(inboxes),
        "auto_skill": "agentmail-mailbox-operator",
        "noop_send": True,
        "notification_min_interval_seconds": DEFAULT_NOTIFICATION_MIN_INTERVAL_SECONDS,
        "notification_batch_window_seconds": DEFAULT_NOTIFICATION_BATCH_WINDOW_SECONDS,
        "wait_for_idle_when_unsafe_busy": True,
    }


def _apply_yaml_config(yaml_cfg: dict, platform_cfg: dict) -> dict | None:
    extra = platform_cfg.get("extra") if isinstance(platform_cfg, dict) else None
    return dict(extra or {}) if isinstance(extra, dict) else None


def _install_bundled_skill() -> None:
    """Expose the plugin-bundled mailbox skill to Hermes' normal skill loader.

    Native plugin installs clone only the plugin repository into
    ``$HERMES_HOME/plugins/<name>``; Hermes does not yet have a plugin manifest
    field that declares bundled skills.  Copy the included skill on startup when
    it is missing so MessageEvent.auto_skill can load it by name. Existing skills
    are left alone to avoid clobbering operator edits.
    """
    try:
        skill_src = Path(__file__).resolve().parent / "skills" / "email" / "agentmail-mailbox-operator"
        if not (skill_src / "SKILL.md").is_file():
            return
        from hermes_constants import get_hermes_home
        skill_dst = get_hermes_home() / "skills" / "email" / "agentmail-mailbox-operator"
        if (skill_dst / "SKILL.md").exists():
            return
        skill_dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(skill_src, skill_dst)
        logger.info("[%s] Installed bundled skill guidance to %s", PLATFORM_NAME, skill_dst)
    except Exception as exc:
        logger.warning("[%s] Failed to install bundled AgentMail skill guidance: %s", PLATFORM_NAME, exc)


def register(ctx) -> None:
    _install_bundled_skill()
    ctx.register_platform(
        name=PLATFORM_NAME,
        label="AgentMail Mailbox",
        adapter_factory=lambda cfg: AgentMailMailboxAdapter(cfg),
        check_fn=check_requirements,
        validate_config=validate_config,
        is_connected=validate_config,
        required_env=["AGENTMAIL_API_KEY", "AGENTMAIL_INBOX"],
        install_hint="websockets is included in the Hermes container; configure AGENTMAIL_API_KEY and AGENTMAIL_INBOX",
        env_enablement_fn=_env_enablement,
        apply_yaml_config_fn=_apply_yaml_config,
        allowed_users_env="AGENTMAIL_MAILBOX_ALLOWED_USERS",
        allow_all_env="AGENTMAIL_MAILBOX_ALLOW_ALL_USERS",
        max_message_length=AgentMailMailboxAdapter.MAX_MESSAGE_LENGTH,
        emoji="📬",
        pii_safe=True,
        allow_update_command=False,
        platform_hint=_default_channel_prompt(),
    )
