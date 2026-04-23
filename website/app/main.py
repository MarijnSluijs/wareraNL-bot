from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from .auth import init_oauth, router as auth_router
from .config import load_settings
from .routers.api import router as api_router
from .routers.pages import router as pages_router
from .services.dashboard_data import PanelDataService


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = load_settings()
    app.state.settings = settings
    app.state.templates = Jinja2Templates(directory="website/app/templates")
    app.state.data_service = PanelDataService(
        db_path=settings.bot_db_path,
        log_path=settings.bot_log_path,
        config_path=settings.bot_config_path,
        audit_log_path=settings.audit_log_path,
    )
    init_oauth(settings)
    yield

app = FastAPI(title="WarEraNL Panel", lifespan=lifespan)
settings = load_settings()
app.add_middleware(
    SessionMiddleware,
    secret_key=settings.session_secret,
    same_site="lax",
    https_only=False,
)
app.mount("/static", StaticFiles(directory="website/app/static"), name="static")

app.include_router(auth_router)
app.include_router(pages_router)
app.include_router(api_router)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    if exc.status_code == 401:
        return RedirectResponse(url="/auth/login", status_code=302)
    if exc.status_code == 403 and request.url.path.startswith("/api/"):
        return JSONResponse(status_code=403, content={"detail": "Forbidden"})
    if exc.status_code == 403:
        user = request.session.get("user")
        return request.app.state.templates.TemplateResponse(
            request,
            "forbidden.html",
            {"user": user, "section": "forbidden"},
            status_code=403,
        )
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})
