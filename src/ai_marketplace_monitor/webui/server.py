"""FastAPI app factory and uvicorn-in-a-thread runner.

The monitor process stays fully synchronous. Uvicorn runs on its own
asyncio loop in a daemon thread; the LogBroadcastHandler bridges records
from the main thread to that loop via ``loop.call_soon_threadsafe``.
"""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import logging
import mimetypes
import os
import re
import secrets
import socket
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List

import uvicorn
from fastapi import (
    Cookie,
    Depends,
    FastAPI,
    Form,
    HTTPException,
    Request,
    Response,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from ..utils import CacheType, amm_home, browser_state_file, cache
from .activity import build_activity
from .auth import (
    CSRF_COOKIE,
    CSRF_HEADER,
    SESSION_COOKIE,
    SESSION_TTL,
    AuthConfig,
    RateLimiter,
    SessionManager,
    hash_password,
    verify_password,
)
from .config_api import ConfigFileService
from .config_auth import extract_credentials
from .found_export import iter_found_csv, iter_found_rows
from .log_handler import LogBroadcastHandler

# Ensure the vendored toml-edit-js WASM bundle is served with the right
# Content-Type. Python's mimetypes module learned .wasm in 3.10 but
# explicit registration is safer across patch versions.
mimetypes.add_type("application/wasm", ".wasm")

STATIC_DIR = Path(__file__).parent / "static"


@dataclass
class WebUIConfig:
    host: str = "127.0.0.1"
    port: int = 8467
    config_files: List[Path] = field(default_factory=list)
    log_handler: LogBroadcastHandler | None = None
    # The running MarketplaceMonitor, when the web UI is embedded in the
    # monitor process. Enables pause/resume and live schedule reporting;
    # None in tests that start the web UI alone.
    monitor: Any = None


@dataclass
class StartupInfo:
    """Information about the running server, shown in the startup banner."""

    urls: List[str]
    username: str | None  # None in open mode
    host: str
    port: int
    exposed: bool


class AuthState:
    """Mutable auth state.

    On loopback (default) the web UI is always open — no password
    required.  When ``--webui-host`` exposes the server on a
    non-loopback interface, ``auth`` must be set (credentials from
    a marketplace config section or environment variables).
    """

    def __init__(self) -> None:
        self.auth: AuthConfig | None = None
        self.exposed: bool = False


def _resolve_auth(config: WebUIConfig) -> tuple[AuthState, StartupInfo]:
    """Build initial AuthState from config files and environment.

    On loopback the UI is always open.  When exposed (--webui-host),
    credentials are required — checked from ``[marketplace.*]`` config
    sections, then ``FACEBOOK_USERNAME`` / ``FACEBOOK_PASSWORD`` env
    vars.
    """
    exposed = config.host not in ("127.0.0.1", "localhost", "::1")
    state = AuthState()
    state.exposed = exposed

    if exposed:
        extracted = extract_credentials(config.config_files)
        if extracted.username and extracted.password:
            state.auth = AuthConfig(
                username=extracted.username,
                password_hash=hash_password(extracted.password),
                secret_key=secrets.token_urlsafe(32),
            )
        # If exposed with no credentials, start_webui() will reject this.

    info = StartupInfo(
        urls=_enumerate_urls(config.host, config.port),
        username=state.auth.username if state.auth else None,
        host=config.host,
        port=config.port,
        exposed=exposed,
    )
    return state, info


def _set_session_cookies(response: Response, token: str, csrf: str) -> None:
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=SESSION_TTL,
        httponly=True,
        samesite="strict",
    )
    response.set_cookie(
        CSRF_COOKIE,
        csrf,
        max_age=SESSION_TTL,
        httponly=False,  # JS reads this to echo via header
        samesite="strict",
    )


