#!/bin/sh
# Wraps mcp-proxy so it can conditionally enable its own --debug flag,
# which logs the client <-> mcp-proxy SSE/HTTP traffic (in addition to
# the mcp <-> iTop logging done inside server.py when MCP_DEBUG=true).
set -e

ARGS="--port 8096 --host 0.0.0.0 --pass-environment"

if [ "$(printf '%s' "${MCP_DEBUG:-false}" | tr '[:upper:]' '[:lower:]')" = "true" ] \
   || [ "${MCP_DEBUG:-}" = "1" ]; then
    echo "[entrypoint] MCP_DEBUG enabled - starting mcp-proxy with --debug" >&2
    ARGS="$ARGS --debug"
fi

# shellcheck disable=SC2086
exec mcp-proxy $ARGS -- python server.py
