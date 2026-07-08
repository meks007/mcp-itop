FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt server.py entrypoint.sh ./

RUN pip install --no-cache-dir -r requirements.txt mcp-proxy \
    && chmod +x /app/entrypoint.sh

RUN useradd -m -u 1000 mcp
USER mcp

EXPOSE 8096

# mcp-proxy spawns `python server.py` as a child process and exposes
# it over SSE at http://<host>:8096/sse, so the stdio-only MCP server
# can run as a standalone, network-reachable service (e.g. in Portainer).
#
# entrypoint.sh always sets --pass-environment (required so ITOP_URL /
# ITOP_TOKEN / etc. reach the server.py subprocess) and additionally
# enables mcp-proxy's own --debug flag when MCP_DEBUG=true, logging the
# client<->mcp-proxy SSE/HTTP traffic as well.
ENTRYPOINT ["/app/entrypoint.sh"]
