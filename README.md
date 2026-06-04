# AgentMail Mailbox Hermes plugin

Outbound-only AgentMail mailbox platform adapter for Hermes.

The plugin opens an outbound WebSocket to AgentMail at gateway startup, subscribes to watched mailbox events, batches queued notifications into a single minimal mailbox turn, and routes every notification to one stable Hermes mailbox session. It avoids public webhook ingress and does not treat Hermes platform final replies as outbound email; the mailbox agent should inspect/send/reply/update mail through AgentMail MCP tools.

## Native install

```bash
hermes plugins install dreamcatcher-agents/agentmail-mailbox --enable
```

Set `AGENTMAIL_API_KEY` and `AGENTMAIL_INBOX` in the target Hermes environment, then restart the gateway. The plugin auto-enables its synthetic platform only when those values are present.

## Runtime shape

- Plugin name: `agentmail-mailbox`
- Platform name: `agentmail_mailbox`
- Required env: `AGENTMAIL_API_KEY`, `AGENTMAIL_INBOX` or platform `extra.inbox_ids`
- Optional env/config: `AGENTMAIL_MAILBOX_SESSION` to override the stable session id, `AGENTMAIL_MAILBOX_NOTIFICATION_MIN_INTERVAL_SECONDS`, `AGENTMAIL_MAILBOX_NOTIFICATION_BATCH_WINDOW_SECONDS`
- Default session id: `agentmail-mailbox:<first-inbox-local-part>` when no explicit session is set
- Watched event classes: regular, spam, unauthenticated, and blocked received-mail events
- Bundled skill guidance: `skills/email/agentmail-mailbox-operator` copied into `$HERMES_HOME/skills` on startup if missing

## AgentMail MCP

Configure Hermes native MCP separately so the mailbox session can operate AgentMail rather than merely receive notifications. A least-privilege local server shape is:

```yaml
mcp_servers:
  agentmail:
    command: npx
    args:
      - -y
      - agentmail-mcp
      - --tools
      - list_inboxes,get_inbox,list_threads,get_thread,get_attachment,send_message,reply_to_message,forward_message,update_message
    env:
      AGENTMAIL_API_KEY: ${AGENTMAIL_API_KEY}
```

The LLM-facing tool list should omit `create_inbox` and `delete_inbox` unless inbox management is explicitly in scope.

## Busy-session behavior

Hermes gateway busy mode is global to the running gateway/profile, not per plugin session. This plugin protects mailbox ordering locally:

- It coalesces rapid arrivals with `notification_batch_window_seconds` (default `1`).
- It paces dispatches with `notification_min_interval_seconds` (default `10`).
- If gateway busy mode is `steer` or `queue`, the current batch is handed to Hermes after pacing.
- If gateway busy mode is `interrupt` or unknown, the plugin waits for the mailbox session to become idle before dispatching the next batch, preventing accidental interruption of in-flight mailbox work.

## WebSocket durability

Startup configuration errors fail closed. After successful start, connection failures and AgentMail WebSocket `error` frames flow through the retry loop with bounded backoff and jitter. WebSocket ping/pong keepalive is enabled so dead connections are noticed.
