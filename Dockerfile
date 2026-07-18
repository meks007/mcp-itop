FROM python:3.11-slim

WORKDIR /app

# Install native libs required by Pillow for full image format support:
#   libjpeg-dev      - JPEG
#   zlib1g-dev       - PNG compression
#   libwebp-dev      - WebP
#   libtiff-dev      - TIFF
#   libopenjp2-7-dev - JPEG 2000
#   libfreetype-dev  - font rendering (used by some Pillow features)
#   liblcms2-dev     - color management
#   libfribidi-dev   - BiDi text support
#   libharfbuzz-dev  - text shaping
#   libxcb1          - X11 (needed by some Pillow builds)
#   libgif-dev       - GIF (via giflib)
RUN apt-get update && apt-get install -y --no-install-recommends \
    libjpeg-dev \
    zlib1g-dev \
    libwebp-dev \
    libtiff-dev \
    libopenjp2-7-dev \
    libfreetype-dev \
    liblcms2-dev \
    libfribidi-dev \
    libharfbuzz-dev \
    libxcb1 \
    libgif-dev \
 && rm -rf /var/lib/apt/lists/*

# Install Python dependencies first for better layer caching.
# requirements.txt lives at the repo root, outside server/.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Copy all server source into /app/server and make it the working directory
# so that flat module imports (from auth import ..., etc.) resolve correctly.
COPY server/ ./server/

WORKDIR /app/server

RUN useradd -m -u 1000 mcp

RUN mkdir -p /app/data && chown mcp:mcp /app/data

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
