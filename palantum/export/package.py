from __future__ import annotations

import json
import os
import shutil
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

FPS = 24
SPURS = {
    "aroll": ("V1", 1),
    "broll": ("V2", 2),
    "graphics": ("V3", 3),
    "subtitles": ("V4", 4),
}


def _frames(seconds: float) -> int:
    return round(seconds * FPS)


def _time(seconds: float) -> str:
    return f"{_frames(seconds)}/{FPS}s"


def _hardlink_or_copy(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.link(source, destination)
    except OSError:
        shutil.copy2(source, destination)


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object in {path}")
    return value


def _visual_kind(edit_dir: Path, source: str) -> str:
    path = edit_dir / "visual" / f"{source}.json"
    if not path.exists():
        return "unknown"
    try:
        return str(_read_json(path)["visual"]["kind"])
    except (KeyError, TypeError, ValueError):
        return "unknown"


def _copy_media(edit_dir: Path, package_dir: Path, edl: dict[str, Any]) -> list[dict[str, Any]]:
    clips_dir = edit_dir / "clips_graded"
    exported: list[dict[str, Any]] = []
    for index, item in enumerate(edl.get("ranges", [])):
        source = str(item["source"])
        source_clip = sorted(clips_dir.glob(f"seg_{index:02d}_*"))[0]
        bucket = (
            "broll" if _visual_kind(edit_dir, source) in {"screen_recording", "b_roll"} else "aroll"
        )
        name = f"{bucket}_{index:02d}_{source_clip.name}"
        destination = package_dir / "media" / bucket / name
        _hardlink_or_copy(source_clip, destination)
        exported.append(
            {
                "index": index,
                "source": source,
                "start": float(item["start"]),
                "end": float(item["end"]),
                "beat": str(item.get("beat", "")),
                "quote": str(item.get("quote", "")),
                "clip": destination.relative_to(package_dir).as_posix(),
                "bucket": bucket,
                "lane": SPURS[bucket][1],
            }
        )
    return exported


def _write_fcpxml(package_dir: Path, clips: list[dict[str, Any]], srt: Path) -> None:
    root = ET.Element("fcpxml", {"version": "1.10"})
    resources = ET.SubElement(root, "resources")
    ET.SubElement(
        resources,
        "format",
        {
            "id": "r1",
            "name": "FFVideoFormat1080x1920p24",
            "frameDuration": "1/24s",
            "width": "1080",
            "height": "1920",
        },
    )
    for index, clip in enumerate(clips):
        ET.SubElement(
            resources,
            "asset",
            {
                "id": f"asset-{index}",
                "name": Path(str(clip["clip"])).name,
                "src": f"file://{package_dir / clip['clip']}",
                "start": _time(clip["start"]),
                "duration": _time(clip["end"] - clip["start"]),
                "hasVideo": "1",
                "hasAudio": "1",
                "format": "r1",
            },
        )
    title_effect = ET.SubElement(
        resources,
        "effect",
        {"id": "title-basic", "name": "Basic Title", "uid": ".../Titles.localized/"},
    )
    title_effect.set("role", "V4 subtitles")
    library = ET.SubElement(root, "library")
    event = ET.SubElement(library, "event", {"name": package_dir.name})
    project = ET.SubElement(event, "project", {"name": package_dir.name})
    sequence = ET.SubElement(
        project,
        "sequence",
        {"format": "r1", "duration": _time(sum(c["end"] - c["start"] for c in clips))},
    )
    metadata = ET.SubElement(sequence, "metadata")
    for name, (spur, lane) in SPURS.items():
        ET.SubElement(metadata, "md", {"key": f"palantum:{spur}", "value": name, "lane": str(lane)})
    spine = ET.SubElement(sequence, "spine")
    offset = 0.0
    for index, clip in enumerate(clips):
        duration = clip["end"] - clip["start"]
        ET.SubElement(
            spine,
            "asset-clip",
            {
                "name": clip["beat"],
                "ref": f"asset-{index}",
                "offset": _time(offset),
                "start": _time(clip["start"]),
                "duration": _time(duration),
                "lane": str(clip["lane"]),
            },
        )
        offset += duration
    if srt.exists():
        for _index, (start, end, text) in enumerate(_parse_srt(srt)):
            ET.SubElement(
                spine,
                "title",
                {
                    "name": text,
                    "ref": "title-basic",
                    "lane": str(SPURS["subtitles"][1]),
                    "offset": _time(start),
                    "duration": _time(end - start),
                },
            )
    ET.indent(root, space="  ")
    ET.ElementTree(root).write(
        package_dir / "timeline.fcpxml", encoding="utf-8", xml_declaration=True
    )


def _parse_srt(path: Path) -> list[tuple[float, float, str]]:
    blocks = path.read_text(encoding="utf-8").strip().split("\n\n")
    result: list[tuple[float, float, str]] = []
    for block in blocks:
        lines = block.splitlines()
        if len(lines) < 3 or " --> " not in lines[1]:
            continue
        start, end = lines[1].split(" --> ", 1)
        result.append((_parse_timestamp(start), _parse_timestamp(end), " ".join(lines[2:])))
    return result


def _parse_timestamp(value: str) -> float:
    hours, minutes, rest = value.replace(",", ".").split(":")
    seconds = float(rest)
    return int(hours) * 3600 + int(minutes) * 60 + seconds


def _write_otio(package_dir: Path, clips: list[dict[str, Any]], srt: Path) -> None:
    tracks: dict[str, list[dict[str, Any]]] = {
        name: [] for name in ("V1 A-roll", "V2 B-roll", "V3 graphics", "V4 subtitles")
    }
    offset = 0
    for clip in clips:
        duration = _frames(clip["end"] - clip["start"])
        track = "V1 A-roll" if clip["bucket"] == "aroll" else "V2 B-roll"
        tracks[track].append(
            {
                "OTIO_SCHEMA": "Clip.2",
                "name": clip["beat"],
                "media_reference": {
                    "OTIO_SCHEMA": "ExternalReference.1",
                    "target_url": clip["clip"],
                },
                "source_range": {
                    "OTIO_SCHEMA": "TimeRange.1",
                    "start_time": {
                        "OTIO_SCHEMA": "RationalTime.1",
                        "value": _frames(clip["start"]),
                        "rate": FPS,
                    },
                    "duration": {"OTIO_SCHEMA": "RationalTime.1", "value": duration, "rate": FPS},
                },
                "timeline_start": offset,
            }
        )
        offset += duration
    payload = {
        "OTIO_SCHEMA": "Timeline.1",
        "name": package_dir.name,
        "global_start_time": {"OTIO_SCHEMA": "RationalTime.1", "value": 0, "rate": FPS},
        "tracks": {
            "OTIO_SCHEMA": "Stack.1",
            "children": [
                {"OTIO_SCHEMA": "Track.1", "name": name, "kind": "Video", "children": children}
                for name, children in tracks.items()
            ],
        },
        "metadata": {"subtitle_source": "subtitles.srt", "duration_frames": offset},
    }
    (package_dir / "timeline.otio").write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
    )


