from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from pitchcraft.engine import videouse


def test_run_helper_uses_active_python(monkeypatch: Any, tmp_path: Path) -> None:
    helper = tmp_path / "helper.py"
    helper.write_text("")
    seen: list[list[str]] = []
    options: dict[str, object] = {}

    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        seen.append(command)
        options.update(kwargs)
        return subprocess.CompletedProcess(command, 0, stdout="done", stderr="")

    monkeypatch.setattr(videouse, "_helper", lambda _name: helper)
    monkeypatch.setattr(subprocess, "run", run)

    result = videouse.run_helper("helper.py", ["--flag"])

    assert result.command == [sys.executable, str(helper), "--flag"]
    assert seen == [result.command]
    assert options["encoding"] == "utf-8"
    assert isinstance(options["env"], dict)
    assert options["env"]["PYTHONUTF8"] == "1"


def test_render_helper_uses_windows_compatibility_bridge(
    monkeypatch: Any, tmp_path: Path
) -> None:
    helper = tmp_path / "render.py"
    helper.write_text("")
    seen: list[str] = []

    def run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        seen.extend(command)
        return subprocess.CompletedProcess(command, 0, stdout="done", stderr="")

    monkeypatch.setattr(videouse, "_helper", lambda _name: helper)
    monkeypatch.setattr(subprocess, "run", run)

    videouse.run_helper("render.py", ["edl.json"])

    assert Path(seen[1]).name == "render_bridge.py"
    assert seen[2:] == [str(helper), "edl.json"]


def test_doctor_validates_pin_and_motion_prerequisites(monkeypatch: Any, tmp_path: Path) -> None:
    checkout = tmp_path / "vendor" / "video-use"
    (checkout / ".git").mkdir(parents=True)
    helpers = checkout / "helpers"
    helpers.mkdir()
    for name in ("pack_transcripts.py", "render.py", "grade.py", "timeline_view.py"):
        (helpers / name).write_text("")
    template = tmp_path / "templates.zip"
    template.write_bytes(b"zip")

    monkeypatch.setattr(shutil, "which", lambda name: f"/tools/{name}")

    versions = {
        "git": videouse.VIDEO_USE_PIN,
        "ffmpeg": "ffmpeg version 7.1",
        "ffprobe": "ffprobe version 7.1",
        "node": "v22.14.0",
        "npm": "10.9.2",
        "npx": "10.9.2",
    }

    def run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        tool = Path(command[0]).name
        return subprocess.CompletedProcess(command, 0, stdout=versions[tool] + "\n", stderr="")

    monkeypatch.setattr(subprocess, "run", run)

    report = videouse.doctor(template, repo_root=tmp_path)

    assert report.ok
    assert all(check.ok for check in report.checks if check.required)
    assert next(check for check in report.checks if check.name == "video-use").value == (
        videouse.VIDEO_USE_PIN
    )
    assert next(check for check in report.checks if check.name == "template-source").required


def test_doctor_keeps_animation_tools_optional_without_templates(
    monkeypatch: Any, tmp_path: Path
) -> None:
    monkeypatch.setattr(videouse, "_video_use_check", lambda _root: _passing_video_use())
    monkeypatch.setattr(
        videouse,
        "_command_version",
        lambda name, _args, *, required: videouse.DoctorCheck(
            name,
            name in {"ffmpeg", "ffprobe"},
            required,
            "available" if name in {"ffmpeg", "ffprobe"} else "not found",
        ),
    )

    report = videouse.doctor(repo_root=tmp_path)

    assert report.ok
    node = next(check for check in report.checks if check.name == "node")
    assert not node.ok
    assert not node.required


def _passing_video_use() -> videouse.DoctorCheck:
    return videouse.DoctorCheck("video-use", True, True, videouse.VIDEO_USE_PIN)
