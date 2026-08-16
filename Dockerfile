FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV NCS_DB_PATH=/data/ncs.db
ENV NCS_REPORTS_DIR=/data/reports
ENV NCS_MCP_HOST=0.0.0.0
ENV NCS_MCP_PORT=8766

WORKDIR /app

RUN addgroup --system app && adduser --system --ingroup app app

COPY pyproject.toml requirements.txt ./
COPY src ./src

RUN python -m pip install --no-cache-dir --upgrade pip \
    && python -m pip install --no-cache-dir .

RUN mkdir -p /data/reports && chown -R app:app /app /data

USER app

EXPOSE 8766

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import os, urllib.request; port=os.environ.get('NCS_MCP_PORT','8766'); urllib.request.urlopen(f'http://127.0.0.1:{port}/ready', timeout=5).read()" || exit 1

CMD ["sh", "-c", "python -m ncs_mcp.server --transport streamable-http --host \"${NCS_MCP_HOST}\" --port \"${NCS_MCP_PORT}\""]
