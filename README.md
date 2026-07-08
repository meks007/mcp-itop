# mcp-itop

MCP server for **iTop ITSM** - analytics, tickets, comments, knowledge base, CI.

Provides AI assistants (opencode, Claude Desktop, Cursor) with **19 tools**
for working with iTop: SLA analytics, agent workload, service quality,
ticket lifecycle, KB search, CI impact analysis.

Originally based on/inspired by [gest0r1/mcp-itop](https://github.com/gest0r1/mcp-itop) - thank you for the foundation this project builds on.

## Features

### Analytics
| Tool | Description |
|------|-------------|
| `itop_sla_report` | SLA report for a service over a period (TTO/TTR passed/breached/N/A, median resolution) |
| `itop_agent_workload` | Agent workload: closed/open tickets, time_spent, backlog |
| `itop_idle_agents` | Find tickets where the agent has been idle for >N hours |
| `itop_service_quality` | Find similar tickets assigned to different services |
| `itop_caller_quality` | Quality of service selection by end users |
| `itop_agent_correction_rate` | Agents who do/don't correct misassigned services |
| `itop_ticket_summary` | Dashboard: created/resolved/open/SLA breaches |

### Comments
| Tool | Description |
|------|-------------|
| `itop_add_comment` | Add a public or private comment to a ticket |
| `itop_get_log` | Read comment history (public_log, private_log) |

### Knowledge base
| Tool | Description |
|------|-------------|
| `itop_search_kb` | Search KB articles (supports KBEntry and FAQ) |
| `itop_get_kb_article` | Full text of an article |
| `itop_list_kb_categories` | List KB categories |

### CRUD + Lifecycle
| Tool | Description |
|------|-------------|
| `itop_get` | Search objects (OQL / ID / JSON criteria) |
| `itop_create` | Create an object |
| `itop_update` | Update object fields |
| `itop_delete` | Delete with simulate mode |
| `itop_apply_stimulus` | Lifecycle transitions: ev_assign, ev_resolve, ev_close, ev_reopen |
| `itop_get_related` | CI impact analysis (impacts/depends on) |
| `itop_describe_class` | Discover class fields from an existing object |

## Authentication

The server no longer stores an iTop token in its own environment
variables. Instead, each MCP client sends its own iTop token as a
bearer token in the `Authorization: Bearer <itop_token>` HTTP header
when it connects.

The server verifies that a non-empty bearer token is present during the
MCP handshake (`initialize`) - connections without one are rejected
with 401 before the client can see the list of tools. That same token
is then used for every iTop REST/JSON API call made on behalf of that
client.

### What changed

Previously, iTop credentials (`ITOP_TOKEN` or `ITOP_USER`/`ITOP_PASSWORD`)
were configured once as server-wide environment variables, shared by
every connected client. This has been replaced with per-client bearer
token authentication:

- `ITOP_TOKEN`, `ITOP_USER`, and `ITOP_PASSWORD` env vars have been removed.
- Each client now presents its own iTop token as a bearer token, validated
  at the MCP handshake by a custom token verifier.
- The validated token is forwarded to iTop as `auth_token` on every
  REST/JSON API call for that client.
- The transport changed from `stdio` to `streamable-http`, since per-request
  HTTP Authorization headers are required to support per-client tokens.
- The Docker image no longer bundles `mcp-proxy` / `entrypoint.sh`; the
  container now runs `server.py` directly, since it is HTTP-native.

## Quick start

### 1. Install

```bash
pip install mcp[fastmcp] httpx python-dotenv
```

### 2. Configuration (global config)

```bash
mkdir -p ~/.config/mcp-itop
cat > ~/.config/mcp-itop/.env << 'CONFIG'
ITOP_URL=https://your-itop.example.com
ITOP_VERSION=1.3
ITOP_VERIFY_SSL=true
ITOP_TIMEOUT=30

# Optional: server bind address/port (streamable-http transport)
# MCP_HOST=0.0.0.0
# MCP_PORT=8096
CONFIG
```

No iTop token is set here - it is supplied by the client (see "Authentication" above).

### 3. Run

```bash
python server.py
```

The server runs on the `streamable-http` transport (default
`0.0.0.0:8096`), since HTTP is required to carry the client's bearer token.

## Integration

### opencode (global config)

Add to `~/.config/opencode/opencode.json`:

```json
"itop": {
  "type": "remote",
  "url": "http://localhost:8096/mcp",
  "headers": {
    "Authorization": "Bearer your_token"
  },
  "enabled": true
}
```

### opencode (per project)

Add to the project's `opencode.json`:

```json
{
  "mcpServers": {
    "itop": {
      "url": "http://localhost:8096/mcp",
      "headers": {
        "Authorization": "Bearer your_token"
      }
    }
  }
}
```

### Claude Desktop

Claude Desktop connects to remote MCP servers via a connector with a URL
and an `Authorization: Bearer <your_token>` header. Point it at the
running server, e.g. `http://localhost:8096/mcp`, and add your iTop
token as the bearer token when configuring the connector.

## Example requests

```
Show me the SLA for "Technical Support" this month
Which agents are overloaded?
Which tickets have been idle for more than 2 hours?
Find similar tickets assigned to different services
Which users often pick the wrong service?
Add a comment to ticket RQ-123
Create a new ticket: printer not working
Assign RQ-456 to Smith
Find CIs related to server srv-web-01
Search the KB for VPN
```

## Compatibility

Tested with:

- **iTop** 3.2.1-1-16749 (PHP 8.1.2, MariaDB 10.6)
- Supports both localized (yes/no) and English (true/false) SLA values
- Auto-detects the KB module: KBEntry -> FAQ

## Requirements

- Python >= 3.10
- `mcp[fastmcp]`
- `httpx`
- `python-dotenv`

## Tests

```bash
python -m pytest tests/ -v
```

## Architecture

```
AI client --(HTTP + Authorization: Bearer <itop_token>)--> server.py (streamable-http) --> iTop REST API
```

The client's token is validated by the server during the MCP handshake
(`initialize`) and then forwarded on every iTop REST API call as `auth_token`.

Configuration priority:
1. `~/.config/mcp-itop/.env` (global, highest priority)
2. `.env` (local, in the project folder)
3. Environment variables

## License

MIT
