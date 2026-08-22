from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from pathlib import Path

from palantum.export import export_project


def test_export_writes_four_spurs_and_matching_duration(tmp_path: Path) -> None:
    edit = tmp_path / "edit"
    clips = edit / "clips_graded"
    visual = edit / "visual"
    clips.mkdir(parents=True)
    visual.mkdir()
    (clips / "seg_00_take_a.mp4").write_bytes(b"clip-a")
    (clips / "seg_01_take_b.mp4").write_bytes(b"clip-b")
    (visual / "take_b.json").write_text(
        json.dumps({"meta": {}, "visual": {"kind": "screen_recording", "has_ui": True}})
    )
    (edit / "coverage.json").write_text("{}")
    (edit / "master.srt").write_text("1\n00:00:00,000 --> 00:00:00,500\nHELLO\n")
    (edit / "edl.json").write_text(
        json.dumps(
            {
                "sources": {"take_a": "take_a.mp4", "take_b": "take_b.mp4"},
                "ranges": [
                    {"source": "take_a", "start": 0, "end": 2, "beat": "HOOK", "quote": "a"},
                    {"source": "take_b", "start": 1, "end": 4, "beat": "DEMO", "quote": "b"},
                ],
            }
        )
    )

    package = export_project(edit)
    root = ET.parse(package / "timeline.fcpxml").getroot()
    sequence = root.find("./library/event/project/sequence")
    assert sequence is not None
    assert {node.attrib["value"] for node in sequence.findall("./metadata/md")} == {
        "aroll",
        "broll",
        "graphics",
        "subtitles",
    }
    assert sequence.attrib["duration"] == "120/24s"
    assert (package / "media/aroll").joinpath("aroll_00_seg_00_take_a.mp4").exists()
    assert (package / "media/broll").joinpath("broll_01_seg_01_take_b.mp4").exists()
    assert json.loads((package / "timeline.otio").read_text())["metadata"]["duration_frames"] == 120
