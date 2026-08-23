from __future__ import annotations

import os
import runpy
import shutil
import sys
import tempfile
from pathlib import Path

_WINDOWS_SUBTITLE_PATH = 'str(subtitles_path.resolve()).replace(":", r"\\:")'
_WINDOWS_SUBTITLE_PATH_FIXED = (
    'str(subtitles_path.resolve()).replace("\\\\", "/").replace(":", r"\\:")'
)
_OVERLAY_FILTER = "overlay=enable='between"
_OVERLAY_FILTER_FIXED = "overlay=eof_action=pass:shortest=0:enable='between"
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
    with tempfile.TemporaryDirectory(prefix="palantum-video-use-") as directory:
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
