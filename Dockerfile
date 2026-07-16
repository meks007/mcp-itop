FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt ./
COPY server.py config.py auth.py client.py helpers.py attachment_store.py ./
COPY tools/ ./tools/

RUN pip install --no-cache-dir -r requirements.txt

RUN useradd -m -u 1000 mcp \
 && mkdir -p /app/data \
 && chown mcp:mcp /app/data

USER mcp

EXPOSE 8096

# server.py runs as a Streamable HTTP MCP server via uvicorn, listening on
# MCP_HOST:MCP_PORT (default 0.0.0.0:8096). Each MCP client authenticates
# by sending its own iTop API token as an "Authorization: Bearer <itop_token>"
# HTTP header. The token is forwarded to iTop on every REST call.
#
# IMAGE_STORE_DB controls the SQLite path for the attachment image store.
# Default: /app/data/attachment_store.db (writable by the mcp user).
# Override via environment variable or mount a volume onto /app/data.
ENV IMAGE_STORE_DB=/app/data/attachment_store.db

ENTRYPOINT ["python", "server.py"]
