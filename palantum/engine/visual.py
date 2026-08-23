from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path
from typing import TypedDict, cast

from palantum.engine.videouse import probe


class VisualBlock(TypedDict, total=False):
    kind: str
    has_ui: bool
    description: str
    error: str


def _cache_meta(source: Path) -> dict[str, int]:
    stat = source.stat()
    return {"mtime_ns": stat.st_mtime_ns, "size": stat.st_size}


def _fallback(error: str) -> VisualBlock:
    return {
        "kind": "unknown",
        "has_ui": False,
        "description": "Visuelle Klassifizierung nicht verfügbar.",
        "error": error,
    }


def _frame(source: Path, timestamp: float, output: Path) -> None:
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-ss",
            f"{timestamp:.3f}",
            "-i",
            str(source),
            "-frames:v",
            "1",
            "-vf",
            "scale=768:-1",
            str(output),
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )


def _classify(frames: list[Path]) -> VisualBlock:
    """Return a conservative local result until a local vision model is configured.

    OpenAI is intentionally reserved for Whisper transcription.  Extracting the
    frames still verifies that the media can be decoded and leaves a stable cache
    point for a future local classifier without inventing visual semantics.
    """
    if not frames or any(not frame.is_file() or frame.stat().st_size == 0 for frame in frames):
        raise ValueError("visual frame extraction produced no usable frames")
    return {
        "kind": "unknown",
        "has_ui": False,
        "description": (
            "Lokale Frames wurden extrahiert; eine semantische "
            "Bildklassifizierung ist nicht aktiviert."
        ),
    }


def classify_visual(source: Path, edit_dir: Path, frame_count: int = 5) -> VisualBlock:
    """Classify spread video frames, caching the result by source mtime and size."""
    visual_dir = edit_dir / "visual"
    visual_dir.mkdir(parents=True, exist_ok=True)
    cache_path = visual_dir / f"{source.stem}.json"
    metadata = _cache_meta(source)
    if cache_path.exists():
        try:
            cached = json.loads(cache_path.read_text())
            if cached.get("meta") == metadata:
                return cast(VisualBlock, cached["visual"])
        except (OSError, TypeError, ValueError, KeyError):
            pass

    try:
        duration = probe(source).duration_s
        count = max(1, frame_count)
        timestamps = [duration * (index + 1) / (count + 1) for index in range(count)]
        with tempfile.TemporaryDirectory(prefix="palantum-visual-") as directory:
            frames = []
            for index, timestamp in enumerate(timestamps):
                path = Path(directory) / f"frame-{index:02d}.png"
                _frame(source, timestamp, path)
                frames.append(path)
            visual = _classify(frames)
    except Exception as exc:
        visual = _fallback(f"{type(exc).__name__}: {exc}")
    cache_path.write_text(
        json.dumps({"meta": metadata, "visual": visual}, indent=2, ensure_ascii=False)
    )
    return visual
