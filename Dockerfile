# Browser MCP server: FastMCP + Playwright (headless Chromium), streamable HTTP.
#
# Uses the official Playwright Python image so the matching Chromium build and
# its system dependencies are preinstalled. The base image tag MUST match the
# `playwright` version pinned in requirements.txt — the browser binary is
# version-locked to the library (a mismatch fails at launch with "Executable
# doesn't exist").
FROM mcr.microsoft.com/playwright/python:v1.65.2-noble

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY browser_mcp_server.py ./

# Artifact + storage-state dirs are provided by the compose volume at /data.
# Runs as the host user via compose `user:` so /data is host-writable.

EXPOSE 8010

HEALTHCHECK --interval=30s --timeout=10s --retries=3 --start-period=20s \
  CMD python3 -c "import socket;s=socket.create_connection(('127.0.0.1',8010),timeout=5);s.close()"

CMD ["python", "browser_mcp_server.py"]