def _write_edl(package_dir: Path, clips: list[dict[str, Any]]) -> None:
    lines = ["TITLE: PALANTUM EXPORT", "FCM: NON-DROP FRAME", ""]
    record = 0
    for index, clip in enumerate(clips, start=1):
        duration = _frames(clip["end"] - clip["start"])
        source_in = _frames(clip["start"])
        source_out = source_in + duration
        record_out = record + duration
        lines.append(
            f"{index:03d}  {clip['source'][:8]:8s} V     C        "
            f"{_tc(source_in)} {_tc(source_out)} {_tc(record)} {_tc(record_out)}"
        )
        lines.append(f"* FROM CLIP NAME: {clip['beat']} / {clip['quote']}")
        record = record_out
    (package_dir / "timeline.edl").write_text("\n".join(lines) + "\n")


def _tc(frame: int) -> str:
    hours, remainder = divmod(frame, FPS * 3600)
    minutes, remainder = divmod(remainder, FPS * 60)
    seconds, frames = divmod(remainder, FPS)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}:{frames:02d}"


def export_project(edit_dir: Path, output_parent: Path | None = None) -> Path:
    """Build a separable NLE package from existing video-use intermediates."""
    edl = _read_json(edit_dir / "edl.json")
    project_name = edit_dir.parent.name
    parent = output_parent or edit_dir.parent
    package_dir = parent / f"palantum_export_{project_name}"
    if package_dir.exists():
        shutil.rmtree(package_dir)
    for directory in ("aroll", "broll", "graphics", "audio"):
        (package_dir / "media" / directory).mkdir(parents=True, exist_ok=True)
    clips = _copy_media(edit_dir, package_dir, edl)
    srt = edit_dir / "master.srt"
    if srt.exists():
        shutil.copy2(srt, package_dir / "subtitles.srt")
    else:
        (package_dir / "subtitles.srt").write_text("")
    shutil.copy2(edit_dir / "coverage.json", package_dir / "coverage.json")
    _write_fcpxml(package_dir, clips, srt)
    _write_otio(package_dir, clips, srt)
    _write_edl(package_dir, clips)
    (package_dir / "README.txt").write_text(
        "Dieses Paket ist der auftrennbare Palantum-Schnitt für "
        f"{project_name}.\n\n"
        "Öffne timeline.fcpxml in DaVinci Resolve oder Final Cut Pro. "
        "timeline.otio und timeline.edl sind Fallbacks für andere NLEs.\n"
        "V1 enthält A-Roll, V2 B-Roll, V3 Grafiken und V4 Untertitel als Titel.\n"
        "Die Untertitel bleiben editierbare Titel und sind nicht in das Video eingebrannt.\n"
        "CapCut wird nicht unterstützt.\n"
    )
    return package_dir
