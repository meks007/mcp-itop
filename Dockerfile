FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt server.py ./

RUN pip install --no-cache-dir -r requirements.txt mcp-proxy

RUN useradd -m -u 1000 mcp
USER mcp

EXPOSE 8096

# mcp-proxy spawns `python server.py` as a child process and exposes
# it over SSE at http://<host>:8096/sse, so the stdio-only MCP server
# can run as a standalone, network-reachable service (e.g. in Portainer).
CMD ["mcp-proxy", "--port", "8096", "--host", "0.0.0.0", "--", "python", "server.py"]
