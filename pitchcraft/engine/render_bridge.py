from __future__ import annotations

import os
import re
import runpy
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any

_WINDOWS_SUBTITLE_PATH = 'str(subtitles_path.resolve()).replace(":", r"\\:")'
_WINDOWS_SUBTITLE_PATH_FIXED = (
    'str(subtitles_path.resolve()).replace("\\\\", "/").replace(":", r"\\:")'
)
_OVERLAY_FILTER = "overlay=enable='between"
_OVERLAY_FILTER_FIXED = "overlay=eof_action=pass:shortest=0:enable='between"
_BUILD_SUBTITLES = "build_master_srt(edl, edit_dir, subs_path)"
_HIDE_SUBTITLES = (
    "build_master_srt(edl, edit_dir, subs_path)\n"
    "            from pitchcraft.engine.render_bridge import "
    "hide_subtitles_during_overlays\n"
    "            hide_subtitles_during_overlays(subs_path, edl.get('overlays') or [])"
)
_SUBTITLE_STYLE = (
    '"FontName=Helvetica,FontSize=18,Bold=1,"\n'
    '    "PrimaryColour=&H00FFFFFF,OutlineColour=&H00000000,BackColour=&H00000000,"\n'
    '    "BorderStyle=1,Outline=2,Shadow=0,"\n'
    '    "Alignment=2,MarginV=90"'
)
_SUBTITLE_STYLE_FIXED = (
    '"FontName=Helvetica,FontSize=14,Bold=1,"\n'
    '    "PrimaryColour=&H00FFFFFF,OutlineColour=&H00000000,BackColour=&H00000000,"\n'
    '    "BorderStyle=1,Outline=1.5,Shadow=0,"\n'
    '    "Alignment=2,MarginV=45"'
)

_SRT_TIME = re.compile(
    r"^(?P<start>\d{2}:\d{2}:\d{2},\d{3})\s+-->\s+"
    r"(?P<end>\d{2}:\d{2}:\d{2},\d{3})$"
)


def _seconds(value: str) -> float:
    hours, minutes, rest = value.split(":")
    seconds, milliseconds = rest.split(",")
    return (
        int(hours) * 3600
        + int(minutes) * 60
        + int(seconds)
        + int(milliseconds) / 1000
    )


def _timestamp(value: float) -> str:
    total_ms = max(0, int(round(value * 1000)))
    hours, remainder = divmod(total_ms, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    seconds, milliseconds = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{milliseconds:03d}"


def _motion_windows(overlays: list[dict[str, Any]]) -> list[tuple[float, float]]:
    windows = sorted(
        (
            max(0.0, float(overlay.get("start_in_output", 0))),
            max(0.0, float(overlay.get("start_in_output", 0)))
            + float(overlay.get("duration", 0)),
        )
        for overlay in overlays
        if float(overlay.get("duration", 0)) > 0
    )
    merged: list[tuple[float, float]] = []
    for start, end in windows:
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def _visible_segments(
    start: float, end: float, windows: list[tuple[float, float]]
) -> list[tuple[float, float]]:
    visible = [(start, end)]
    for hidden_start, hidden_end in windows:
        updated: list[tuple[float, float]] = []
        for segment_start, segment_end in visible:
            if hidden_end <= segment_start or hidden_start >= segment_end:
                updated.append((segment_start, segment_end))
                continue
            if segment_start < hidden_start:
                updated.append((segment_start, min(segment_end, hidden_start)))
            if hidden_end < segment_end:
                updated.append((max(segment_start, hidden_end), segment_end))
        visible = updated
    return [(a, b) for a, b in visible if b - a >= 0.001]


def hide_subtitles_during_overlays(
    subtitles_path: Path, overlays: list[dict[str, Any]]
) -> None:
    """Remove motion time windows from SRT cues, splitting partial overlaps."""
    windows = _motion_windows(overlays)
    if not windows or not subtitles_path.exists():
        return
    text = subtitles_path.read_text(encoding="utf-8").strip()
    if not text:
        return

    visible_cues: list[tuple[float, float, list[str]]] = []
    for block in re.split(r"\r?\n\r?\n+", text):
        lines = block.splitlines()
        if len(lines) < 3:
            raise ValueError("generated subtitle file contains an invalid SRT cue")
        match = _SRT_TIME.fullmatch(lines[1].strip())
        if match is None:
            raise ValueError("generated subtitle file contains an invalid timestamp")
        start = _seconds(match.group("start"))
        end = _seconds(match.group("end"))
        for visible_start, visible_end in _visible_segments(start, end, windows):
            visible_cues.append((visible_start, visible_end, lines[2:]))

    output: list[str] = []
    for index, (start, end, lines) in enumerate(visible_cues, start=1):
        output.extend(
            [
                str(index),
                f"{_timestamp(start)} --> {_timestamp(end)}",
                *lines,
                "",
            ]
        )
    subtitles_path.write_text("\n".join(output), encoding="utf-8")


def patch_render_source(source: str) -> str:
    """Normalize libass paths without modifying the pinned video-use checkout."""
    if _OVERLAY_FILTER_FIXED not in source:
        if _OVERLAY_FILTER not in source:
            raise RuntimeError("pinned video-use overlay expression changed")
        source = source.replace(_OVERLAY_FILTER, _OVERLAY_FILTER_FIXED, 1)
    if _SUBTITLE_STYLE_FIXED not in source:
        if _SUBTITLE_STYLE not in source:
            raise RuntimeError("pinned video-use subtitle style changed")
        source = source.replace(_SUBTITLE_STYLE, _SUBTITLE_STYLE_FIXED, 1)
    if "hide_subtitles_during_overlays(subs_path" not in source:
        if _BUILD_SUBTITLES in source:
            source = source.replace(_BUILD_SUBTITLES, _HIDE_SUBTITLES, 1)
        elif "def build_master_srt(" in source:
            raise RuntimeError("pinned video-use subtitle build call changed")
    if os.name == "nt" and _WINDOWS_SUBTITLE_PATH_FIXED not in source:
        if _WINDOWS_SUBTITLE_PATH not in source:
            raise RuntimeError("pinned video-use subtitle path expression changed")
        source = source.replace(_WINDOWS_SUBTITLE_PATH, _WINDOWS_SUBTITLE_PATH_FIXED, 1)
    return source


def main() -> None:
    if len(sys.argv) < 2:
        raise SystemExit("usage: render_bridge.py <video-use-render.py> [args...]")
    source = Path(sys.argv[1]).resolve()
    arguments = sys.argv[2:]
    with tempfile.TemporaryDirectory(prefix="pitchcraft-video-use-") as directory:
        root = Path(directory)
        render = root / "render.py"
        render.write_text(patch_render_source(source.read_text(encoding="utf-8")), encoding="utf-8")
        grade = source.with_name("grade.py")
        if grade.exists():
            shutil.copy2(grade, root / "grade.py")
        sys.path.insert(0, str(root))
        sys.argv = [str(render), *arguments]
        runpy.run_path(str(render), run_name="__main__")


if __name__ == "__main__":
    main()
