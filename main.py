"""
IG Unified API — single FastAPI server hosting Growth, Status, and Checker services.
Run with: uvicorn main:app --reload --port 8000

All three services share one port, differentiated by route prefix:
  /api/v1/growth/...   — Growth Metrics (ADB/UIAutomator)
  /api/v1/status/...   — Account Status (ADB/UIAutomator)
  /api/v1/checker/...  — Account Checker (HTTP scraper)

Frontends:
  /              — Growth dashboard (default)
  /growth        — Growth dashboard
  /status        — Status dashboard
  /checker       — Checker dashboard
"""

import hmac
import time
from collections import defaultdict
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
from starlette.middleware.cors import CORSMiddleware

from pydantic import BaseModel

from logger import logger
from config import shared_settings, growth_settings, status_settings, checker_settings
from shared.api_keys import load_keys, list_keys, create_key, delete_key, is_valid_key

# ── Import routers ────────────────────────────────────────
from growth.routes import router as growth_router
from status.routes import router as status_router
from checker.routes import router as checker_router

# Admin key = full control (manage keys + all endpoints)
# api_key   = legacy single key (backward compat — treated as admin if no admin_key)
ADMIN_KEY = shared_settings.admin_key.strip() if shared_settings.admin_key else ""
LEGACY_KEY = shared_settings.api_key.strip() if shared_settings.api_key else ""

# If admin_key is set, it's the admin. Otherwise fall back to api_key as admin.
EFFECTIVE_ADMIN_KEY = ADMIN_KEY or LEGACY_KEY
# Auth is enabled if any key is configured
AUTH_ENABLED = bool(EFFECTIVE_ADMIN_KEY)


# ── Lifespan ──────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup / shutdown for all services."""
    logger.info("IG Unified API starting up")

    # ── Growth init ──
    from growth.registry import load_devices as growth_load_devices
    from growth.database import init_db as growth_init_db, close_db as growth_close_db
    from growth.scheduler import init_scheduler as growth_init_sched, start_scheduler as growth_start, stop_scheduler as growth_stop

    growth_load_devices()
    await growth_init_db()
    growth_init_sched()
    if growth_settings.scheduler_enabled:
        growth_start()
        logger.info(f"Growth scheduler auto-started — daily at {growth_settings.scheduler_daily_hour:02d}:{growth_settings.scheduler_daily_minute:02d}")

    # ── Status init ──
    from status.registry import load_devices as status_load_devices
    from status.database import init_db as status_init_db, close_db as status_close_db
    from status.scheduler import init_scheduler as status_init_sched, start_scheduler as status_start, stop_scheduler as status_stop

    status_load_devices()
    await status_init_db()
    status_init_sched()
    if status_settings.scheduler_enabled:
        status_start()
        logger.info(f"Status scheduler auto-started — daily at {status_settings.scheduler_daily_hour:02d}:{status_settings.scheduler_daily_minute:02d}")

    # ── Checker init ──
    from checker.database import init_db as checker_init_db, close_db as checker_close_db
    from checker.scheduler import start_scheduler as checker_start, stop_scheduler as checker_stop

    await checker_init_db()
    if checker_settings.scheduler_enabled:
        checker_start()
        logger.info("Checker scheduler auto-started")

    # ── Telegram bot ──
    _bot_task = None
    try:
        from config import telegram_settings
        if telegram_settings.bot_token:
            import asyncio
            from telegram.bot import run as bot_run, stop as bot_stop
            _bot_task = asyncio.create_task(bot_run())
            logger.info("Telegram bot started")
    except Exception as e:
        logger.warning(f"Telegram bot failed to start: {e}")

    # ── Worker heartbeat (if HUB_URL configured) ──
    from shared.hub_reporter import start_heartbeat, stop_heartbeat
    start_heartbeat()

    # ── API Keys store ──
    load_keys()

    if AUTH_ENABLED:
        if ADMIN_KEY:
            logger.info("API key authentication ENABLED (admin_key set)")
        else:
            logger.info("API key authentication ENABLED (legacy api_key as admin)")
    else:
        logger.warning("API key authentication DISABLED — set ADMIN_KEY in .env for production")

    logger.info("IG Unified API ready — all services loaded")

    yield

    # ── Shutdown ──
    logger.info("IG Unified API shutting down")

    # Stop telegram bot
    if _bot_task:
        try:
            from telegram.bot import stop as bot_stop
            bot_stop()
            _bot_task.cancel()
        except Exception:
            pass

    stop_heartbeat()
    growth_stop()
    status_stop()
    checker_stop()
    await growth_close_db()
    await status_close_db()
    await checker_close_db()


# ── App ───────────────────────────────────────────────────

