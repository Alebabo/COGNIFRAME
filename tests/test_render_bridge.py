from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from palantum.engine.render_bridge import (
    hide_subtitles_during_overlays,
    patch_render_source,
)


def test_windows_render_bridge_normalizes_subtitle_filter_paths() -> None:
    source = (
        'SUB_FORCE_STYLE = (\n'
        '    "FontName=Helvetica,FontSize=18,Bold=1,"\n'
        '    "PrimaryColour=&H00FFFFFF,OutlineColour=&H00000000,BackColour=&H00000000,"\n'
        '    "BorderStyle=1,Outline=2,Shadow=0,"\n'
        '    "Alignment=2,MarginV=90"\n'
        ')\n'
        'subs_abs = str(subtitles_path.resolve()).replace(":", r"\\:")\n'
        'filter = "overlay=enable=\'between(t,0,1)\'"'
        "\n            build_master_srt(edl, edit_dir, subs_path)\n"
    )
    with patch("palantum.engine.render_bridge.os.name", "nt"):
        result = patch_render_source(source)

    assert '.replace("\\\\", "/").replace(":", r"\\:")' in result
    assert "overlay=eof_action=pass:shortest=0:enable='between" in result
    assert "FontSize=14" in result
    assert "Outline=1.5" in result
    assert "Alignment=2,MarginV=45" in result
    assert "hide_subtitles_during_overlays(subs_path" in result


def test_motion_windows_are_removed_from_generated_subtitles(tmp_path: Path) -> None:
    subtitles = tmp_path / "master.srt"
    subtitles.write_text(
        "1\n"
        "00:00:00,500 --> 00:00:03,000\n"
        "SPLIT ME\n\n"
        "2\n"
        "00:00:01,200 --> 00:00:02,000\n"
        "HIDE ME\n\n"
        "3\n"
        "00:00:03,000 --> 00:00:04,000\n"
        "KEEP ME\n",
        encoding="utf-8",
    )

    hide_subtitles_during_overlays(
        subtitles,
        [
            {"start_in_output": 1.0, "duration": 1.0},
            {"start_in_output": 1.5, "duration": 1.0},
        ],
    )

    assert subtitles.read_text(encoding="utf-8") == (
        "1\n"
        "00:00:00,500 --> 00:00:01,000\n"
        "SPLIT ME\n\n"
        "2\n"
        "00:00:02,500 --> 00:00:03,000\n"
        "SPLIT ME\n\n"
        "3\n"
        "00:00:03,000 --> 00:00:04,000\n"
        "KEEP ME\n"
    )


def test_subtitle_file_is_removed_when_motion_covers_all_cues(tmp_path: Path) -> None:
    subtitles = tmp_path / "master.srt"
    subtitles.write_text(
        "1\n00:00:00,000 --> 00:00:02,000\nHIDE ME\n\n"
        "2\n00:00:02,000 --> 00:00:04,000\nHIDE ME TOO\n",
        encoding="utf-8",
    )

    hide_subtitles_during_overlays(
        subtitles, [{"start_in_output": 0.0, "duration": 5.0}]
    )

    assert not subtitles.exists()


def test_subtitles_are_unchanged_without_motion(tmp_path: Path) -> None:
    subtitles = tmp_path / "master.srt"
    original = "1\n00:00:00,000 --> 00:00:01,000\nKEEP ME\n"
    subtitles.write_text(original, encoding="utf-8")

    hide_subtitles_during_overlays(subtitles, [])

    assert subtitles.read_text(encoding="utf-8") == original


def test_windows_render_bridge_fails_if_pinned_expression_drifted() -> None:
    with (
        patch("palantum.engine.render_bridge.os.name", "nt"),
        pytest.raises(RuntimeError, match="expression changed"),
    ):
        patch_render_source('subs_abs = str(subtitles_path.resolve()).replace(":", r"\\:")')


def test_render_bridge_fails_if_pinned_subtitle_style_drifted() -> None:
    source = (
        'subs_abs = str(subtitles_path.resolve()).replace("\\\\", "/").replace(":", r"\\:")\n'
        'filter = "overlay=eof_action=pass:shortest=0:enable=\'between(t,0,1)\'"'
    )
    with pytest.raises(RuntimeError, match="subtitle style changed"):
        patch_render_source(source)
