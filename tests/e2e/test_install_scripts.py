from __future__ import annotations

import plistlib
from pathlib import Path

import yaml

ROOT = Path(__file__).parents[2]


def test_compose_binds_only_loopback_and_persists_data() -> None:
    compose = yaml.safe_load((ROOT / "compose.yaml").read_text(encoding="utf-8"))
    app = compose["services"]["app"]

    assert app["ports"] == ["127.0.0.1:8787:8787"]
    assert app["restart"] == "unless-stopped"
    assert "${VIDEO_RECOVER_DATA_DIR}:/data" in app["volumes"]
    assert app["healthcheck"]["test"][-1] == "http://127.0.0.1:8787/healthz"
    assert app["deploy"]["resources"]["limits"]["memory"] == "8G"


def test_docker_image_is_unprivileged_and_arm_compatible() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")

    assert "FROM python:3.12-slim" in dockerfile
    assert "ffmpeg" in dockerfile
    assert "USER app" in dockerfile
    assert "HEALTHCHECK" in dockerfile
    assert "--platform=" not in dockerfile


def test_launch_agent_runs_as_background_user_process() -> None:
    with (ROOT / "deploy/com.codesfly.video-recover.worker.plist").open("rb") as source:
        plist = plistlib.load(source)

    assert plist["Label"] == "com.codesfly.video-recover.worker"
    assert plist["RunAtLoad"] is True
    assert plist["KeepAlive"] is True
    assert plist["ProcessType"] == "Background"
    assert "__WORKER_EXECUTABLE__" in plist["ProgramArguments"]
    assert plist["EnvironmentVariables"]["VIDEO_RECOVER_DATA_DIR"] == "__DATA_DIR__"
    assert plist["EnvironmentVariables"]["VIDEO_RECOVER_WORKER_TOKEN"] == "__WORKER_TOKEN__"


def test_up_script_builds_before_replacing_and_generates_token() -> None:
    script = (ROOT / "scripts/dev-up.sh").read_text(encoding="utf-8")

    build_index = script.index("docker compose build")
    up_index = script.index("docker compose up -d")
    assert build_index < up_index
    assert "openssl rand -hex 32" in script
    assert "scripts/dev-check.sh" in script


def test_mcp_installer_covers_codex_and_claude_stdio() -> None:
    script = (ROOT / "scripts/install-mcp.sh").read_text(encoding="utf-8")

    assert "codex mcp add video-recover --url http://127.0.0.1:8787/mcp" in script
    assert "claude_desktop_config.json" in script
    assert "docker compose" in script
    assert "video_recover.mcp_stdio" in script
    assert "backup" in script.lower()


def test_env_example_does_not_contain_real_secret() -> None:
    values = (ROOT / ".env.example").read_text(encoding="utf-8")

    assert "VIDEO_RECOVER_WORKER_TOKEN=change-me" in values
    assert "VIDEO_RECOVER_DATA_DIR=" in values


def test_shell_scripts_are_strict_and_have_shebangs() -> None:
    scripts = sorted((ROOT / "scripts").glob("*.sh"))
    assert scripts
    for script in scripts:
        content = script.read_text(encoding="utf-8")
        assert content.startswith("#!/usr/bin/env bash")
        assert "set -euo pipefail" in content
