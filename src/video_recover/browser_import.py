from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from video_recover.config import Settings
from video_recover.errors import InternalFailure, UserFacingError
from video_recover.main import build_service


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Import a media file captured from a logged-in Chrome tab.",
    )
    parser.add_argument("--url", required=True, help="Canonical Douyin video URL")
    parser.add_argument("--file", required=True, type=Path, help="File under data/browser-capture")
    parser.add_argument("--description", required=True, help="Visible Douyin post description")
    parser.add_argument("--author", required=True, help="Visible Douyin author name")
    parser.add_argument("--duration", type=float, default=None, help="Duration in seconds")
    parser.add_argument(
        "--no-transcribe",
        action="store_true",
        help="Import the video without scheduling speech transcription",
    )
    return parser


def _print_error(error: UserFacingError) -> int:
    print(
        json.dumps(
            {"error_code": error.code, "message": error.message},
            ensure_ascii=False,
        ),
        file=sys.stderr,
    )
    return 2


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        settings = Settings()
        settings.ensure_directories()
        service = build_service(settings)
        task = service.import_local_capture(
            args.url,
            args.file,
            description=args.description,
            author=args.author,
            duration_seconds=args.duration,
            transcribe=not args.no_transcribe,
        )
    except UserFacingError as exc:
        return _print_error(exc)
    except Exception:
        return _print_error(InternalFailure())
    print(
        json.dumps(
            {
                "task_id": task.id,
                "status": task.status.value,
                "aweme_id": task.aweme_id,
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
