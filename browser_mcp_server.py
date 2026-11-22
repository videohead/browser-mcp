"""Browser MCP server: FastMCP + Playwright, streamable HTTP, bearer-auth.

Exposes a Chromium browser as MCP tools for the agent-framework harness
("the brain"). This service is a peer in the MCP Mesh alongside the
filesystem/docker/git/database servers; the harness mounts it directly as a
second MCPStreamableHTTPTool — it does NOT go through LiteLLM and does NOT
live in the chatbot frontend.

Tools
-----
- browser_navigate      navigate the shared page to a URL
- browser_snapshot      accessibility-tree snapshot (roles/names) for reading
- browser_extract_text  visible text content of the page (or a selector)
- browser_click         click an element by CSS selector or text
- browser_type          type text into a field (selector), optionally submit
- browser_take_screenshot  capture a PNG screenshot, returned as a path
- browser_pdf           save the page as a PDF, returned as a path
- browser_back/forward/reload  history navigation
- browser_get_url/title current location metadata
- browser_list_tabs / browser_new_tab / browser_switch_tab / browser_close_tab
- browser_save_storage_state / browser_load_storage_state  auth sessions

Transport & auth
----------------
Streamable HTTP on loopback (default 127.0.0.1:8010/mcp). All non-health
requests require ``Authorization: Bearer <MCP_BRIDGE_API_KEY>``. The key is
loaded from MCP_BRIDGE_API_KEY or an env file at MCP_BRIDGE_ENV_FILE, matching
the agent-framework-mcp bridge convention.
"""

from __future__ import annotations

import asyncio
import hmac
import logging
import os
import time
from pathlib import Path

import uvicorn
from dotenv import dotenv_values
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.datastructures import Headers
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp, Receive, Scope, Send

from playwright.async_api import async_playwright, Browser, BrowserContext, Page

# --- Configuration ---------------------------------------------------------

# Bind 0.0.0.0 inside the container so Docker's published loopback port
# (127.0.0.1:8010 on the host) can reach it. External exposure is restricted to
# the host loopback by the compose port mapping, so this stays loopback-only
# in practice.
HOST = os.environ.get("BROWSER_MCP_HOST", "0.0.0.0")
PORT = int(os.environ.get("BROWSER_MCP_PORT", "8010"))
PUBLIC_PATHS = {"/health", "/ready"}

# Where screenshots / PDFs / storage state are written inside the container.
# Mount a host dir here so the harness (and the user) can retrieve artifacts.
ARTIFACT_DIR = Path(os.environ.get("BROWSER_ARTIFACT_DIR", "/data/artifacts"))
STORAGE_DIR = Path(os.environ.get("BROWSER_STORAGE_DIR", "/data/storage"))
ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
STORAGE_DIR.mkdir(parents=True, exist_ok=True)

# Headless Chromium. Keep the browser alive across calls; the harness is
# multi-turn, so the page/state must persist between tool invocations.
HEADLESS = os.environ.get("BROWSER_HEADLESS", "true").lower() != "false"
NAV_TIMEOUT_MS = int(os.environ.get("BROWSER_NAV_TIMEOUT_MS", "45000"))
DEFAULT_VIEWPORT = {"width": 1280, "height": 800}

logger = logging.getLogger("browser-mcp")
logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO").upper())


# --- Bearer auth (mirrors agent-framework mcp_http_bridge.py) ---------------

def validate_api_key(value: str | None) -> str:
    if value is None:
        raise RuntimeError("MCP_BRIDGE_API_KEY is missing")
    try:
        encoded = value.encode("ascii")
    except UnicodeEncodeError as exc:
        raise RuntimeError("MCP_BRIDGE_API_KEY must contain only printable ASCII") from exc
    if len(encoded) < 32 or any(byte < 33 or byte > 126 for byte in encoded):
        raise RuntimeError(
            "MCP_BRIDGE_API_KEY must be at least 32 printable ASCII characters without spaces"
        )
    return value


