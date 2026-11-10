import asyncio
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

from app import auth, config, database, payments, privacy, sentiment
from app.vectorstore import ingest_policy_directory
from app.routers import admin, chat
from app.routers import payments as payment_routes

FRONTEND_DIR = Path(__file__).resolve().parent.parent.parent / "frontend"

app = FastAPI(title=config.APP_TITLE, description=config.APP_TAGLINE)

# Credentialed requests cannot use a wildcard origin, and a wildcard would in
# any case have allowed any site to drive this API with an operator's cookies.
app.add_middleware(
    CORSMiddleware,
    allow_origins=config.ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type"],
)

app.include_router(chat.router)
app.include_router(admin.router)
app.include_router(payment_routes.router)


@app.on_event("startup")
def startup():
    database.init_db()
    database.purge_expired_admin_sessions()
    ingest_policy_directory()
    sentiment.train_frustration_classifier()
    # Settle or expire payments whose outcome arrived while the server was down.
    payments.reconcile_pending()
    # Retention is enforced on a timer, not on request: personal data must go
    # when its period ends even if nobody opens the dashboard.
    if config.PRIVACY_RETENTION_ENABLED:
        print(f"[privacy] retention purge: {privacy.purge_expired()}", flush=True)
    else:
        print("[privacy] automatic retention is OFF (PRIVACY_RETENTION_ENABLED=false); "
              "personal data is kept until erased on request.", flush=True)
    print(auth.startup_banner(), flush=True)


async def _purge_periodically():
    while True:
        await asyncio.sleep(config.PRIVACY_PURGE_INTERVAL_MINUTES * 60)
        try:
            await asyncio.to_thread(privacy.purge_expired)
        except Exception as exc:  # a failed run is retried on the next tick
            print(f"[privacy] retention purge failed: {exc!r}", flush=True)


@app.on_event("startup")
async def schedule_purge():
    if config.PRIVACY_RETENTION_ENABLED:
        asyncio.get_running_loop().create_task(_purge_periodically())


@app.get("/api/health")
def health():
    return {"status": "ok", "app": config.APP_TITLE}


if FRONTEND_DIR.exists():
    app.mount("/static", StaticFiles(directory=FRONTEND_DIR), name="static")

    @app.get("/")
    def index():
        return FileResponse(FRONTEND_DIR / "index.html")

    @app.get("/admin")
    def admin_page():
        return FileResponse(FRONTEND_DIR / "admin.html")

    @app.get("/privacy")
    def privacy_page():
        return FileResponse(FRONTEND_DIR / "privacy.html")
