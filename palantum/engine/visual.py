from __future__ import annotations

import base64
import json
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any, TypedDict, cast

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
    from openai import OpenAI

    content: list[dict[str, object]] = [
        {
            "type": "text",
            "text": (
                "Classify the video frames as one of talking_head, screen_recording, "
                "b_roll, slate, or unknown. Return has_ui=true only when a product "
                "interface is visibly present. Give one concise sentence describing "
                "the frames."
            ),
        }
    ]
    for frame in frames:
        encoded = base64.b64encode(frame.read_bytes()).decode("ascii")
        content.append(
            {
                "type": "image_url",
                "image_url": {"url": f"data:image/png;base64,{encoded}"},
            }
        )
    client: Any = OpenAI(api_key=os.environ["OPENAI_API_KEY"])
    response = client.chat.completions.create(
        model=os.getenv("PALANTUM_VISION_MODEL", "gpt-4o-mini"),
        messages=[{"role": "user", "content": content}],
        temperature=0,
        response_format={
            "type": "json_schema",
            "json_schema": {
                "name": "palantum_visual_classification",
                "strict": True,
                "schema": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["kind", "has_ui", "description"],
                    "properties": {
                        "kind": {
                            "type": "string",
                            "enum": [
                                "talking_head",
                                "screen_recording",
                                "b_roll",
                                "slate",
                                "unknown",
                            ],
                        },
                        "has_ui": {"type": "boolean"},
                        "description": {"type": "string"},
                    },
                },
            },
        },
    )
    message = response.choices[0].message.content
    if not message:
        raise ValueError("vision model returned an empty response")
    result = json.loads(message)
    if not isinstance(result, dict):
        raise ValueError("vision model returned a non-object response")
    return cast(VisualBlock, result)


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