def load_api_key() -> str:
    env_file = os.environ.get("MCP_BRIDGE_ENV_FILE")
    env_key = os.environ.get("MCP_BRIDGE_API_KEY")
    if env_file and env_key is not None:
        raise RuntimeError("Set MCP_BRIDGE_ENV_FILE or MCP_BRIDGE_API_KEY, not both")
    if env_file:
        path = Path(env_file)
        if not path.is_file():
            raise RuntimeError(f"MCP bridge environment file does not exist: {path}")
        return validate_api_key(dotenv_values(path, interpolate=False).get("MCP_BRIDGE_API_KEY"))
    return validate_api_key(env_key)


class BearerAuthMiddleware:
    def __init__(self, app: ASGIApp, api_key: str) -> None:
        self.app = app
        self.api_key = validate_api_key(api_key).encode("ascii")

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        if scope["path"] in PUBLIC_PATHS and scope["method"] in {"GET", "HEAD"}:
            await self.app(scope, receive, send)
            return
        auth_headers = Headers(scope=scope).getlist("authorization")
        token = b""
        if len(auth_headers) == 1:
            scheme, sep, credentials = auth_headers[0].partition(" ")
            if sep and scheme.lower() == "bearer":
                try:
                    token = credentials.encode("ascii")
                except UnicodeEncodeError:
                    token = b""
        if not token or not hmac.compare_digest(token, self.api_key):
            response = JSONResponse({"detail": "Unauthorized"}, status_code=401)
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)


# --- Browser lifecycle ------------------------------------------------------

class BrowserManager:
    """Lazily launches one shared headless Chromium and hands out pages.

    A single persistent browser+context keeps cookies/localStorage alive across
    tool calls (authenticated sessions) and avoids paying Chromium startup cost
    on every call. Tabs are tracked so the model can keep multiple pages open.
    """

    def __init__(self) -> None:
        self._playwright = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self._pages: list[Page] = []
        self._active: int = 0
        self._lock = asyncio.Lock()

    async def ensure(self) -> BrowserContext:
        async with self._lock:
            if self._browser is None or not self._browser.is_connected():
                self._playwright = await async_playwright().start()
                self._browser = await self._playwright.chromium.launch(
                    headless=HEADLESS,
                    args=["--no-sandbox", "--disable-dev-shm-usage"],
                )
                self._context = await self._browser.new_context(
                    viewport=DEFAULT_VIEWPORT,
                    user_agent=(
                        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
                    ),
                )
                self._pages = []
                self._active = 0
                logger.info("browser.launched", extra={"headless": HEADLESS})
            assert self._context is not None
            return self._context

    async def page(self) -> Page:
        context = await self.ensure()
        if not self._pages:
            self._pages.append(await context.new_page())
            self._active = 0
        # Clamp index in case a tab was closed externally.
        self._active = max(0, min(self._active, len(self._pages) - 1))
        return self._pages[self._active]

    async def new_tab(self) -> int:
        context = await self.ensure()
        self._pages.append(await context.new_page())
        self._active = len(self._pages) - 1
        return self._active

    async def switch_tab(self, index: int) -> Page:
        if not (0 <= index < len(self._pages)):
            raise RuntimeError(f"tab index {index} out of range (0..{len(self._pages)-1})")
        self._active = index
        return self._pages[index]

    async def close_tab(self, index: int) -> None:
        if not (0 <= index < len(self._pages)):
            raise RuntimeError(f"tab index {index} out of range")
        await self._pages[index].close()
        del self._pages[index]
        self._active = max(0, min(self._active, len(self._pages) - 1))

    async def tabs(self) -> list[dict]:
        out = []
        for i, p in enumerate(self._pages):
            try:
                out.append({"index": i, "url": p.url, "title": await p.title(),
                            "active": i == self._active})
            except Exception:
                out.append({"index": i, "url": "<closed>", "title": "", "active": False})
        return out

    async def context(self) -> BrowserContext:
        return await self.ensure()


MANAGER = BrowserManager()

mcp = FastMCP(
    "browser-mcp",
    instructions=(
        "Headless Chromium browser for the agent-framework harness. Navigate, "
        "read pages (snapshot/extract_text), interact (click/type), capture "
        "screenshots/PDFs, and manage tabs + authenticated storage state."
    ),
)


def _err(exc: Exception) -> str:
    return f"browser error: {type(exc).__name__}: {exc}"


