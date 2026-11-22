# Browser MCP

Exposes a headless Chromium browser as MCP tools for the [agent-framework](../agent-framework-service) harness. Built on FastMCP + Playwright with a streamable HTTP transport and bearer-token auth.

This service is a peer in the MCP Mesh alongside the filesystem, docker, git, and database servers. The harness mounts it directly as a second `MCPStreamableHTTPTool`; it does **not** go through LiteLLM and does **not** live in the chatbot frontend.

## Tools

- `browser_navigate` — navigate the shared page to a URL
- `browser_snapshot` — accessibility-tree snapshot (roles/names) for reading
- `browser_extract_text` — visible text content of the page (or a selector)
- `browser_click` — click an element by CSS selector or text
- `browser_type` — type text into a field (selector), optionally submit
- `browser_take_screenshot` — capture a PNG screenshot, returned as a path
- `browser_pdf` — save the page as a PDF, returned as a path
- `browser_back` / `browser_forward` / `browser_reload` — history navigation
- `browser_get_url` / `browser_get_title` — current location metadata
- `browser_list_tabs` / `browser_new_tab` / `browser_switch_tab` / `browser_close_tab` — tab management
- `browser_save_storage_state` / `browser_load_storage_state` — auth sessions

## Transport & auth

Streamable HTTP on loopback (default `127.0.0.1:8010/mcp`). All non-health requests require `Authorization: Bearer <MCP_BRIDGE_API_KEY>`. The key is loaded from `MCP_BRIDGE_API_KEY` or an env file at `MCP_BRIDGE_ENV_FILE`, matching the agent-framework-mcp bridge convention.

## Run

```bash
docker compose up -d --build
```

The compose file binds the port loopback-only (`127.0.0.1:8010:8010`) and reuses the shared `agent-framework-service_agent-framework-mcp-secrets` bearer-key volume.

## Layout

- `browser_mcp_server.py` — FastMCP server and tool definitions
- `Dockerfile` — Playwright Python base image, Chromium runtime
- `docker-compose.yml` — loopback-only service config, external secret volume
- `data/` — runtime artifacts and storage-state (git-ignored)
