# Docker / Portainer Deployment

The server runs natively over HTTP using the `streamable-http` MCP
transport, so it can be deployed directly as a network service without
any additional proxy. This Docker setup runs `server.py` directly and
exposes it on port `8096`, making it usable as a permanently running
Portainer stack.

## Included files

- `Dockerfile` - builds the image from `server.py` and `requirements.txt`
  and runs `python server.py` directly.
- `docker-compose.yml` - stack definition for Portainer/Docker Compose.
- `.env.example` - template for the required environment variables.
- `.dockerignore` - excludes unnecessary files from the build.

## Authentication

There is no server-wide iTop token configured in this container. Each
MCP client authenticates by sending its own iTop API token as a bearer
token in the `Authorization: Bearer <itop_token>` HTTP header when it
connects to the server.

The server validates that a non-empty bearer token is present during
the MCP handshake (`initialize`) - connections without one are rejected
with 401 before any tool is listed or called. That same token is then
forwarded to iTop as `auth_token` on every REST/JSON API call made on
behalf of that client.

## Setting up in Portainer

1. In Portainer, go to Stacks -> Add stack and create a new stack
   (e.g. via a Git repository reference to this repo).
2. Set the environment variables from `.env.example`, in particular:
   - `ITOP_URL` - base URL of the iTop instance
   - `ITOP_VERSION`, `ITOP_VERIFY_SSL`, `ITOP_TIMEOUT` optional, adjust as needed
   - `MCP_HOST`, `MCP_PORT` optional, bind address/port (default `0.0.0.0:8096`)
   - `MCP_DEBUG` optional, set to `true` for verbose request/response logging
3. Start the stack. Portainer builds the image automatically from the Dockerfile.
4. Once started, the MCP server is reachable at:
   `http://<docker-host>:8096/mcp`
5. Each client must be configured with its own iTop token as a bearer
   token in the `Authorization` header - see the main `README.md` for
   client configuration examples.

## Running locally with Docker Compose

```bash
cp .env.example .env
# fill in .env with real values (no iTop token needed here)
docker compose up -d --build
```

## Debug logging (MCP_DEBUG)

Set `MCP_DEBUG=true` (in `.env` or as a Portainer stack variable) to enable
verbose logging of:

- every MCP tool call/response between the client (Claude Desktop, opencode,
  MCP Inspector, etc.) and this server (via a FastMCP middleware in
  `server.py`)
- every iTop REST/JSON API request/response between `server.py` and iTop

Authentication secrets (bearer tokens, `auth_token`, `auth_pwd`) are always
redacted from log output, regardless of `MCP_DEBUG`. View logs with:

```bash
docker compose logs -f mcp-itop
```

## Note on credentials

The container itself holds no iTop credentials. Each client's bearer
token is presented per connection over HTTPS/HTTP and is never written
to the image, the container environment, or persisted logs. Terminate
TLS in front of this service (e.g. a reverse proxy) if it is reachable
outside a trusted network, since bearer tokens are sent as plain HTTP
headers otherwise.
