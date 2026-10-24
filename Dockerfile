FROM python:3.14-slim

WORKDIR /app

ENV PYTHONUNBUFFERED=1

# ca-certificates for httpx2 (OS trust store)
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml uv.lock README.md ./
COPY mcp_lexoffice/ mcp_lexoffice/

RUN pip install --no-cache-dir uv \
    && uv export --frozen --no-dev --no-emit-project -o /tmp/requirements.txt \
    && pip install --no-cache-dir --require-hashes -r /tmp/requirements.txt \
    && pip install --no-cache-dir --no-deps . \
    && rm /tmp/requirements.txt \
    && addgroup --system --gid 1000 mcp \
    && adduser --system --uid 1000 --ingroup mcp mcp

USER mcp

# Release version (the git tag); /health reports it. Unset → pyproject version.
ARG APP_VERSION=""
ENV APP_VERSION=$APP_VERSION

ENV MCP_TRANSPORT=streamable-http
ENV MCP_HOST=0.0.0.0
ENV MCP_PORT=8000

EXPOSE 8000

# Probe the address the server actually binds: same MCP_HOST/MCP_PORT (and defaults) as
# server.py main(). Wildcard binds (0.0.0.0, ::) are probed on loopback; a specific bind
# (e.g. the tailnet IP under host networking, where nothing listens on 127.0.0.1) is probed as-is.
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD python3 -c "import os,json,urllib.request as u; h=os.environ.get('MCP_HOST','0.0.0.0'); h='127.0.0.1' if h in ('','0.0.0.0','::') else h; h='['+h+']' if ':' in h else h; d=json.load(u.urlopen('http://%s:%s/health' % (h, os.environ.get('MCP_PORT','8000')), timeout=3)); raise SystemExit(d.get('status') != 'healthy')"

CMD ["mcp-lexoffice"]
