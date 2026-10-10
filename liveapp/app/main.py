"""FastAPI app entrypoint."""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .config import LIVEAPP_DIR, settings
from .database import init_db
from .routers import admin, auth, data, health
from .seed import bootstrap_admin, seed_users


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Prepare the database, but never fail the boot because of it.

    Vercel runs lifespan startup through uvicorn with ``lifespan="auto"``. If
    this handler raises, the process exits before the ASGI app is served and
    every single request comes back as ``500 FUNCTION_INVOCATION_FAILED`` with
    no diagnostic in the logs. A database that is briefly unreachable must
    degrade to a 503 on the affected endpoints, not take the site offline.
    """
    _warn_if_insecure()
    error = init_db()
    if error is None:
        _ensure_first_user()
    else:
        print(f"[liveapp] database not ready, serving degraded: {error}")
    yield


app = FastAPI(
    title="DGP Live Dashboard",
    description="Live, database-backed Easy SBF dashboard with role-based access",
    version="1.0.0",
    lifespan=lifespan,
)

# CORS is off unless DGP_CORS_ORIGINS is set. The frontend is served by this
# same app, so it is always same-origin and needs no CORS. Wildcard origins
# must not be combined with credentials in production.
if settings.cors_origin_list:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

app.include_router(auth.router)
app.include_router(data.router)
app.include_router(admin.router)
app.include_router(health.router)


FRONTEND_DIR = LIVEAPP_DIR / "frontend"
INDEX_FILE = FRONTEND_DIR / "index.html"

# Mount BEFORE the catch-all route below. Starlette matches routes in
# registration order, so a mount added in a startup hook would be shadowed by
# the "/{path:path}" SPA fallback and every /static/* asset would 404.
if FRONTEND_DIR.is_dir():
    app.mount("/static", StaticFiles(directory=FRONTEND_DIR), name="static")
else:  # pragma: no cover - misconfigured checkout
    print(f"[liveapp] frontend directory missing: {FRONTEND_DIR}")


def _ensure_first_user() -> None:
    """Guarantee the deployment has at least one way to log in.

    The env-configured bootstrap admin wins on an empty database; on the source
    machine the gitignored seed file adds the rest. Both are no-ops once users
    exist, and neither can overwrite an existing password.
    """
    from .database import SessionLocal

    def _step(label: str, fn) -> None:
        db = SessionLocal()
        try:
            result = fn(db)
            print(f"[liveapp] {label}: {result}" if result else f"[liveapp] {label}: nothing to do")
        except Exception as exc:
            # Must never stop startup: the database may legitimately already
            # hold users, and the console is the only diagnostic available.
            print(f"[liveapp] {label} skipped: {exc}")
        finally:
            db.close()

    def _bootstrap(db):
        name = bootstrap_admin(db)
        return f"created admin '{name}'" if name else None

    def _seed(db):
        created = seed_users(db)
        return f"seeded {', '.join(created)}" if created else None

    _step("bootstrap admin", _bootstrap)
    _step("user seed", _seed)


def _warn_if_insecure() -> None:
    """Print loud warnings for settings that are unsafe outside localhost."""
    secret = settings.jwt_secret or ""
    if "change-me" in secret or len(secret) < 32:
        print(
            "\n"
            "  ============================================================\n"
            "   WARNING: DGP_JWT_SECRET is still the default or too short.\n"
            "   Anyone can forge a login token. Set a strong secret:\n"
            "     python -c \"import secrets; print(secrets.token_urlsafe(48))\"\n"
            "   -> liveapp/.env  (DGP_JWT_SECRET=...)\n"
            "  ============================================================\n"
        )
    if "*" in settings.cors_origin_list:
        print("[liveapp] WARNING: DGP_CORS_ORIGINS contains '*'; restrict it.")


@app.exception_handler(HTTPException)
async def handle_http(req: Request, e: HTTPException):  # pragma: no cover - debugging helper
    return JSONResponse(status_code=e.status_code, content={"detail": e.detail})


@app.get("/")
def root():
    if INDEX_FILE.is_file():
        return FileResponse(INDEX_FILE)
    raise HTTPException(status_code=404, detail="frontend/index.html not found")


def find_template():
    """Locate _dashboard_template.html.

    Normally it sits at the repo root, one level above liveapp/. Serverless
    bundlers can relocate the function, so a couple of fallbacks are checked
    before giving up.
    """
    candidates = [
        LIVEAPP_DIR.parent / "_dashboard_template.html",
        LIVEAPP_DIR / "_dashboard_template.html",
        Path.cwd() / "_dashboard_template.html",
    ]
    for path in candidates:
        if path.is_file():
            return path
    print(f"[liveapp] template not found; looked in: {[str(c) for c in candidates]}")
    return None


@app.get("/_dashboard_template.html")
def dashboard_template():
    tpl = find_template()
    if tpl is not None:
        return FileResponse(tpl)
    raise HTTPException(status_code=404, detail="_dashboard_template.html not found")


@app.get("/{path:path}")
def spa_fallback(path: str):
    """Serve the SPA for any non-API route."""
    if path.startswith("api/") or path.startswith("static/"):
        raise HTTPException(status_code=404)
    if INDEX_FILE.is_file():
        return FileResponse(INDEX_FILE)
    raise HTTPException(status_code=404, detail="frontend/index.html not found")
