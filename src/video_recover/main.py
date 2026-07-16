from __future__ import annotations

import sqlite3
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI

from video_recover.api import build_router
from video_recover.config import Settings
from video_recover.crypto import CookieVault
from video_recover.parsers import DouyinPageParser, ParserChain, YtDlpParser
from video_recover.repository import Repository
from video_recover.runner import JobRunner
from video_recover.service import VideoService
from video_recover.transcribers import CpuTranscriber


def probe_sqlite(database_path: Path) -> None:
    with sqlite3.connect(database_path) as connection:
        connection.execute("SELECT 1").fetchone()


def build_lifespan(
    settings: Settings,
    runner: JobRunner | None,
) -> Callable[[FastAPI], AsyncIterator[None]]:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        settings.ensure_directories()
        app.state.runner_enabled = runner is not None
        thread = runner.start_thread() if runner is not None else None
        try:
            yield
        finally:
            if runner is not None:
                runner.stop()
            if thread is not None:
                thread.join(timeout=5)

    return lifespan


def build_service(settings: Settings) -> VideoService:
    return VideoService(
        settings=settings,
        repository=Repository(settings.database_path),
        parser=ParserChain([YtDlpParser(), DouyinPageParser()]),
        cookie_vault=CookieVault(settings.secret_key_path),
    )


def create_app(
    settings: Settings | None = None,
    *,
    service: VideoService | None = None,
    start_runner: bool = True,
) -> FastAPI:
    config = settings or Settings()
    config.ensure_directories()
    video_service = service or build_service(config)
    runner = None
    if start_runner:
        runner = JobRunner(
            video_service,
            cpu_transcriber=CpuTranscriber(),
            allow_cpu_fallback=config.cpu_fallback_enabled,
        )
    app = FastAPI(
        title="VideoRecover",
        version="0.1.0",
        lifespan=build_lifespan(config, runner),
    )
    app.state.settings = config
    app.state.video_service = video_service
    app.include_router(build_router(video_service, config))

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
