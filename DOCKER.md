# Docker / Portainer Deployment

The server communicates over **stdio**, so it cannot run directly as a
network service. This Docker setup uses `mcp-proxy` to spawn `server.py`
as a child process and additionally expose it over SSE/HTTP on port
`8096` - this makes the server usable as a permanently running
Portainer stack.

## Included files

- `Dockerfile` - builds the image from `server.py` and `requirements.txt`,
  installs dependencies including `mcp-proxy`, and uses `entrypoint.sh`
  as the container entrypoint.
- `entrypoint.sh` - starts `mcp-proxy` with `--pass-environment` (required
  so `ITOP_URL`/`ITOP_TOKEN`/etc. reach the `server.py` subprocess), and
  additionally adds `--debug` when `MCP_DEBUG=true`.
- `docker-compose.yml` - stack definition for Portainer/Docker Compose.
- `.env.example` - template for the required environment variables.
- `.dockerignore` - excludes unnecessary files from the build.

## Setting up in Portainer

1. In Portainer, go to Stacks -> Add stack and create a new stack
   (e.g. via a Git repository reference to this repo).
2. Set the environment variables from `.env.example`, in particular:
   - `ITOP_URL` - base URL of the iTop instance
   - `ITOP_TOKEN` - auth token (recommended) or `ITOP_USER` + `ITOP_PASSWORD`
   - `ITOP_VERSION`, `ITOP_VERIFY_SSL`, `ITOP_TIMEOUT` optional, adjust as needed
   - `MCP_DEBUG` optional, set to `true` for verbose request/response logging
3. Start the stack. Portainer builds the image automatically from the Dockerfile.
4. Once started, the MCP server is reachable at:
   `http://<docker-host>:8096/sse`

## Running locally with Docker Compose

```bash
cp .env.example .env
# fill in .env with real values
docker compose up -d --build
```

## Debug logging (MCP_DEBUG)

Set `MCP_DEBUG=true` (in `.env` or as a Portainer stack variable) to enable
verbose logging of:

- every MCP tool call/response between the client (Claude Desktop, opencode,
  MCP Inspector, etc.) and this server (via a FastMCP middleware in
  `server.py`)
- every iTop REST/JSON API request/response between `server.py` and iTop
- the client<->mcp-proxy SSE/HTTP traffic (via `mcp-proxy --debug`, enabled
  in `entrypoint.sh`)

Authentication secrets (`auth_token`, `auth_pwd`) are always redacted from
log output, regardless of `MCP_DEBUG`. View logs with:

```bash
docker compose logs -f mcp-itop
```

## Note on credentials

Credentials (token or username/password) are only ever set as environment
variables in the container and are not stored in the image. In Portainer,
they should be managed through the built-in environment variable management,
not stored in plain text in the stack file.
