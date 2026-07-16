from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture
def tmp_settings(tmp_path: Path):
    from video_recover.config import Settings

    return Settings(data_dir=tmp_path / "data")

