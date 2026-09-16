from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

from app import config, database, sentiment
from app.vectorstore import ingest_policy_directory
from app.routers import chat, admin

FRONTEND_DIR = Path(__file__).resolve().parent.parent.parent / "frontend"

app = FastAPI(title=config.APP_TITLE, description=config.APP_TAGLINE)

app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"],
)

app.include_router(chat.router)
app.include_router(admin.router)


@app.on_event("startup")
def startup():
    database.init_db()
    ingest_policy_directory()
    sentiment.train_frustration_classifier()


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