# --- Navigation / reading ---------------------------------------------------

@mcp.tool()
async def browser_navigate(url: str, wait_until: str = "load") -> str:
    """Navigate the active tab to a URL.

    Args:
        url: Absolute URL to visit (https://...).
        wait_until: load | domcontentloaded | networkidle (default load).
    Returns: final URL and page title.
    """
    try:
        page = await MANAGER.page()
        await page.goto(url, wait_until=wait_until, timeout=NAV_TIMEOUT_MS)
        return f"navigated to {page.url} — title: {await page.title()}"
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


@mcp.tool()
async def browser_snapshot(max_chars: int = 12000) -> str:
    """Return an accessibility-tree snapshot of the active page.

    Best first step after navigating: gives element roles/names you can feed
    to browser_click / browser_type. Truncated to max_chars.
    """
    try:
        page = await MANAGER.page()
        snap = await page.accessibility.snapshot()
        import json
        text = json.dumps(snap, indent=2) if snap else "(empty page)"
        return text[:max_chars]
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


@mcp.tool()
async def browser_extract_text(selector: str = "body", max_chars: int = 20000) -> str:
    """Return visible inner text of a CSS selector (default whole page body)."""
    try:
        page = await MANAGER.page()
        el = await page.query_selector(selector)
        if el is None:
            return f"no element matches selector: {selector}"
        text = await el.inner_text()
        return text[:max_chars]
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


@mcp.tool()
async def browser_get_url() -> str:
    """Return the active tab's current URL."""
    try:
        return (await MANAGER.page()).url
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


@mcp.tool()
async def browser_get_title() -> str:
    """Return the active tab's document title."""
    try:
        return await (await MANAGER.page()).title()
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


# --- Interaction ------------------------------------------------------------

@mcp.tool()
async def browser_click(selector: str = "", text: str = "") -> str:
    """Click an element by CSS selector or visible text.

    Provide either selector (CSS) or text (exact visible-text match).
    Returns the resulting URL/title after the click settles.
    """
    try:
        page = await MANAGER.page()
        if selector:
            await page.click(selector, timeout=NAV_TIMEOUT_MS)
        elif text:
            await page.get_by_text(text, exact=True).first.click(timeout=NAV_TIMEOUT_MS)
        else:
            return "provide either selector or text"
        await page.wait_for_load_state("load", timeout=NAV_TIMEOUT_MS)
        return f"clicked; now at {page.url} — title: {await page.title()}"
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


@mcp.tool()
async def browser_type(selector: str, text: str, submit: bool = False) -> str:
    """Type text into an input matched by a CSS selector.

    Args:
        selector: CSS selector of the input/textarea.
        text: text to type.
        submit: press Enter after typing (e.g. submit a search).
    """
    try:
        page = await MANAGER.page()
        await page.fill(selector, text, timeout=NAV_TIMEOUT_MS)
        if submit:
            await page.press(selector, "Enter")
            await page.wait_for_load_state("load", timeout=NAV_TIMEOUT_MS)
        return f"typed {len(text)} chars into {selector}; url {page.url}"
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


# --- Capture ----------------------------------------------------------------

@mcp.tool()
async def browser_take_screenshot(full_page: bool = False, name: str = "") -> str:
    """Capture a PNG screenshot of the active tab.

    Returns the absolute path (under the artifact dir) of the saved PNG.
    """
    try:
        page = await MANAGER.page()
        fname = name or f"shot-{int(time.time())}.png"
        if not fname.endswith(".png"):
            fname += ".png"
        path = ARTIFACT_DIR / fname
        await page.screenshot(path=str(path), full_page=full_page)
        return f"screenshot saved: {path}"
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


@mcp.tool()
async def browser_pdf(name: str = "") -> str:
    """Save the active page as a PDF. Returns the saved file path.

    (Chromium print-to-PDF; works headless.)
    """
    try:
        page = await MANAGER.page()
        fname = name or f"page-{int(time.time())}.pdf"
        if not fname.endswith(".pdf"):
            fname += ".pdf"
        path = ARTIFACT_DIR / fname
        await page.pdf(path=str(path))
        return f"pdf saved: {path}"
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