app = FastAPI(
    title="IG Unified API",
    description="Combined Growth Metrics + Account Status + Account Checker",
    version="2.0.0",
    lifespan=lifespan,
)

# ── CORS ──────────────────────────────────────────────────
origins = [o.strip() for o in shared_settings.cors_origins.split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── API Key Auth Middleware ───────────────────────────────
OPEN_PATHS = {"/", "/docs", "/openapi.json", "/redoc", "/growth", "/status", "/checker"}
OPEN_PREFIXES = ("/static",)


def _is_admin(key: str) -> bool:
    """Check if key is the admin key."""
    return bool(EFFECTIVE_ADMIN_KEY and hmac.compare_digest(key, EFFECTIVE_ADMIN_KEY))


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    if not AUTH_ENABLED:
        # No auth configured — allow everything
        request.state.is_admin = True
        return await call_next(request)

    path = request.url.path
    if path in OPEN_PATHS or path.startswith(OPEN_PREFIXES):
        return await call_next(request)

    key = request.headers.get("X-API-Key", "")
    if not key:
        return JSONResponse(status_code=401, content={"detail": "API key required."})

    # Admin key — full access
    if _is_admin(key):
        request.state.is_admin = True
        return await call_next(request)

    # Generated API key — normal access (no admin routes)
    if is_valid_key(key):
        request.state.is_admin = False
        if path.startswith("/api/v1/admin"):
            return JSONResponse(status_code=403, content={"detail": "Admin access required."})
        return await call_next(request)

    return JSONResponse(status_code=401, content={"detail": "Invalid API key."})


# ── Rate Limiter ──────────────────────────────────────────
_rate_log: dict[str, list[float]] = defaultdict(list)
_whitelist: set[str] = set()

_raw = shared_settings.rate_limit_whitelist.strip() if shared_settings.rate_limit_whitelist else ""
_whitelist = {ip.strip() for ip in _raw.split(",") if ip.strip()}


@app.middleware("http")
async def rate_limit_middleware(request: Request, call_next):
    if shared_settings.rate_limit_force <= 0:
        return await call_next(request)

    path = request.url.path
    if "/check/force" not in path:
        return await call_next(request)

    client_ip = request.client.host if request.client else "unknown"
    if client_ip in _whitelist:
        return await call_next(request)

    now = time.time()
    window = 60.0
    _rate_log[client_ip] = [t for t in _rate_log[client_ip] if now - t < window]

    if len(_rate_log) > 1000:
        stale = [ip for ip, ts in _rate_log.items() if not ts]
        for ip in stale:
            del _rate_log[ip]

    if len(_rate_log[client_ip]) >= shared_settings.rate_limit_force:
        return JSONResponse(
            status_code=429,
            content={
                "detail": f"Rate limit exceeded. Max {shared_settings.rate_limit_force} force checks per minute.",
                "retry_after_seconds": int(window - (now - _rate_log[client_ip][0])),
            },
        )

    _rate_log[client_ip].append(now)
    return await call_next(request)


# ── Admin routes (API key management) ────────────────────

class KeyCreateBody(BaseModel):
    label: str = ""


@app.get("/api/v1/admin/keys", tags=["Admin"])
async def api_list_keys():
    """List all API keys (admin only)."""
    return {"keys": list_keys()}


@app.post("/api/v1/admin/keys", tags=["Admin"])
async def api_create_key(body: KeyCreateBody):
    """Create a new API key (admin only). Returns the full key once."""
    entry = create_key(label=body.label)
    return {
        "created": True,
        "id": entry["id"],
        "key": entry["key"],
        "label": entry["label"],
        "created_at": entry["created_at"],
    }


@app.delete("/api/v1/admin/keys/{key_id}", tags=["Admin"])
async def api_delete_key(key_id: str):
    """Delete an API key (admin only)."""
    if not delete_key(key_id):
        return JSONResponse(status_code=404, content={"detail": f"Key {key_id} not found"})
    return {"deleted": True, "id": key_id}


@app.get("/api/v1/admin/whoami", tags=["Admin"])
async def api_whoami(request: Request):
    """Check if current key is admin."""
    is_admin = getattr(request.state, "is_admin", False)
    return {"is_admin": is_admin}


# ── Mount routers ─────────────────────────────────────────
app.include_router(growth_router)
app.include_router(status_router)
app.include_router(checker_router)


# ── Worker endpoints (Hub ↔ Worker communication) ────────

class WorkerReport(BaseModel):
    worker_id: str
    service: str  # "growth", "status", "checker"
    result: dict
    reported_at: str = ""


class WorkerHeartbeat(BaseModel):
    worker_id: str
    worker_url: str
    services: dict  # {growth: [...devices], status: [...devices], checker: [...]}


class WorkerCheckRequest(BaseModel):
    service: str     # "growth" or "status"
    device_id: str


class WorkerViralReport(BaseModel):
    worker_id: str
    alerts: list[dict]
    reported_at: str = ""


@app.post("/api/v1/worker/viral")
async def receive_worker_viral(report: WorkerViralReport):
    """Receive viral alerts detected on a remote worker and persist locally.

    Workers run their own viral_detection() and keep a copy in their local DB,
    but without this endpoint those alerts would never reach the hub dashboard.
    This re-uses save_viral_alerts (with dedup by device_id+content_id) so
    repeated reports of the same trending content don't pile up.
    """
    if not report.alerts:
        return {"status": "ok", "saved": 0, "worker_id": report.worker_id}
    try:
        from growth.database import save_viral_alerts
        # Tag each alert with the worker for traceability — schema accepts
        # extra dict keys silently, the writer only takes the columns it knows.
        for a in report.alerts:
            a.setdefault("_worker_id", report.worker_id)
        saved = await save_viral_alerts(report.alerts)
        logger.info(
            f"[hub] Received {len(report.alerts)} viral alert(s) from worker "
            f"'{report.worker_id}', saved {saved} after dedup"
        )
        return {"status": "ok", "received": len(report.alerts), "saved": saved, "worker_id": report.worker_id}
    except Exception as e:
        logger.error(f"[hub] Failed to save worker viral alerts: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


@app.post("/api/v1/worker/report")
async def receive_worker_report(report: WorkerReport, request: Request):
    """Receive a check result from a remote worker and save it locally."""
    service = report.service.lower()
    result = report.result
    worker = report.worker_id

    # Tag the result so we know it came from a worker
    result["_worker_id"] = worker
    result["_reported_at"] = report.reported_at

    # Renew remote activity tracking — DO NOT clear here.
    # A device with multiple accounts triggers multiple /worker/report calls
    # (one per account). If we clear on the first one, the frontend sees a
    # premature busy→free transition, refreshes once, then loses every
    # subsequent account's update until manual reload.
    # Instead, refresh the timestamp on each report so the device stays
    # "busy" in /api/v1/activity for the full duration. It eventually
    # expires via _REMOTE_ACTIVITY_TIMEOUT (which is the natural end signal).
    try:
        from shared.worker_registry import register_remote_check
        device_id = result.get("device_id", "")
        if device_id:
            register_remote_check(service, device_id, worker)
    except ImportError:
        pass

    try:
        if service == "growth":
            from growth.database import save_metric
            await save_metric(result)
        elif service == "status":
            from status.database import save_check
            await save_check(result)
        elif service == "checker":
            from checker.database import save_check as checker_save
            username = result.get("username", "")
            if username:
                await checker_save(username, result)
            else:
                return JSONResponse({"error": "checker result missing username"}, status_code=400)
        else:
            return JSONResponse({"error": f"unknown service: {service}"}, status_code=400)

        logger.info(f"[hub] Received {service} result from worker '{worker}' for device {result.get('device_id', '?')}")
        return {"status": "ok", "service": service, "worker_id": worker}

    except Exception as e:
        logger.error(f"[hub] Failed to save worker report: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


@app.post("/api/v1/worker/heartbeat")
async def receive_worker_heartbeat(hb: WorkerHeartbeat):
    """Receive a heartbeat from a remote worker with its device list."""
    from shared.worker_registry import register_heartbeat
    register_heartbeat(hb.worker_id, hb.worker_url, hb.services)
    return {"status": "ok", "worker_id": hb.worker_id}


@app.get("/api/v1/workers")
async def list_workers():
    """List all connected workers and their devices."""
    from shared.worker_registry import get_all_workers
    workers = get_all_workers()
    return {
        "total": len(workers),
        "workers": {
            wid: {
                "url": w["url"],
                "last_seen": w["last_seen"],
                "devices": {
                    svc: len(devs) for svc, devs in w["services"].items()
                },
            }
            for wid, w in workers.items()
        },
    }


@app.get("/api/v1/workers/devices/{service}")
async def list_worker_devices(service: str):
    """List all devices from workers for a given service (growth/status)."""
    from shared.worker_registry import get_worker_devices
    devices = get_worker_devices(service)
    return {"total": len(devices), "devices": devices}


@app.post("/api/v1/workers/check")
async def proxy_check_to_worker(req: WorkerCheckRequest):
    """Proxy a check request to the worker that owns the device.

    The hub finds which worker has the device and forwards the
    check/force request to the worker's API.
    """
    import httpx as _httpx
    from shared.worker_registry import find_worker_for_device

    found = find_worker_for_device(req.device_id)
    if not found:
        return JSONResponse(
            {"error": f"Device '{req.device_id}' not found on any worker"},
            status_code=404,
        )

    worker_id, worker_url = found
    service = req.service.lower()

    if service not in ("growth", "status"):
        return JSONResponse({"error": f"Unsupported service: {service}"}, status_code=400)

    # Forward the check to the worker's own API
    target = f"{worker_url}/api/v1/{service}/check/force/{req.device_id}"
    logger.info(f"[hub] Proxying {service} check for {req.device_id} to worker '{worker_id}' @ {target}")

    try:
        headers = {}
        if EFFECTIVE_ADMIN_KEY:
            headers["X-API-Key"] = EFFECTIVE_ADMIN_KEY
        # Short timeout — just confirm the worker accepted the job
        # Results arrive async via hub_reporter
        async with _httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(target, headers=headers)
            return JSONResponse(content=resp.json(), status_code=resp.status_code)
    except _httpx.ReadTimeout:
        # Worker accepted the check but it takes long — that's expected
        return JSONResponse(
            {"status": "accepted", "device_id": req.device_id,
             "message": "Check started on remote worker. Results will appear when complete."},
            status_code=202,
        )
    except Exception as e:
        logger.error(f"[hub] Failed to proxy check to worker '{worker_id}': {e}")
        return JSONResponse({"error": f"Worker unreachable: {e}"}, status_code=502)


@app.delete("/api/v1/workers/viral")
async def clear_workers_viral():
    """Fan-out viral-alerts clear to every connected worker.

    The hub's own /api/v1/growth/metrics/viral/alerts DELETE only clears the
    hub's local table. This endpoint additionally walks every registered
    worker and tells each one to wipe its own viral_alerts. Returns a
    per-worker breakdown.
    """
    import httpx as _httpx
    from shared.worker_registry import get_all_workers

    workers = get_all_workers()
    results = {}
    headers = {}
    if EFFECTIVE_ADMIN_KEY:
        headers["X-API-Key"] = EFFECTIVE_ADMIN_KEY

    async with _httpx.AsyncClient(timeout=15) as client:
        for wid, w in workers.items():
            target = f"{w['url']}/api/v1/growth/metrics/viral/alerts"
            try:
                resp = await client.delete(target, headers=headers)
                if resp.status_code == 200:
                    body = resp.json()
                    results[wid] = {"deleted": body.get("deleted", 0), "ok": True}
                else:
                    results[wid] = {"deleted": 0, "ok": False, "status": resp.status_code}
            except Exception as e:
                results[wid] = {"deleted": 0, "ok": False, "error": str(e)}

    total = sum(v.get("deleted", 0) for v in results.values())
    logger.info(f"[hub] Cleared viral alerts on {len(results)} worker(s), total deleted={total}")
    return {"total_deleted": total, "workers": results}


# ── Global activity endpoint ──────────────────────────────
@app.get("/api/v1/activity")
async def get_activity():
    """Consolidated activity status across all 3 services (local + remote workers)."""
    from checker.runner import get_activity_summary as checker_activity
    from growth.orchestrator import get_activity_summary as growth_activity
    from status.orchestrator import get_activity_summary as status_activity

    growth = growth_activity()
    status = status_activity()
    checker = checker_activity()

    # Merge remote worker activity into local summaries
    try:
        from shared.worker_registry import get_remote_activity
        for ra in get_remote_activity():
            svc = ra["service"]
            target = growth if svc == "growth" else status if svc == "status" else None
            if target is None:
                continue
            # If local summary is inactive, bootstrap it
            if not target.get("active"):
                target["active"] = True
                target["mode"] = "single"
                target["total_devices"] = 0
                target["completed_devices"] = 0
                target["devices"] = []
            target["devices"].append({
                "device_id": ra["device_id"],
                "status": "checking",
                "started_at": ra["started_at"],
                "_remote": True,
                "_worker_id": ra["worker_id"],
            })
            target["total_devices"] = len(target["devices"])
    except ImportError:
        pass  # worker_registry not available

    return {
        "checker": checker,
        "growth": growth,
        "status": status,
    }


# ── Serve frontends ──────────────────────────────────────
FRONTEND_DIR = Path(__file__).parent / "frontend"


@app.get("/", include_in_schema=False)
async def serve_hub():
    return FileResponse(FRONTEND_DIR / "index.html")


@app.get("/growth", include_in_schema=False)
async def serve_growth():
    return FileResponse(FRONTEND_DIR / "ig_growth.html")


@app.get("/status", include_in_schema=False)
async def serve_status():
    return FileResponse(FRONTEND_DIR / "ig_status.html")


@app.get("/checker", include_in_schema=False)
async def serve_checker():
    return FileResponse(FRONTEND_DIR / "dashboard.html")


if FRONTEND_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(FRONTEND_DIR)), name="static")
