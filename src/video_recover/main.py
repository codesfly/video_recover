from __future__ import annotations

import sqlite3
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI

from video_recover.config import Settings


def probe_sqlite(database_path: Path) -> None:
    with sqlite3.connect(database_path) as connection:
        connection.execute("SELECT 1").fetchone()


def build_lifespan(
    settings: Settings,
    start_runner: bool,
) -> Callable[[FastAPI], AsyncIterator[None]]:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        settings.ensure_directories()
        app.state.runner_enabled = start_runner
        yield

    return lifespan


def create_app(settings: Settings | None = None, *, start_runner: bool = True) -> FastAPI:
    config = settings or Settings()
    config.ensure_directories()
    app = FastAPI(
        title="VideoRecover",
        version="0.1.0",
        lifespan=build_lifespan(config, start_runner),
    )

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        probe_sqlite(config.database_path)
        return {"status": "ok", "storage": "ok"}

    return app


def run() -> None:
    settings = Settings()
    uvicorn.run(
        create_app(settings),
        host=settings.host,
        port=settings.port,
        log_level=settings.log_level.lower(),
    )


if __name__ == "__main__":
    run()
