from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import TypedDict, cast


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


def _helper(name: str) -> Path:
    root = Path(__file__).resolve().parents[2]
    path = root / "vendor" / "video-use" / "helpers" / name
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
    command = ["python", str(_helper(name)), *args]
    raw = subprocess.run(command, check=True, capture_output=True, text=True)
    return HelperResult(command=command, stdout=raw.stdout, stderr=raw.stderr)


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
