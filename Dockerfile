FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt server.py ./

RUN pip install --no-cache-dir -r requirements.txt

RUN useradd -m -u 1000 mcp
USER mcp

EXPOSE 8096

# server.py runs natively as a Streamable HTTP MCP server (transport=
# "streamable-http"), listening on MCP_HOST:MCP_PORT (default
# 0.0.0.0:8096). No proxy process is needed: each MCP client connects
# directly and authenticates by sending its own iTop API token as an
# "Authorization: Bearer <itop_token>" HTTP header.
ENTRYPOINT ["python", "server.py"]
