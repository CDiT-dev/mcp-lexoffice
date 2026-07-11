FROM python:3.12-slim

WORKDIR /app

ENV PYTHONUNBUFFERED=1

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

ENV MCP_TRANSPORT=streamable-http
ENV MCP_HOST=0.0.0.0
ENV MCP_PORT=8000

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD python3 -c "import urllib.request,json,sys; r=urllib.request.urlopen('http://localhost:8000/health',timeout=3); d=json.loads(r.read()); sys.exit(0 if d.get('status')=='healthy' else 1)"

CMD ["mcp-lexoffice"]
