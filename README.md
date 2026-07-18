# mcp-itop

MCP server for **iTop ITSM** — analytics, tickets, comments, knowledge base, CI, and image attachments.

Provides AI assistants (opencode, Claude Desktop, Cursor) with **22 tools** and **1 resource** for working with iTop: SLA analytics, agent workload, service quality, ticket lifecycle, KB search, CI impact analysis, and full image attachment retrieval.

Originally inspired by [gest0r1/mcp-itop](https://github.com/gest0r1/mcp-itop) — thank you for the foundation this project builds on.

---

## Features

### Analytics

| Tool | Description |
|------|-------------|
| `itop_sla_report` | SLA compliance report: TTO/TTR pass/breach rates, median and min/max resolution time, average time_spent. Covers all services or a single service over a date range. |
| `itop_agent_workload` | Agent workload: closed/open ticket counts, total time_spent, current backlog per agent or team. Flags overloaded agents (>10 open tickets). |
| `itop_idle_agents` | Find tickets where the assigned agent has been idle for more than N hours. |
| `itop_service_quality` | Find tickets whose service was changed after creation — indicates systematic misassignment. |
| `itop_caller_quality` | Rank end users by how often they pick the wrong service category. |
| `itop_agent_correction_rate` | Compare agents who correct vs. leave misassigned services. |
| `itop_ticket_summary` | Dashboard snapshot: created / resolved / still open / SLA breaches for a period. |

### CRUD and Lifecycle

| Tool | Description |
|------|-------------|
| `itop_get` | Retrieve iTop objects by ticket ref, numeric ID, or OQL query. Auto-detects concrete class from a ref. Supports pagination (`limit`, `page`). Returns image counts as a synthetic `_images` field when applicable. |
| `itop_create` | Create any iTop object. Pass fields as JSON. |
| `itop_update` | Update fields on an existing object. Rejects `status` changes and redirects the caller to `itop_apply_stimulus`. |
| `itop_delete` | Delete objects. Runs in simulation mode by default as a safety guard. |
| `itop_apply_stimulus` | Apply lifecycle transitions: `ev_assign`, `ev_resolve`, `ev_reopen`, `ev_pending`. Blocks `ev_close` by policy. |
| `itop_get_related` | CI impact analysis: find objects related via `impacts` or `depends on` at configurable depth and direction. |
| `itop_list_operations` | List all REST/JSON operations available on the connected iTop server. |
| `itop_describe_class` | Discover the available fields for an iTop class by sampling an existing object. |

### Image Attachments

| Tool / Resource | Description |
|------|-------------|
| `itop_get_ticket_images` | Fetch image attachments and inline images for a ticket. Stores them in a per-session SQLite cache. Returns image count and the MCP resource URI. |
| `itop_get_ticket_attachments` | List all non-image file attachments for a ticket (metadata + browser download links). |
| `itop://attachment/image.jpg` *(resource)* | MCP resource that returns all images from the most recent `itop_get_ticket_images` call for the current session as JPEG binaries. |

**How image retrieval works:**
- Inline images are resolved exclusively from `<img data-img-id data-img-secret>` tags parsed out of ticket HTML fields. The `InlineImage` REST endpoint is intentionally avoided because iTop does not clean up stale `InlineImage` records when the corresponding tag is removed.
- On a cache miss the ticket is re-fetched, HTML is parsed, and refs are written to the `inline_image_refs` SQLite table. On a cache hit the stored refs are used directly.
- All images are normalised to JPEG before storage. Oversized images are first compressed (configurable quality steps), then downscaled if still above `IMAGE_MAX_BYTES`.

### Comments

| Tool | Description |
|------|-------------|
| `itop_add_comment` | Add a public or private comment to a ticket. |
| `itop_get_log` | Read the comment history (`public_log` or `private_log`). |

### Knowledge Base

| Tool | Description |
|------|-------------|
| `itop_search_kb` | Full-text search over KB articles. Auto-detects the available KB module (KBEntry or FAQ). |
| `itop_get_kb_article` | Retrieve the full text of a KB article by ID. |
| `itop_list_kb_categories` | List all KB categories. |

---

## Architecture

```
AI client
  -- HTTP + Authorization: Bearer <itop_token> -->
    server.py  (FastMCP, streamable-http, uvicorn)
      -- iTop REST/JSON API -->
        iTop instance
```

```
server/
  config.py             env vars, logging, constants
  auth.py               ItopMiddleware, bearer token validation
  client.py             iTop REST/JSON HTTP client (httpx, async)
  cache.py              class field registry, resolve_key cache
  background_tasks.py   central housekeeping asyncio loop
  db/
    base.py             DbBackend ABC
    __init__.py         backend selection (DB_BACKEND env var), proxy surface
                        db.execute() / db.executemany() / db.transaction()
                        db.register_schema() / db.register_migration() / db.init()
    sqlite.py           SQLite backend: WAL mode, incremental vacuum thread
  attachment_store/
    session.py          per-session image store (attachment_sessions table)
    refs.py             inline image ref cache (inline_image_refs table)
    image.py            JPEG normalisation and downscaling
  helpers/              formatting, HTML parsing, OQL helpers, SLA detection
  tools/
    analytics.py        SLA, workload, idle agents, service/caller quality
    crud.py             generic CRUD + stimulus + impact + describe tools
    comments.py         ticket log read/write
    kb.py               knowledge base search and retrieval
    attachments.py      image/file attachment tools + static image resource
```

**Database layer:** All domain modules register their DDL at import time via `db.register_schema()`. A single `db.init()` call in `server.py` connects the backend and runs all registered DDL — no per-module `init_db()` calls. Adding a new database backend requires only a single file in `server/db/` exposing a `Backend` class.

---

## Authentication

Each MCP client sends its own iTop token as an HTTP bearer token:

```
Authorization: Bearer <itop_token>
```

The server validates that a non-empty token is present during the MCP `initialize` handshake — connections without one are rejected with 401 before the client sees any tool. The same token is forwarded to iTop as `auth_token` on every REST/JSON API call. No server-wide iTop credential is stored anywhere.

**Removed:** `ITOP_TOKEN`, `ITOP_USER`, `ITOP_PASSWORD` env vars. The transport changed from `stdio` to `streamable-http` since per-request HTTP headers are required for per-client tokens.

---

## Quick Start

### 1. Install

```bash
pip install -r requirements.txt
```

### 2. Configure

```bash
mkdir -p ~/.config/mcp-itop
cat > ~/.config/mcp-itop/.env << 'EOF'
ITOP_URL=https://your-itop.example.com
ITOP_VERSION=1.3
ITOP_VERIFY_SSL=true
ITOP_TIMEOUT=30
EOF
```

See `.env.example` for all available configuration options.

### 3. Run

```bash
cd server
python server.py
```

The server listens on `0.0.0.0:8096` by default (`streamable-http` transport).

---

## Docker

```bash
cp .env.example .env
# Edit .env: set ITOP_URL at minimum
docker compose up -d --build
```

The SQLite database is persisted in the `mcp-itop-data` Docker volume. See [DOCKER.md](DOCKER.md) for Portainer setup and debug logging instructions.

---

## Client Configuration

### opencode (global)

`~/.config/opencode/opencode.json`:

```json
{
  "mcpServers": {
    "itop": {
      "type": "remote",
      "url": "http://localhost:8096/mcp",
      "headers": {
        "Authorization": "Bearer your_itop_token"
      },
      "enabled": true
    }
  }
}
```

### opencode (per project)

`opencode.json` in the project root:

```json
{
  "mcpServers": {
    "itop": {
      "url": "http://localhost:8096/mcp",
      "headers": {
        "Authorization": "Bearer your_itop_token"
      }
    }
  }
}
```

### Claude Desktop

Configure a remote MCP connector pointing at `http://localhost:8096/mcp` with an `Authorization: Bearer <your_itop_token>` header.

---

## Environment Variables

| Variable | Default | Description |
|---|---|---|
| `ITOP_URL` | *(required)* | Base URL of the iTop instance |
| `ITOP_VERSION` | `1.3` | iTop REST API version |
| `ITOP_VERIFY_SSL` | `true` | Verify TLS certificates |
| `ITOP_TIMEOUT` | `30` | HTTP timeout in seconds |
| `MCP_HOST` | `0.0.0.0` | Server bind address |
| `MCP_PORT` | `8096` | Server bind port |
| `MCP_SERVER_URL` | | Public URL (required behind a reverse proxy) |
| `DB_BACKEND` | `sqlite` | Database backend module name |
| `SQLITE_DB_PATH` | `<server_root>/mcp_itop.db` | SQLite database file path |
| `SQLITE_VACUUM_INTERVAL` | `3600` | Seconds between incremental vacuum runs (0 = off) |
| `IMAGE_STORE_TTL_SECONDS` | `3600` | Image session cache TTL in seconds |
| `IMAGE_MAX_BYTES` | `1048576` | Max image size in bytes before compression/downscale |
| `IMAGE_JPEG_QUALITY` | `85` | Starting JPEG quality for normalisation (1-95) |
| `INLINE_IMAGE_REF_TTL` | `3600` | Inline image ref cache TTL in seconds |
| `RESOLVE_KEY_CACHE_TTL` | `86400` | resolve_key cache TTL in seconds (0 = off) |
| `CLEANUP_INTERVAL` | `300` | Housekeeping cycle interval in seconds |
| `MCP_DEBUG` | `false` | Log full request/response payloads (secrets redacted) |
| `MCP_DEBUG_HEADERS` | `false` | Also log HTTP headers (requires `MCP_DEBUG=true`) |

---

## Example Requests

```
Show me the SLA report for "Technical Support" this month
Which agents are overloaded?
Which tickets have been idle for more than 2 hours?
Find tickets where the service was changed after creation
Which users pick the wrong service most often?
Add a comment to ticket RQ-123
Create a new ticket: printer not working, caller John Doe
Assign RQ-456 to agent Smith
Resolve RQ-789 with solution "Replaced the cable"
Find CIs related to server srv-web-01
Search the KB for VPN setup
Get all image attachments for RQ-100
```

---

## Compatibility

- **iTop** 3.2.1-1-16749 (PHP 8.1.2, MariaDB 10.6)
- Supports both localised (yes/no) and English (true/false) SLA field values
- Auto-detects the KB module: `KBEntry` falls back to `FAQ`

## Requirements

```
mcp[fastmcp] >= 2.11.0
httpx
python-dotenv
Pillow
```

Full list in `requirements.txt`.

## Tests

```bash
python -m pytest tests/ -v
```
