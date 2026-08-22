from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypedDict, cast

VIDEO_USE_PIN = "92c2b34e44c205cbc2acae7f6ca7c1c219d5dd66"


class ProbeStream(TypedDict, total=False):
    codec_type: str
    width: int
    height: int
    r_frame_rate: str
    duration: str
    sample_rate: str


@dataclass(frozen=True)
class ProbeResult:
    path: Path
    duration_s: float
    width: int
    height: int
    fps: float
    has_audio: bool
    streams: list[ProbeStream]


@dataclass(frozen=True)
class HelperResult:
    command: list[str]
    stdout: str
    stderr: str


@dataclass(frozen=True)
class DoctorCheck:
    name: str
    ok: bool
    required: bool
    value: str
    detail: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "ok": self.ok,
            "required": self.required,
            "value": self.value,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class DoctorReport:
    ok: bool
    checks: list[DoctorCheck]

    def as_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "checks": [check.as_dict() for check in self.checks]}


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _helper(name: str) -> Path:
    path = _repo_root() / "vendor" / "video-use" / "helpers" / name
    if not path.exists():
        raise FileNotFoundError(f"video-use is not vendored: {path}")
    return path


def probe(path: Path) -> ProbeResult:
    command = [
        "ffprobe",
        "-v",
        "error",
        "-show_streams",
        "-show_format",
        "-of",
        "json",
        str(path),
    ]
    raw = subprocess.run(command, check=True, capture_output=True, text=True)
    payload = json.loads(raw.stdout)
    raw_streams = payload.get("streams", [])
    streams: list[ProbeStream] = [
        cast(ProbeStream, stream) for stream in raw_streams if isinstance(stream, dict)
    ]
    video = cast(
        ProbeStream,
        next((stream for stream in streams if stream.get("codec_type") == "video"), {}),
    )
    fps_text = str(video.get("r_frame_rate", "0/1"))
    numerator, denominator = (int(part) for part in fps_text.split("/", 1))
    duration = float(payload.get("format", {}).get("duration", video.get("duration", 0)))
    return ProbeResult(
        path=path,
        duration_s=duration,
        width=int(video.get("width", 0)),
        height=int(video.get("height", 0)),
        fps=numerator / denominator if denominator else 0,
        has_audio=any(s.get("codec_type") == "audio" for s in streams),
        streams=streams,
    )


def run_helper(name: str, args: list[str]) -> HelperResult:
    command = [sys.executable, str(_helper(name)), *args]
    environment = os.environ.copy()
    environment.setdefault("PYTHONUTF8", "1")
    raw = subprocess.run(
        command,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=environment,
    )
    return HelperResult(command=command, stdout=raw.stdout, stderr=raw.stderr)


def _first_line(value: str) -> str:
    return next((line.strip() for line in value.splitlines() if line.strip()), "")


def _command_version(name: str, args: list[str], *, required: bool) -> DoctorCheck:
    executable = shutil.which(name)
    if executable is None:
        return DoctorCheck(name, False, required, "not found", f"{name} is not on PATH")
    try:
        result = subprocess.run(
            [executable, *args],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except OSError as error:
        return DoctorCheck(name, False, required, executable, str(error))
    output = _first_line(result.stdout or result.stderr)
    return DoctorCheck(
        name,
        result.returncode == 0,
        required,
        output or executable,
        "" if result.returncode == 0 else f"exit code {result.returncode}",
    )


def _node_major(value: str) -> int | None:
    token = value.lstrip("v").split(".", 1)[0]
    return int(token) if token.isdigit() else None


def _video_use_check(root: Path) -> DoctorCheck:
    checkout = root / "vendor" / "video-use"
    helpers = checkout / "helpers"
    required_helpers = ("pack_transcripts.py", "render.py", "grade.py", "timeline_view.py")
    if not (checkout / ".git").exists():
        setup_script = root / "scripts" / "setup_engine.py"
        return DoctorCheck(
            "video-use",
            False,
            True,
            "not installed",
            f"run {sys.executable} {setup_script}",
        )
    missing = [name for name in required_helpers if not (helpers / name).is_file()]
    if missing:
        return DoctorCheck(
            "video-use",
            False,
            True,
            str(checkout),
            f"missing helpers: {', '.join(missing)}",
        )
    git = shutil.which("git")
    if git is None:
        return DoctorCheck("video-use", False, True, str(checkout), "git is not on PATH")
    try:
        result = subprocess.run(
            [git, "-C", str(checkout), "rev-parse", "HEAD"],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except OSError as error:
        return DoctorCheck("video-use", False, True, str(checkout), str(error))
    head = result.stdout.strip()
    ok = result.returncode == 0 and head == VIDEO_USE_PIN
    detail = "" if ok else f"expected {VIDEO_USE_PIN}"
    return DoctorCheck("video-use", ok, True, head or "unknown", detail)


def doctor(template_source: Path | None = None, *, repo_root: Path | None = None) -> DoctorReport:
    """Inspect the local video toolchain without changing files or installing packages."""
    root = (repo_root or _repo_root()).resolve()
    python_ok = sys.version_info >= (3, 11)
    checks = [
        DoctorCheck(
            "python",
            python_ok,
            True,
            f"{sys.version.split()[0]} ({sys.executable})",
            "" if python_ok else "Python 3.11 or newer is required",
        ),
        _video_use_check(root),
        _command_version("ffmpeg", ["-version"], required=True),
        _command_version("ffprobe", ["-version"], required=True),
    ]

    animation_required = template_source is not None
    node = _command_version("node", ["--version"], required=animation_required)
    if node.ok:
        major = _node_major(node.value)
        if major is None or major < 22:
            node = DoctorCheck(
                node.name,
                False,
                node.required,
                node.value,
                "Node 22 or newer is required for Remotion/HyperFrames",
            )
    checks.extend(
        [
            node,
            _command_version("npm", ["--version"], required=animation_required),
            _command_version("npx", ["--version"], required=animation_required),
        ]
    )

    if template_source is None:
        checks.append(
            DoctorCheck(
                "template-source",
                True,
                False,
                "not configured",
                "pass --template-source to validate motion-graphics prerequisites",
            )
        )
    else:
        source = template_source.expanduser().resolve()
        exists = source.is_dir() or (source.is_file() and source.suffix.lower() == ".zip")
        checks.append(
            DoctorCheck(
                "template-source",
                exists,
                True,
                str(source),
                "" if exists else "template source must be an existing directory or ZIP archive",
            )
        )

    return DoctorReport(
        ok=all(check.ok for check in checks if check.required),
        checks=checks,
    )


def pack_transcripts(edit_dir: Path) -> Path:
    run_helper("pack_transcripts.py", ["--edit-dir", str(edit_dir)])
    return edit_dir / "takes_packed.md"


def render(edit_dir: Path, output: Path, preview: bool = False) -> Path:
    args = [str(edit_dir / "edl.json"), "-o", str(output), "--build-subtitles"]
    if preview:
        args.append("--preview")
    run_helper("render.py", args)
    return output


def grade(source: Path, output: Path, preset: str = "neutral_punch") -> Path:
    run_helper("grade.py", [str(source), "-o", str(output), "--preset", preset])
    return output


def timeline_view(source: Path, start: float, end: float, output: Path) -> Path:
    run_helper("timeline_view.py", [str(source), str(start), str(end), "-o", str(output)])
    return output
