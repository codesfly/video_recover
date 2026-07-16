from __future__ import annotations

from video_recover.config import Settings
from video_recover.main import build_service
from video_recover.mcp_server import build_mcp


def main() -> None:
    settings = Settings()
    settings.ensure_directories()
    build_mcp(build_service(settings)).run(transport="stdio")


if __name__ == "__main__":
    main()
