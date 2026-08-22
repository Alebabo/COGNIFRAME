from __future__ import annotations

from unittest.mock import patch

import pytest

from palantum.engine.render_bridge import patch_render_source


def test_windows_render_bridge_normalizes_subtitle_filter_paths() -> None:
    source = (
        'subs_abs = str(subtitles_path.resolve()).replace(":", r"\\:")\n'
        'filter = "overlay=enable=\'between(t,0,1)\'"'
    )
    with patch("palantum.engine.render_bridge.os.name", "nt"):
        result = patch_render_source(source)

    assert '.replace("\\\\", "/").replace(":", r"\\:")' in result
    assert "overlay=eof_action=pass:shortest=0:enable='between" in result


def test_windows_render_bridge_fails_if_pinned_expression_drifted() -> None:
    with (
        patch("palantum.engine.render_bridge.os.name", "nt"),
        pytest.raises(RuntimeError, match="expression changed"),
    ):
        patch_render_source('subs_abs = str(subtitles_path.resolve()).replace(":", r"\\:")')
