FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt ./
COPY server.py config.py auth.py client.py helpers.py oauth_config.py token_store.py ./
COPY tools/ ./tools/

RUN pip install --no-cache-dir -r requirements.txt

RUN useradd -m -u 1000 mcp
USER mcp

EXPOSE 8096

# server.py runs natively as a Streamable HTTP MCP server (transport=
# "streamable-http"), listening on MCP_HOST:MCP_PORT (default
# 0.0.0.0:8096). No proxy process is needed: each MCP client connects
# directly and authenticates with an OIDC JWT issued by the provider
# configured in oauth_config.yaml.
#
# Mount oauth_config.yaml and token_store.yaml at runtime:
#   docker run -v /host/path/oauth_config.yaml:/app/oauth_config.yaml \
#              -v /host/path/token_store.yaml:/app/token_store.yaml \
#              mcp-itop
ENTRYPOINT ["python", "server.py"]