# --- History / tabs ---------------------------------------------------------

@mcp.tool()
async def browser_back() -> str:
    """Go back in history. Returns the resulting URL."""
    try:
        page = await MANAGER.page()
        await page.go_back(wait_until="load", timeout=NAV_TIMEOUT_MS)
        return f"back to {page.url}"
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


@mcp.tool()
async def browser_forward() -> str:
    """Go forward in history. Returns the resulting URL."""
    try:
        page = await MANAGER.page()
        await page.go_forward(wait_until="load", timeout=NAV_TIMEOUT_MS)
        return f"forward to {page.url}"
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


@mcp.tool()
async def browser_reload() -> str:
    """Reload the active tab. Returns the URL/title."""
    try:
        page = await MANAGER.page()
        await page.reload(wait_until="load", timeout=NAV_TIMEOUT_MS)
        return f"reloaded {page.url} — title: {await page.title()}"
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


@mcp.tool()
async def browser_new_tab(url: str = "") -> str:
    """Open a new tab (optionally navigating to url). Returns its index."""
    try:
        idx = await MANAGER.new_tab()
        if url:
            page = await MANAGER.page()
            await page.goto(url, wait_until="load", timeout=NAV_TIMEOUT_MS)
        return f"opened tab index {idx}"
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


@mcp.tool()
async def browser_switch_tab(index: int) -> str:
    """Switch the active tab to the given index."""
    try:
        page = await MANAGER.switch_tab(index)
        return f"active tab {index}: {page.url}"
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


@mcp.tool()
async def browser_close_tab(index: int) -> str:
    """Close the tab at the given index."""
    try:
        await MANAGER.close_tab(index)
        return f"closed tab {index}"
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


@mcp.tool()
async def browser_list_tabs() -> str:
    """List open tabs (index, url, title, active)."""
    try:
        import json
        return json.dumps(await MANAGER.tabs(), indent=2)
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


# --- Authenticated sessions -------------------------------------------------

@mcp.tool()
async def browser_save_storage_state(name: str = "default") -> str:
    """Persist cookies/localStorage of the current context to a named file.

    Use after a login flow so later runs can resume the authenticated session.
    """
    try:
        context = await MANAGER.context()
        path = STORAGE_DIR / f"{name}.json"
        await context.storage_state(path=str(path))
        return f"storage state saved: {path}"
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


@mcp.tool()
async def browser_load_storage_state(name: str = "default") -> str:
    """Replace the current context with one built from a saved storage state.

    Relaunches the browser context with the named cookies/localStorage so an
    authenticated session is restored. Open tabs are reset.
    """
    try:
        path = STORAGE_DIR / f"{name}.json"
        if not path.is_file():
            return f"no saved storage state named '{name}'"
        async with MANAGER._lock:  # rebuild context under lock
            if MANAGER._context is not None:
                await MANAGER._context.close()
            assert MANAGER._browser is not None
            MANAGER._context = await MANAGER._browser.new_context(
                viewport=DEFAULT_VIEWPORT, storage_state=str(path)
            )
            MANAGER._pages = []
            MANAGER._active = 0
        return f"loaded storage state '{name}'; tabs reset"
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


# --- App assembly -----------------------------------------------------------

def build_app() -> ASGIApp:
    """Assemble the ASGI app: FastMCP streamable HTTP + health + bearer auth."""
    api_key = load_api_key()
    # FastMCP streamable-http sub-app mounted at root; FastMCP exposes /mcp.
    mcp_app = mcp.streamable_http_app()

    async def health(scope: Scope, receive: Receive, send: Send) -> None:
        response = JSONResponse({"status": "ok", "service": "browser-mcp"})
        await response(scope, receive, send)

    async def router(scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and scope["path"] in PUBLIC_PATHS:
            await health(scope, receive, send)
            return
        await mcp_app(scope, receive, send)

    secured = BearerAuthMiddleware(router, api_key)
    return secured


if __name__ == "__main__":
    uvicorn.run(
        build_app(),
        host=HOST,
        port=PORT,
        log_level=os.environ.get("LOG_LEVEL", "INFO").lower(),
    )
