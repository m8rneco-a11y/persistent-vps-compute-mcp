# Security model

This project intentionally exposes a real root terminal. It is not a restricted sandbox.

## Treat the Bearer token as a root password

Anyone who can reach the MCP endpoint and authenticate can run arbitrary commands as `root`. Never place the token in a Skill page, README, issue, screenshot, chat message, shell history, or public environment variable.

The installer stores it only in `/etc/mcp-compute.env` with mode `0600`. Retrieve the bare value locally on the VPS with:

```bash
sudo /opt/mcp-compute/scripts/show-token.sh
```

Rotate it after a suspected disclosure or an account migration:

```bash
sudo /opt/mcp-compute/scripts/rotate-token.sh
```

## Network boundaries

- The Python service binds to `127.0.0.1` by default.
- Only Caddy should be reachable from the internet on TCP 80/443.
- Do not expose the internal MCP port directly.
- Use a dedicated DNS name and valid HTTPS certificate.
- Keep the health endpoint minimal; it returns only `ok`.

## Agent boundaries

- Connect only trusted personal agents.
- `Always allow` removes confirmation prompts and therefore grants unattended root execution.
- Do not share a Custom Agent that carries this connection unless every editor is meant to have root access.
- External text, repository instructions, web pages, and retrieved files may contain prompt injection. The Skill tells the agent to preserve unrelated services and verify destructive targets, but this is not a hard security boundary.

## Host boundaries

Use a dedicated development VPS whenever practical. Maintain provider snapshots or application-level backups. A compromised root-capable MCP is equivalent to a compromised server.

## Reporting a problem

Because this repository is private, report security issues directly to its owner. Do not open a public issue containing credentials, hostnames, IP addresses, or logs with secrets.
