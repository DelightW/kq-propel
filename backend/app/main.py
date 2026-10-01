from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

from app import auth, config, database, sentiment
from app.vectorstore import ingest_policy_directory
from app.routers import chat, admin

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


@app.on_event("startup")
def startup():
    database.init_db()
    database.purge_expired_admin_sessions()
    ingest_policy_directory()
    sentiment.train_frustration_classifier()
    print(auth.startup_banner(), flush=True)


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
