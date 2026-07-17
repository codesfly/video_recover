from __future__ import annotations

from hashlib import sha256
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

PACKAGE_DIR = Path(__file__).parent
STATIC_DIR = PACKAGE_DIR / "static"
templates = Jinja2Templates(directory=PACKAGE_DIR / "templates")


def _asset_version() -> str:
    digest = sha256()
    for filename in ("app.css", "app.js"):
        digest.update((STATIC_DIR / filename).read_bytes())
    return digest.hexdigest()[:12]


ASSET_VERSION = _asset_version()


def build_web_router() -> APIRouter:
    router = APIRouter(include_in_schema=False)

    @router.get("/", response_class=HTMLResponse)
    def archive_page(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(
            request=request,
            name="index.html",
            context={"app_version": "0.1.0", "asset_version": ASSET_VERSION},
        )

    return router
