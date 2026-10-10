"""FastAPI app entrypoint."""

from __future__ import annotations

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .config import LIVEAPP_DIR, settings
from .database import init_db
from .routers import admin, auth, data
from .seed import seed_users

app = FastAPI(
    title="DGP Live Dashboard",
    description="Live, database-backed Easy SBF dashboard with role-based access",
    version="1.0.0",
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


FRONTEND_DIR = LIVEAPP_DIR / "frontend"
INDEX_FILE = FRONTEND_DIR / "index.html"

# Mount BEFORE the catch-all route below. Starlette matches routes in
# registration order, so a mount added in a startup hook would be shadowed by
# the "/{path:path}" SPA fallback and every /static/* asset would 404.
if FRONTEND_DIR.is_dir():
    app.mount("/static", StaticFiles(directory=FRONTEND_DIR), name="static")
else:  # pragma: no cover - misconfigured checkout
    print(f"[liveapp] frontend directory missing: {FRONTEND_DIR}")


@app.on_event("startup")
def on_startup() -> None:
    """Initialize database, ensure tables exist and seed demo users."""
    _warn_if_insecure()
    init_db()
    from .database import SessionLocal

    db = SessionLocal()
    try:
        try:
            created = seed_users(db)
            if created:
                print(f"[liveapp] seeded users: {', '.join(created)}")
            else:
                print("[liveapp] users already present; skipping seed")
        except Exception as exc:
            print(f"[liveapp] user seeding skipped: {exc}")
    finally:
        db.close()


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


@app.get("/_dashboard_template.html")
def dashboard_template():
    tpl = (LIVEAPP_DIR.parent / "_dashboard_template.html")
    if tpl.is_file():
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
