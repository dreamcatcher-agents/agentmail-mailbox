import inspect
from pathlib import Path

from gateway.config import PlatformConfig

import adapter


def clear_agentmail_env(monkeypatch):
    for name in (
        "AGENTMAIL_API_KEY",
        "AGENTMAIL_INBOX",
        "AGENTMAIL_INBOX_IDS",
        "AGENTMAIL_MAILBOX_SESSION",
        "FLY_APP_NAME",
    ):
        monkeypatch.delenv(name, raising=False)


def test_env_enablement_requires_explicit_mailbox_session(monkeypatch):
    clear_agentmail_env(monkeypatch)
    monkeypatch.setenv("AGENTMAIL_API_KEY", "test-key")
    monkeypatch.setenv("AGENTMAIL_INBOX", "persona@example.test")

    assert adapter._env_enablement() is None


def test_env_enablement_uses_explicit_mailbox_session(monkeypatch):
    clear_agentmail_env(monkeypatch)
    monkeypatch.setenv("AGENTMAIL_API_KEY", "test-key")
    monkeypatch.setenv("AGENTMAIL_INBOX", "persona@example.test")
    monkeypatch.setenv("AGENTMAIL_MAILBOX_SESSION", "agentmail-mailbox:persona")

    cfg = adapter._env_enablement()

    assert cfg is not None
    assert cfg["session_chat_id"] == "agentmail-mailbox:persona"
    assert cfg["inbox_ids"] == ["persona@example.test"]


def test_validate_config_requires_explicit_mailbox_session(monkeypatch):
    clear_agentmail_env(monkeypatch)
    monkeypatch.setenv("AGENTMAIL_API_KEY", "test-key")
    monkeypatch.setenv("AGENTMAIL_INBOX", "persona@example.test")

    assert adapter.validate_config(PlatformConfig(extra={})) is False

    monkeypatch.setenv("AGENTMAIL_MAILBOX_SESSION", "agentmail-mailbox:persona")

    assert adapter.validate_config(PlatformConfig(extra={})) is True


def test_validate_config_accepts_platform_session_override(monkeypatch):
    clear_agentmail_env(monkeypatch)
    monkeypatch.setenv("AGENTMAIL_API_KEY", "test-key")
    monkeypatch.setenv("AGENTMAIL_INBOX", "persona@example.test")

    cfg = PlatformConfig(extra={"session_chat_id": "agentmail-mailbox:persona"})

    assert adapter.validate_config(cfg) is True


def test_connect_accepts_gateway_reconnect_contract():
    signature = inspect.signature(adapter.AgentMailMailboxAdapter.connect)

    parameter = signature.parameters["is_reconnect"]
    assert parameter.kind is inspect.Parameter.KEYWORD_ONLY
    assert parameter.default is False


def test_default_prompts_include_cc_and_reply_storm_etiquette():
    channel_prompt = adapter._default_channel_prompt()
    batch_prompt = adapter._build_mail_batch_prompt([
        {
            "kind": "regular",
            "event_type": "message.received",
            "inbox_id": "persona@example.test",
            "thread_id": "thread-1",
            "message_id": "message-1",
            "event_id": "event-1",
            "labels": [],
        }
    ])

    for prompt in (channel_prompt, batch_prompt):
        normalized = prompt.lower()
        assert "being cc'd is usually an awareness signal" in normalized
        assert "avoid reply-all by default" in normalized
        assert "silence is better" in normalized


def test_bundled_skill_includes_email_etiquette_section():
    skill_path = (
        Path(__file__).resolve().parents[1]
        / "skills"
        / "email"
        / "agentmail-mailbox-operator"
        / "SKILL.md"
    )
    content = skill_path.read_text(encoding="utf-8")

    assert "## Recipient posture and reply obligation" in content
    assert "## High-cost-reader etiquette" in content
    assert "Being CC'd is usually an awareness signal" in content
    assert "Silence is a valid storm-prevention action" in content
    assert "Do background work before asking" in content
