# Outbound WebSocket single-session AgentMail pattern for Hermes

Use this reference when building or operating a Hermes AgentMail mailbox agent that should not expose public inbound ports.

## Pattern

- Run a Hermes platform plugin that opens an outbound WebSocket to AgentMail at startup.
- Subscribe to the inbox IDs and event types needed by the mailbox agent, including regular, spam, unauthenticated, and blocked received-mail events when the mailbox agent needs complete visibility.
- Dispatch every notification for the mailbox to one stable Hermes session/chat id rather than creating a new session per email.
- Keep the trigger prompt minimal: tell the agent that one or more mail events arrived and include metadata only (`kind`, event type, inbox, thread id, message id, labels, event id). Do not inject the body by default.
- Batch queued notifications into one mailbox turn when several events arrive while the adapter is pacing or while the mailbox session is active. One Hermes steer/queue event can list multiple AgentMail message events because the mailbox agent must inspect the live thread state anyway.
- Auto-load the mailbox-operator skill or include equivalent operating guidance in the session context.
- Suppress platform final responses if the platform is only a notification transport. The agent should send email through AgentMail MCP tools, not by relying on the Hermes platform response; any final response should be brief/status-only for logs.

## Hermes implementation notes

- A platform adapter can dispatch notifications to a fixed `session_chat_id` to preserve mailbox-wide continuity.
- Hermes profiles are whole-Hermes-home/gateway boundaries. Running only one platform plugin/session under another profile inside an existing gateway is not a neat per-plugin switch; use a dedicated agent/app for profile isolation or make the plugin behavior safe under the active gateway profile.
- Configure the platform/session so one mailbox maps to one long-running session. This lets the agent compact naturally over time while preserving mailbox perception.
- If the platform is not a user-facing chat transport, implement/send-configure a no-op `send()` path and log suppression clearly.
- Ensure platform authorization/pairing state is compatible with synthetic platform users. If the inbound source is already authenticated by AgentMail API/WebSocket credentials, a scoped allow-all/bypass for that platform may be appropriate; keep it platform-specific.
- For Fly experiments that must avoid public ingress, remove machine services/ports and verify the machine still runs the gateway with `services` empty/null.

## AgentMail MCP verification

- Verify the actual MCP tools exposed by the installed server before prompt-tuning. In one run, unfiltered `npx -y agentmail-mcp` exposed 11 tools: create/list/get/delete inbox, list/get threads, get attachment, send/reply/forward/update message.
- For mailbox-runtime agents, filter MCP tools with `--tools` to omit inbox-management capabilities unless the mailbox agent is explicitly supposed to create/delete inboxes. A safe current filter is `list_inboxes,get_inbox,list_threads,get_thread,get_attachment,send_message,reply_to_message,forward_message,update_message`.
- AgentMail docs may mention draft tools, but package/version differences mean drafts may be absent. Skills/prompts should say “use drafts if exposed” rather than assuming availability.

## WebSocket durability

- The outbound WebSocket is an enduring connection. If the connection drops after a successful startup, the plugin should reconnect indefinitely with bounded backoff and jitter.
- Startup validation may fail closed for missing dependencies, API key, or inbox configuration because those are operator/configuration errors rather than transient network failures.
- Use WebSocket ping/pong or equivalent keepalive settings so dead connections are noticed and retried.
- Log connect, subscribe, error, and reconnect events without printing API keys or message bodies.

## Busy-session caveat and mitigation

Hermes `display.busy_input_mode` is currently gateway/profile-global, not per mailbox session. A plugin cannot neatly switch just one mailbox lane into another Hermes profile without running a separate Hermes gateway/process. `busy_input_mode=steer` also does not guarantee every rapid follow-up becomes an in-process steering event: if a notification lands while the session is active but before the AIAgent is registered as steerable, Hermes may queue it as a follow-up turn instead.

For email this is acceptable, and the plugin should make it safe regardless of global config:

- Pace notification batches with a short delay between dispatches; the current default is 10 seconds, with a short coalescing window before each dispatch so rapid arrivals become one prompt.
- If the active gateway mode is `queue` or `steer`, dispatch the current batch after the pacing delay and let Hermes queue/steer.
- If the active gateway mode is `interrupt` or unknown, wait for the mailbox session to become idle before dispatching the next notification so the mailbox agent is not interrupted by global chat settings.
- Optionally fail closed at startup if a deployment explicitly sets `require_safe_busy_input_mode=true` and the gateway is not configured for `queue` or `steer`.

## Verification checklist

1. Confirm plugin loads at gateway startup.
2. Confirm WebSocket connects and subscribes to expected inbox IDs and event types.
3. Confirm public ingress/services are absent if no public ports are allowed.
4. Send a real test email and confirm the log shows dispatch to the fixed mailbox session.
5. Confirm the mailbox skill auto-loads or equivalent instructions appear in context.
6. Confirm the agent uses `mcp_agentmail_get_thread`/related MCP tools to inspect mail.
7. Confirm platform final responses are suppressed or routed only as designed.
8. Send rapid follow-ups and confirm they enter the same session, either as steering or queued follow-up turns.