def _enumerate_urls(host: str, port: int) -> List[str]:
    if host in ("127.0.0.1", "localhost", "::1"):
        return [f"http://127.0.0.1:{port}"]
    if host in ("0.0.0.0", "::"):  # noqa: S104 — intentional bind-all
        # Enumerate local interface addresses so the user sees every reachable URL.
        urls = [f"http://127.0.0.1:{port}"]
        try:
            hostname = socket.gethostname()
            for info in socket.getaddrinfo(hostname, None):
                addr = str(info[4][0])
                if addr and addr not in ("127.0.0.1", "::1"):
                    if ":" in addr:
                        urls.append(f"http://[{addr}]:{port}")
                    else:
                        urls.append(f"http://{addr}:{port}")
        except socket.gaierror:
            pass
        # De-duplicate preserving order.
        seen: set[str] = set()
        unique: List[str] = []
        for url in urls:
            if url not in seen:
                seen.add(url)
                unique.append(url)
        return unique
    return [f"http://{host}:{port}"]


def _supported_marketplace_names() -> List[str]:
    # Imported lazily: config.py imports the marketplace modules, and this
    # module is imported by cli.py before the monitor exists.
    from ..config import supported_marketplaces

    return sorted(supported_marketplaces)


def create_app(
    config: WebUIConfig,
    state: AuthState,
    config_service: ConfigFileService,
    log_handler: LogBroadcastHandler,
) -> FastAPI:
    app = FastAPI(
        title="AI Marketplace Monitor",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    process_secret = secrets.token_urlsafe(32)
    sessions = SessionManager(process_secret)
    rate_limiter = RateLimiter()

    # ------------------------------------------------------------------
    # Reverse-proxy authentication (Traefik forward-auth and kin)
    #
    # When the UI sits behind an authenticating proxy, the proxy has already
    # verified the user (e.g. Google SSO) and asserts the identity in a header.
    # Trusting that header lets the web UI run with NO credentials of its own,
    # so marketplace passwords never have to double as the UI login.
    #
    # The header is only believed when the TCP peer is inside
    # AIMM_TRUSTED_PROXY_IPS — anyone else can type the header but arrives
    # from an untrusted address and falls through to normal auth. Note the
    # residual trust: other workloads on the trusted network segment could
    # assert the header too, so the CIDR should be as narrow as practical.
    # ------------------------------------------------------------------
    proxy_auth_enabled = os.environ.get("AIMM_PROXY_AUTH") == "1"
    proxy_header = os.environ.get("AIMM_PROXY_AUTH_HEADER", "x-forwarded-user").lower()
    trusted_nets = []
    for net_text in os.environ.get("AIMM_TRUSTED_PROXY_IPS", "").split(","):
        net_text = net_text.strip()
        if not net_text:
            continue
        try:
            trusted_nets.append(ipaddress.ip_network(net_text, strict=False))
        except ValueError:
            logging.getLogger(__name__).warning(
                "Ignoring invalid AIMM_TRUSTED_PROXY_IPS entry %r", net_text
            )

    def proxy_user(request: Request) -> str | None:
        if not (proxy_auth_enabled and trusted_nets and request.client):
            return None
        try:
            addr = ipaddress.ip_address(request.client.host)
        except ValueError:
            return None
        if not any(addr in net for net in trusted_nets):
            return None
        value = request.headers.get(proxy_header, "").strip()
        return value or None

    @app.middleware("http")
    async def _proxy_session_middleware(request: Request, call_next: Any) -> Any:
        """Mint session + CSRF cookies for proxy-authenticated visitors.

        The SPA's POSTs echo the CSRF cookie in a header, so a proxy-authed
        browser needs the same cookies a password login would set. CSRF checks
        stay mandatory: the SSO cookie rides along on cross-site requests, so
        proxy auth alone must never authorize a state change.
        """
        response = await call_next(request)
        if SESSION_COOKIE not in request.cookies:
            user = proxy_user(request)
            if user:
                token, csrf = sessions.issue(user)
                _set_session_cookies(response, token, csrf)
        return response

    def is_open() -> bool:
        """True when running on loopback — no password required."""
        return not state.exposed

    def require_session(
        request: Request,
        session: str | None = Cookie(default=None, alias=SESSION_COOKIE),
    ) -> str:
        if is_open():
            return "anonymous"
        if session is not None:
            username = sessions.validate(session)
            if username is not None:
                return username
        forwarded = proxy_user(request)
        if forwarded is not None:
            return forwarded
        raise HTTPException(
            status_code=401,
            detail="Not authenticated" if session is None else "Session expired",
        )

    def require_csrf(
        request: Request,
        csrf_cookie: str | None = Cookie(default=None, alias=CSRF_COOKIE),
    ) -> None:
        if is_open():
            return  # open mode skips CSRF (nothing to protect)
        header = request.headers.get(CSRF_HEADER)
        if not header or not csrf_cookie or not secrets.compare_digest(header, csrf_cookie):
            raise HTTPException(status_code=403, detail="CSRF token mismatch")

    # ------------------------------------------------------------------
    # Routes
    # ------------------------------------------------------------------

    @app.get("/api/auth/info")
    async def auth_info() -> Dict[str, Any]:
        """Return auth mode info for the frontend login screen."""
        return {
            "open": is_open(),
            "username_hint": state.auth.username if state.auth else None,
            "proxy_auth": proxy_auth_enabled,
            "password_login": state.auth is not None,
        }

    @app.post("/api/login")
    async def login(
        request: Request,
        response: Response,
        username: str = Form(""),
        password: str = Form(""),
    ) -> Dict[str, Any]:
        # Loopback — always open, no password needed.
        if is_open():
            token, csrf = sessions.issue("anonymous")
            _set_session_cookies(response, token, csrf)
            return {"username": "anonymous", "csrf": csrf}

        # Exposed — credentials required.
        client_ip = request.client.host if request.client else "unknown"
        if rate_limiter.is_locked(client_ip):
            raise HTTPException(status_code=429, detail="Too many failed attempts")

        assert state.auth is not None  # enforced by start_webui()
        if username != state.auth.username or not verify_password(
            password, state.auth.password_hash
        ):
            rate_limiter.record_failure(client_ip)
            raise HTTPException(status_code=401, detail="Invalid credentials")

        rate_limiter.reset(client_ip)
        token, csrf = sessions.issue(username)
        _set_session_cookies(response, token, csrf)
        return {"username": username, "csrf": csrf}

    @app.post("/api/logout")
    async def logout(response: Response) -> Dict[str, Any]:
        response.delete_cookie(SESSION_COOKIE)
        response.delete_cookie(CSRF_COOKIE)
        return {"ok": True}

    @app.get("/api/status")
    async def status(_: str = Depends(require_session)) -> Dict[str, Any]:
        files = config_service.list_files()
        return {
            "config_files": [f.__dict__ for f in files],
            "urls": _enumerate_urls(config.host, config.port),
            "auth_mode": "open" if is_open() else "authenticated",
            "open": is_open(),
            "vnc_enabled": os.environ.get("AIMM_ENABLE_VNC") == "1"
            and Path(os.environ.get("AIMM_NOVNC_DIR", "/usr/share/novnc")).is_dir(),
            # Every marketplace type the backend can drive, so the config UI
            # can offer un-configured ones as "Set up" cards instead of hiding
            # them behind the Add-section dropdown.
            "marketplaces": _supported_marketplace_names(),
        }

    @app.get("/api/config/files")
    async def list_config_files(_: str = Depends(require_session)) -> Dict[str, Any]:
        return {"files": [f.__dict__ for f in config_service.list_files()]}

    @app.get("/api/config/file/{file_id}")
    async def get_config_file(file_id: str, _: str = Depends(require_session)) -> Dict[str, Any]:
        try:
            content, mtime = config_service.read(file_id)
        except KeyError as e:
            raise HTTPException(status_code=404, detail=str(e)) from None
        from .config_api import scan_sections
        from .secrets_redact import MASK, has_mask

        sections = [
            {
                "name": s.name,
                "prefix": s.prefix,
                "suffix": s.suffix,
                "line_start": s.line_start,
                "line_end": s.line_end,
                "fields": s.fields,
            }
            for s in scan_sections(content)
        ]
        return {
            "content": content,
            "mtime": mtime,
            "has_masked_secrets": has_mask(content),
            "mask_token": MASK,
            "sections": sections,
        }

    @app.put("/api/config/file/{file_id}", response_model=None)
    async def put_config_file(
        file_id: str,
        body: Dict[str, Any],
        _: str = Depends(require_session),
        __: None = Depends(require_csrf),
    ) -> Dict[str, Any]:
        content = body.get("content")
        if not isinstance(content, str):
            raise HTTPException(status_code=400, detail="Missing 'content' field")
        base_mtime = body.get("base_mtime")
        try:
            new_mtime, ok, error = config_service.write(
                file_id, content, base_mtime if isinstance(base_mtime, (int, float)) else None
            )
        except KeyError as e:
            raise HTTPException(status_code=404, detail=str(e)) from None
        if not ok:
            status_code = 409 if error and "conflict" in error else 400
            return JSONResponse(  # type: ignore[return-value]
                status_code=status_code,
                content={"ok": False, "error": error, "mtime": new_mtime},
            )
        return {"ok": True, "mtime": new_mtime}

    @app.post("/api/config/validate")
    async def validate_config(
        body: Dict[str, Any],
        _: str = Depends(require_session),
        __: None = Depends(require_csrf),
    ) -> Dict[str, Any]:
        content = body.get("content")
        if not isinstance(content, str):
            raise HTTPException(status_code=400, detail="Missing 'content' field")
        ok, error = config_service.validate(content)
        return {"valid": ok, "error": error}

    @app.post("/api/monitor/restart")
    async def restart_monitor(
        _: str = Depends(require_session),
        __: None = Depends(require_csrf),
    ) -> Dict[str, Any]:
        """Wake the monitor by touching the config file.

        The file watcher interrupts the monitor's doze() sleep, causing
        it to reload the config and run all scheduled searches immediately.
        """
        try:
            path = config_service.editable_path
            path.touch()
            return {"ok": True, "message": "Monitor woken — searching all items now."}
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Failed to touch config: {e}") from e

    @app.get("/api/logs")
    async def get_logs(
        limit: int = 500,
        level: str = "DEBUG",
        kind: str | None = None,
        item: str | None = None,
        min_score: int | None = None,
        _: str = Depends(require_session),
    ) -> Dict[str, Any]:
        level_value = logging.getLevelName(level.upper())
        if not isinstance(level_value, int):
            level_value = 0
        return {
            "records": log_handler.snapshot(
                limit=limit,
                min_level=level_value,
                kind=kind,
                item=item,
                min_score=min_score,
            ),
            "capacity": log_handler._buffer.maxlen,
        }

    @app.websocket("/ws/stream")
    async def ws_stream(websocket: WebSocket) -> None:
        # In open mode (loopback) skip cookie check; otherwise require
        # a valid session cookie on the WebSocket handshake.
        if not is_open():
            session = websocket.cookies.get(SESSION_COOKIE)
            if not session or sessions.validate(session) is None:
                await websocket.close(code=4401)
                return

        await websocket.accept()
        queue: asyncio.Queue[Dict[str, Any]] = asyncio.Queue(maxsize=1000)
        log_handler.subscribe(queue)
        try:
            # Send a brief hello so clients know the stream is live.
            await websocket.send_json({"type": "hello", "time": time.time()})
            while True:
                payload = await queue.get()
                await websocket.send_json({"type": "log", "record": payload})
        except WebSocketDisconnect:
            pass
        except Exception:  # noqa: S110 — client disconnected; nothing to handle
            pass
        finally:
            log_handler.unsubscribe(queue)

    # ------------------------------------------------------------------
    # Optional noVNC bridge (Docker deployments)
    # ------------------------------------------------------------------
    novnc_dir = os.environ.get("AIMM_NOVNC_DIR", "/usr/share/novnc")
    vnc_host = os.environ.get("AIMM_VNC_HOST", "127.0.0.1")
    vnc_port = int(os.environ.get("AIMM_VNC_PORT", "5900"))
    if os.environ.get("AIMM_ENABLE_VNC") == "1" and Path(novnc_dir).is_dir():
        app.mount("/vnc", StaticFiles(directory=novnc_dir, html=True), name="vnc")

        @app.websocket("/ws/vnc")
        async def ws_vnc(websocket: WebSocket) -> None:
            if not is_open():
                session = websocket.cookies.get(SESSION_COOKIE)
                if not session or sessions.validate(session) is None:
                    await websocket.close(code=4401)
                    return
            # No subprotocol. noVNC >= 1.2 calls `new WebSocket(url, [])` and so
            # offers none; RFC 6455 requires a client to fail the connection if
            # the server answers with one it did not offer. Replying "binary"
            # here made Chrome drop every handshake -- the browser view showed
            # "Failed to connect to server" no matter what. Debian bookworm,
            # which the Dockerfile installs novnc from, ships 1.3.0.
            await websocket.accept()
            try:
                reader, writer = await asyncio.open_connection(vnc_host, vnc_port)
            except OSError:
                await websocket.close(code=1011)
                return

            async def ws_to_tcp() -> None:
                try:
                    while True:
                        data = await websocket.receive_bytes()
                        writer.write(data)
                        await writer.drain()
                except WebSocketDisconnect:
                    pass
                finally:
                    writer.close()

            async def tcp_to_ws() -> None:
                try:
                    while True:
                        chunk = await reader.read(65536)
                        if not chunk:
                            break
                        await websocket.send_bytes(chunk)
                finally:
                    try:
                        await websocket.close()
                    except Exception:  # noqa: S110 — already closed
                        pass

            await asyncio.gather(ws_to_tcp(), tcp_to_ws(), return_exceptions=True)

    # ------------------------------------------------------------------
    # Static UI
    # ------------------------------------------------------------------
    if STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

        @app.get("/")
        async def index() -> FileResponse:
            # FileResponse sends no Cache-Control here, and "/" carries no
            # validator a browser could revalidate against, so a cached copy of
            # this shell can outlive an upgrade indefinitely -- the user gets
            # new /static assets stapled to old markup, and any element added
            # in a release is simply absent. no-cache still allows a 304 via
            # ETag; it only forbids using the copy without asking first.
            return FileResponse(
                STATIC_DIR / "index.html",
                headers={"Cache-Control": "no-cache, must-revalidate"},
            )

    # Sync def (not async): FastAPI runs it in a threadpool and Starlette
    # iterates the sync generator there too, so the blocking cache scan never
    # runs on the event loop. The body streams row-by-row rather than buffering
    # the whole CSV, keeping memory bounded for large exports.
    @app.get("/api/found.csv")
    def export_found_csv(_: str = Depends(require_session)) -> StreamingResponse:
        filename = f"found-items-{time.strftime('%Y%m%d-%H%M%S')}.csv"
        return StreamingResponse(
            iter_found_csv(iter_found_rows(cache)),
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    def _fb_session_summary() -> Dict[str, Any]:
        """What the saved browser state says about the Facebook login.

        c_user/xs are the cookies Facebook issues to a signed-in session;
        their absence with a file present means the state captured an
        anonymous browser -- worth distinguishing, because that exact case
        looked like success once and was not.
        """
        out: Dict[str, Any] = {"exists": False, "logged_in": False, "saved_at": None}
        try:
            if browser_state_file.exists():
                out["exists"] = True
                out["saved_at"] = browser_state_file.stat().st_mtime
                cookie_names = {
                    c.get("name")
                    for c in json.loads(browser_state_file.read_text()).get("cookies", [])
                }
                out["logged_in"] = {"c_user", "xs"} <= cookie_names
        except KeyboardInterrupt:
            raise
        except Exception:  # noqa: S110 — a malformed state file reads as absent
            pass
        return out

    # Sync def: the counters live in the diskcache, and the scan must stay off
    # the event loop.
    @app.get("/api/monitor/state")
    def monitor_state(_: str = Depends(require_session)) -> Dict[str, Any]:
        out: Dict[str, Any] = {"available": config.monitor is not None}
        if config.monitor is not None:
            try:
                out.update(config.monitor.monitor_state())
            except KeyboardInterrupt:
                raise
            except Exception as e:
                out["error"] = str(e)
        out["fb_session"] = _fb_session_summary()
        counters: Dict[str, Dict[str, int]] = {}
        try:
            for key in cache.iterkeys():
                if isinstance(key, tuple) and len(key) >= 3 and key[0] == CacheType.COUNTERS.value:
                    value = cache.get(key)
                    if isinstance(value, int):
                        counters.setdefault(key[1], {})[key[2]] = value
        except KeyboardInterrupt:
            raise
        except Exception:  # noqa: S110 — partial counters beat a failed page
            pass
        out["counters"] = counters
        return out

    @app.post("/api/monitor/pause")
    async def pause_monitor(
        user: str = Depends(require_session),
        __: None = Depends(require_csrf),
    ) -> Dict[str, Any]:
        if config.monitor is None:
            raise HTTPException(status_code=503, detail="Monitor not attached to web UI.")
        config.monitor.web_paused.set()
        return {"ok": True, "paused": True}

    @app.post("/api/monitor/resume")
    async def resume_monitor(
        user: str = Depends(require_session),
        __: None = Depends(require_csrf),
    ) -> Dict[str, Any]:
        if config.monitor is None:
            raise HTTPException(status_code=503, detail="Monitor not attached to web UI.")
        config.monitor.web_paused.clear()
        return {"ok": True, "paused": False}

    @app.post("/api/listing/flag")
    async def flag_listing(
        request: Request,
        _: str = Depends(require_session),
        __: None = Depends(require_csrf),
    ) -> Dict[str, Any]:
        """Set the user's own state on a listing.

        my_rank (1-5 or null) and/or hidden, stored in the diskcache keyed by
        (marketplace, id) so it joins the same way ratings do — hidden
        listings stay fully tracked.
        """
        try:
            body = await request.json()
        except Exception:
            raise HTTPException(status_code=400, detail="JSON body required") from None
        marketplace = str(body.get("marketplace") or "").strip()
        listing_id = str(body.get("id") or "").strip()
        if not marketplace or not listing_id:
            raise HTTPException(status_code=400, detail="marketplace and id are required")
        key = (CacheType.USER_FLAGS.value, marketplace, listing_id)
        current = cache.get(key)
        flags: Dict[str, Any] = dict(current) if isinstance(current, dict) else {}
        if "my_rank" in body:
            rank = body["my_rank"]
            if rank is None:
                flags.pop("my_rank", None)
            elif isinstance(rank, int) and 1 <= rank <= 5:
                flags["my_rank"] = rank
            else:
                raise HTTPException(status_code=400, detail="my_rank must be 1-5 or null")
        if "hidden" in body:
            flags["hidden"] = bool(body["hidden"])
        flags["updated_at"] = time.time()
        cache.set(key, flags, tag=CacheType.USER_FLAGS.value)
        return {"ok": True, "flags": flags}

    @app.get("/api/env-status")
    def env_status(_: str = Depends(require_session)) -> Dict[str, Any]:
        """Which ${VAR} references in the config actually resolve.

        Reports set / not-set only -- never a value. Closes the gap where a
        missing credential is silent until the first search that needed it.
        """
        referenced: set = set()
        for path in config.config_files:
            try:
                for line in path.read_text(encoding="utf-8").splitlines():
                    # Commented-out examples reference vars nobody needs set;
                    # listing those as "not set" is noise, not signal.
                    if line.lstrip().startswith("#"):
                        continue
                    referenced |= set(re.findall(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}", line))
            except OSError:
                continue
        return {"vars": {name: name in os.environ for name in sorted(referenced)}}

    # ------------------------------------------------------------------
    # Listing photo snapshots.
    #
    # Facebook's CDN URLs are signed, expire, and refuse hotlinked loads, so
    # the browser cannot show them directly for long. This endpoint fetches
    # the image server-side ONCE and caches the bytes on disk — a snapshot
    # that keeps working after the source URL dies.
    #
    # SSRF containment: the client never supplies an image URL. It supplies a
    # listing post URL, which must already exist as a LISTING_DETAILS cache
    # key, and the fetch goes only to the image URL the scraper stored there.
    # ------------------------------------------------------------------
    img_cache_dir = amm_home / "imgcache"

    @app.get("/api/listing-image")
    def listing_image(post: str, _: str = Depends(require_session)) -> FileResponse:
        import requests as _requests  # type: ignore  # local: server module stays uvicorn-only otherwise

        normalized = post.split("?")[0]
        details = cache.get((CacheType.LISTING_DETAILS.value, normalized))
        if not isinstance(details, dict):
            raise HTTPException(status_code=404, detail="Unknown listing.")
        image_url = str(details.get("image") or "")
        if not image_url.startswith(("http://", "https://")):
            raise HTTPException(status_code=404, detail="Listing has no image.")

        img_cache_dir.mkdir(parents=True, exist_ok=True)
        key = hashlib.sha256(normalized.encode()).hexdigest()[:32]
        cached = img_cache_dir / (key + ".img")
        if not cached.exists():
            try:
                resp = _requests.get(
                    image_url,
                    timeout=15,
                    headers={
                        # A plain browser UA and no referrer is what the CDN
                        # expects from a direct visit.
                        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36",
                    },
                    stream=True,
                )
                if resp.status_code != 200:
                    raise HTTPException(status_code=404, detail="Image expired.")
                content = resp.raw.read(5 * 1024 * 1024 + 1, decode_content=True)
                if len(content) > 5 * 1024 * 1024:
                    raise HTTPException(status_code=404, detail="Image too large.")
                cached.write_bytes(content)
            except HTTPException:
                raise
            except Exception:
                raise HTTPException(status_code=404, detail="Image fetch failed.") from None
        return FileResponse(
            cached,
            media_type="image/jpeg",
            headers={"Cache-Control": "private, max-age=86400"},
        )

    # ------------------------------------------------------------------
    # Drive time via OSRM's public demo router — free, keyless, and fine for
    # light personal use. Results cache for a day per rounded coordinate pair
    # so repeated views of the same listing cost nothing.
    # ------------------------------------------------------------------
    @app.get("/api/route")
    def route_estimate(to: str, _: str = Depends(require_session)) -> Dict[str, Any]:
        import requests as _requests  # type: ignore

        from .activity import home_from_config

        match = re.match(r"^\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*$", to)
        if not match:
            raise HTTPException(status_code=400, detail="to must be 'lat,lon'")
        tlat, tlon = float(match.group(1)), float(match.group(2))
        home = home_from_config(config.config_files)
        if home is None:
            raise HTTPException(status_code=404, detail="home_location not set")
        hlat, hlon = home

        cache_key = (
            "route-cache",
            f"{round(hlat, 3)},{round(hlon, 3)}",
            f"{round(tlat, 3)},{round(tlon, 3)}",
        )
        hit = cache.get(cache_key)
        if isinstance(hit, dict) and hit.get("at", 0) > time.time() - 86400:
            return hit["result"]

        url = (
            f"https://router.project-osrm.org/route/v1/driving/"
            f"{hlon},{hlat};{tlon},{tlat}?overview=false"
        )
        try:
            resp = _requests.get(url, timeout=8)
            data = resp.json()
            leg = data["routes"][0]
            result = {
                "minutes": round(leg["duration"] / 60),
                "miles": round(leg["distance"] / 1609.344, 1),
            }
        except KeyboardInterrupt:
            raise
        except Exception:
            raise HTTPException(status_code=502, detail="Routing unavailable.") from None
        cache.set(cache_key, {"at": time.time(), "result": result}, tag="route-cache")
        return result

    @app.get("/api/logs/download")
    def download_log(_: str = Depends(require_session)) -> FileResponse:
        log_path = amm_home / "ai-marketplace-monitor.log"
        if not log_path.exists():
            raise HTTPException(status_code=404, detail="No log file on disk yet.")
        stamp = time.strftime("%Y%m%d-%H%M%S")
        return FileResponse(
            log_path,
            media_type="text/plain; charset=utf-8",
            headers={
                "Content-Disposition": f'attachment; filename="aimm-{stamp}.log"',
                "Cache-Control": "no-store",
            },
        )

    # Sync def for the same reason as the CSV export: build_activity walks the
    # whole cache, so it must stay off the event loop.
    @app.get("/api/activity")
    def activity(limit: int = 500, _: str = Depends(require_session)) -> Dict[str, Any]:
        limit = max(1, min(limit, 2000))
        return build_activity(cache, config_service.all_paths(), limit=limit)

    return app


# ----------------------------------------------------------------------
# Thread runner
# ----------------------------------------------------------------------


class WebUIServer:
    """Runs uvicorn in a background thread."""

    def __init__(
        self,
        config: WebUIConfig,
        state: AuthState,
        config_service: ConfigFileService,
    ) -> None:
        if config.log_handler is None:
            raise ValueError("WebUIConfig.log_handler is required")
        self._config = config
        self._state = state
        self._config_service = config_service
        self._app = create_app(config, state, config_service, config.log_handler)
        self._server: uvicorn.Server | None = None
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._ready = threading.Event()

    def start(self) -> None:
        uv_config = uvicorn.Config(
            self._app,
            host=self._config.host,
            port=self._config.port,
            log_level="warning",
            access_log=False,
            lifespan="off",
        )
        self._server = uvicorn.Server(uv_config)

        def runner() -> None:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            self._loop = loop
            assert self._config.log_handler is not None
            self._config.log_handler.attach_loop(loop)
            self._ready.set()
            try:
                loop.run_until_complete(self._server.serve())  # type: ignore[union-attr]
            finally:
                loop.close()

        self._thread = threading.Thread(target=runner, name="aimm-webui", daemon=True)
        self._thread.start()
        # Give the loop a moment to bind so attach_loop completes before
        # any log records are emitted.
        self._ready.wait(timeout=5)

    def stop(self) -> None:
        if self._server is not None:
            self._server.should_exit = True


def start_webui(
    config: WebUIConfig, logger: logging.Logger | None = None
) -> tuple[WebUIServer, StartupInfo]:
    """Resolve auth, build the service, and start the server thread."""
    if config.log_handler is None:
        raise ValueError("WebUIConfig.log_handler is required")
    state, info = _resolve_auth(config)

    # --webui-host requires credentials. Refuse to expose without auth --
    # unless an authenticating reverse proxy is declared, in which case the
    # proxy is the credential and the UI may run without one of its own.
    if state.exposed and state.auth is None and os.environ.get("AIMM_PROXY_AUTH") != "1":
        raise RuntimeError(
            f"--webui-host {config.host} requires authentication. "
            "Set username/password in a [marketplace.*] config section "
            "or set FACEBOOK_USERNAME and FACEBOOK_PASSWORD environment "
            "variables. Omit --webui-host to run on 127.0.0.1 without "
            "a password."
        )

    config_service = ConfigFileService(config.config_files, logger=logger)
    server = WebUIServer(config, state, config_service)
    server.start()
    return server, info
