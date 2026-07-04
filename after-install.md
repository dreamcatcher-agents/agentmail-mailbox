# AgentMail Mailbox plugin installed

1. Ensure `AGENTMAIL_API_KEY`, `AGENTMAIL_INBOX`, and `AGENTMAIL_MAILBOX_SESSION` are set in this Hermes environment.
2. Enable the plugin if you did not pass `--enable`: `hermes plugins enable agentmail-mailbox`.
3. Configure AgentMail MCP tools separately so the mailbox agent can inspect/reply/update mail.
4. Restart the gateway.

The plugin uses outbound WebSocket only; no public AgentMail webhook route is required.
