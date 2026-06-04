# EXP Hermes AgentMail mailbox experiment notes

Use this reference as a compact evidence bank when recreating or verifying the outbound WebSocket mailbox-agent pattern on a Hermes experiment agent.

## Proven shape

- Fly experiment app reused a spec-based Hermes image and ran with no public services/ports (`services` empty/null).
- A persisted platform plugin loaded at gateway startup and opened an outbound WebSocket to AgentMail.
- The plugin subscribed to all mailbox-relevant receive events:
  - `message.received`
  - `message.received.spam`
  - `message.received.unauthenticated`
  - `message.received.blocked`
- The notification prompt intentionally omitted the email body and only included routing/trust metadata: kind, event type, inbox id, thread id, message id, labels, and event id.
- All notifications used one stable synthetic chat/session id (for example `agentmail-mailbox:exp`) so Hermes resumed one mailbox-wide session instead of spawning per-message sessions.
- The platform `send()` path was suppressed/no-op; real outbound email was done with AgentMail MCP tools.

## Verification markers to collect

- Gateway startup log shows plugin/platform registration and WebSocket connect.
- AgentMail WebSocket log shows subscribe acknowledgement for the expected inbox ids and event types.
- Fly/Machine inspection shows no public ingress if the goal is outbound-only operation.
- A real test email produces a log line equivalent to “dispatching ... to single session”.
- The same session id is reused for rapid follow-ups.
- The mailbox skill/guidance is loaded or injected in the session context.
- MCP discovery exposes only mailbox-operation tools needed by the agent. A safe observed local filter was:
  `list_inboxes,get_inbox,list_threads,get_thread,get_attachment,send_message,reply_to_message,forward_message,update_message`
- MCP discovery should not expose `create_inbox` or `delete_inbox` unless the agent is explicitly allowed to manage inbox lifecycle.
- Logs show final platform responses suppressed, not delivered as email/Telegram messages.
- Reconnect behavior is visible after WebSocket disconnects: bounded backoff, reconnect, resubscribe, no API key/message-body leakage in logs.

## Pitfalls from the experiment

- A stable synthetic `chat_id` is necessary glue for Hermes session routing. It is not an external chat; it is the key that keeps one mailbox session alive across notifications and restarts. Do not derive it from event id, message id, or thread id.
- `busy_input_mode=steer` is not truly session-scoped in current upstream Hermes. On a dedicated EXP agent it is effectively isolated, but a platform plugin cannot guarantee strict live steering if a notification arrives before the AIAgent is registered as steerable; queued follow-up turns are an acceptable fallback for one-at-a-time mailbox processing.
- AgentMail docs and package versions can diverge. Draft tools may be documented but absent from a particular `agentmail-mcp` release; always inspect the actual `mcp_agentmail_*` tool list before prompt-tuning.
- Filtering MCP tools reduces the LLM-facing tool surface, but it is not a substitute for least-privilege API credentials if AgentMail later supports key-level scopes.
- If the platform is only a notification source, do not let final assistant text be confused with a delivered reply. Make the status/log-only nature explicit in both the adapter and the skill prompt.

## Design preference captured

For human-facing mailboxes, prefer one mailbox-wide Hermes session over one session per inbound email. Humans perceive “the mailbox” and “the thread” as shared context; rapid correction emails such as “oh wait…” should be handled by re-reading the thread in the same mailbox session before acting.
