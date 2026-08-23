from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

from pitchcraft.engine import visual


def test_visual_classification_stays_local_and_conservative(tmp_path: Path) -> None:
    frame = tmp_path / "frame.png"
    frame.write_bytes(b"png")

    result = visual._classify([frame])

    assert result == {
        "kind": "unknown",
        "has_ui": False,
        "description": (
            "Local frames were extracted; semantic "
            "image classification is not enabled."
        ),
    }


def test_visual_failure_is_cached_as_unknown(tmp_path: Path, monkeypatch: Any) -> None:
    source = tmp_path / "take.mp4"
    source.write_bytes(b"source")
    edit = tmp_path / "edit"
    monkeypatch.setattr(visual, "probe", lambda _: SimpleNamespace(duration_s=1.0))
    monkeypatch.setattr(visual, "_frame", lambda _source, _time, output: output.write_bytes(b"png"))
    monkeypatch.setattr(
        visual, "_classify", lambda _frames: (_ for _ in ()).throw(RuntimeError("offline"))
    )

    result = visual.classify_visual(source, edit)

    assert result["kind"] == "unknown"
    assert result["has_ui"] is False
    assert "offline" in result["error"]
    assert (edit / "visual/take.json").exists()
