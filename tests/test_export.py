from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from pathlib import Path
from types import SimpleNamespace

from palantum.export import package


def test_export_writes_fps_aware_zero_based_timelines(tmp_path: Path, monkeypatch: object) -> None:
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
    (edit / "final.mp4").write_bytes(b"render")
    (edit / "master.srt").write_text("1\n00:00:00,000 --> 00:00:00,500\nHELLO\n")
    (edit / "edl.json").write_text(
        json.dumps(
            {
                "sources": {"take_a": "take_a.mp4", "take_b": "take_b.mp4"},
                "ranges": [
                    {"source": "take_a", "start": 3, "end": 5, "beat": "HOOK", "quote": "a"},
                    {"source": "take_b", "start": 8, "end": 11, "beat": "DEMO", "quote": "b"},
                ],
            }
        )
    )

    monkeypatch.setattr(package, "probe", lambda _: SimpleNamespace(fps=25.0))
    monkeypatch.setattr(
        package,
        "_extract_audio",
        lambda _source, destination: destination.write_bytes(b"audio"),
    )
    output = package.export_project(edit)
    root = ET.parse(output / "timeline.fcpxml").getroot()
    sequence = root.find("./library/event/project/sequence")
    assert sequence is not None
    assert {
        node.attrib["value"]
        for node in sequence.findall("./metadata/md")
        if node.attrib["value"] in {"aroll", "broll", "graphics", "subtitles"}
    } == {
        "aroll",
        "broll",
        "graphics",
        "subtitles",
    }
    assert sequence.attrib["duration"] == "125/25s"
    assert root.find("./resources/format").attrib["frameDuration"] == "1/25s"
    assert all(node.attrib["start"] == "0/25s" for node in root.findall("./resources/asset"))
    assert all(node.attrib["start"] == "0/25s" for node in root.findall(".//asset-clip"))
    assert (output / "media/aroll").joinpath("aroll_00_seg_00_take_a.mp4").exists()
    assert (output / "media/broll").joinpath("broll_01_seg_01_take_b.mp4").exists()
    assert (output / "media/audio/voice_00_seg_00_take_a.wav").exists()
    assert (output / "media/audio/voice_01_seg_01_take_b.wav").exists()
    otio = json.loads((output / "timeline.otio").read_text())
    assert otio["metadata"]["duration_frames"] == 125
    assert otio["metadata"]["fps"] == 25.0
    tracks = otio["tracks"]["children"]
    voice = next(track for track in tracks if track["name"] == "A1 voice")
    assert voice["children"][0]["source_range"]["start_time"]["value"] == 0
    assert voice["children"][1]["timeline_start"] == 50
    edl_lines = (output / "timeline.edl").read_text().splitlines()
    events = [line for line in edl_lines if line[:3].isdigit()]
    assert all(line.split()[4] == "00:00:00:00" for line in events)
    assert events[1].split()[6:8] == ["00:00:02:00", "00:00:05:00"]
