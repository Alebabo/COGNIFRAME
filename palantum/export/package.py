from __future__ import annotations

import json
import os
import shutil
import subprocess
import xml.etree.ElementTree as ET
from fractions import Fraction
from pathlib import Path
from typing import Any

from palantum.engine.videouse import probe

SPURS = {
    "aroll": ("V1", 1),
    "broll": ("V2", 2),
    "graphics": ("V3", 3),
    "subtitles": ("V4", 4),
}
AUDIO_SPURS = {
    "voice": ("A1", 5),
    "music": ("A2", 6),
}


def _fps_fraction(fps: float) -> Fraction:
    return Fraction(fps).limit_denominator(1001)


def _frames(seconds: float, fps: float) -> int:
    return round(seconds * fps)


def _time(seconds: float, fps: float) -> str:
    rate = _fps_fraction(fps)
    return f"{_frames(seconds, fps) * rate.denominator}/{rate.numerator}s"


def _nominal_fps(fps: float) -> int:
    return max(1, round(fps))


def _hardlink_or_copy(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.link(source, destination)
    except OSError:
        shutil.copy2(source, destination)


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
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


def _extract_audio(source: Path, destination: Path) -> None:
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-i",
            str(source),
            "-map",
            "0:a:0",
            "-vn",
            "-ac",
            "1",
            "-ar",
            "48000",
            "-c:a",
            "pcm_s16le",
            str(destination),
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )


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
        audio_destination = (
            package_dir / "media" / "audio" / f"voice_{index:02d}_{source_clip.stem}.wav"
        )
        _extract_audio(source_clip, audio_destination)
        duration = float(item["end"]) - float(item["start"])
        exported.append(
            {
                "index": index,
                "source": source,
                "start": 0.0,
                "end": duration,
                "source_start": float(item["start"]),
                "beat": str(item.get("beat", "")),
                "quote": str(item.get("quote", "")),
                "clip": destination.relative_to(package_dir).as_posix(),
                "bucket": bucket,
                "lane": SPURS[bucket][1],
                "audio_clip": audio_destination.relative_to(package_dir).as_posix(),
                "audio_lane": AUDIO_SPURS["voice"][1],
            }
        )
    return exported


def _resolve_overlay(edit_dir: Path, value: str) -> Path:
    path = Path(value)
    if path.is_absolute():
        return path
    for candidate in (edit_dir / path, edit_dir.parent / path):
        if candidate.exists():
            return candidate
    raise FileNotFoundError(f"overlay does not exist: {value}")


def _copy_graphics(
    edit_dir: Path, package_dir: Path, edl: dict[str, Any]
) -> list[dict[str, Any]]:
    graphics: list[dict[str, Any]] = []
    for index, item in enumerate(edl.get("overlays", [])):
        source = _resolve_overlay(edit_dir, str(item["file"]))
        destination = package_dir / "media" / "graphics" / f"graphic_{index:02d}_{source.name}"
        _hardlink_or_copy(source, destination)
        graphics.append(
            {
                "index": index,
                "name": str(item.get("template_id", item.get("beat", source.stem))),
                "start": float(item["start_in_output"]),
                "duration": float(item["duration"]),
                "clip": destination.relative_to(package_dir).as_posix(),
            }
        )
    return graphics


def _write_fcpxml(
    package_dir: Path,
    clips: list[dict[str, Any]],
    graphics: list[dict[str, Any]],
    srt: Path,
    fps: float,
) -> None:
    root = ET.Element("fcpxml", {"version": "1.10"})
    resources = ET.SubElement(root, "resources")
    rate = _fps_fraction(fps)
    ET.SubElement(
        resources,
        "format",
        {
            "id": "r1",
            "name": f"FFVideoFormat1080x1920p{fps:g}",
            "frameDuration": f"{rate.denominator}/{rate.numerator}s",
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
                "start": _time(clip["start"], fps),
                "duration": _time(clip["end"] - clip["start"], fps),
                "hasVideo": "1",
                "hasAudio": "1",
                "format": "r1",
            },
        )
        ET.SubElement(
            resources,
            "asset",
            {
                "id": f"audio-{index}",
                "name": Path(str(clip["audio_clip"])).name,
                "src": f"file://{package_dir / clip['audio_clip']}",
                "start": _time(0, fps),
                "duration": _time(clip["end"] - clip["start"], fps),
                "hasVideo": "0",
                "hasAudio": "1",
            },
        )
    for graphic in graphics:
        ET.SubElement(
            resources,
            "asset",
            {
                "id": f"graphic-{graphic['index']}",
                "name": Path(str(graphic["clip"])).name,
                "src": f"file://{package_dir / graphic['clip']}",
                "start": _time(0, fps),
                "duration": _time(float(graphic["duration"]), fps),
                "hasVideo": "1",
                "hasAudio": "0",
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
        {
            "format": "r1",
            "duration": _time(sum(c["end"] - c["start"] for c in clips), fps),
        },
    )
    metadata = ET.SubElement(sequence, "metadata")
    for name, (spur, lane) in SPURS.items():
        ET.SubElement(metadata, "md", {"key": f"palantum:{spur}", "value": name, "lane": str(lane)})
    for name, (spur, lane) in AUDIO_SPURS.items():
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
                "offset": _time(offset, fps),
                "start": _time(0, fps),
                "duration": _time(duration, fps),
                "lane": str(clip["lane"]),
            },
        )
        ET.SubElement(
            spine,
            "asset-clip",
            {
                "name": f"A1 voice {clip['beat']}",
                "ref": f"audio-{index}",
                "offset": _time(offset, fps),
                "start": _time(0, fps),
                "duration": _time(duration, fps),
                "lane": str(AUDIO_SPURS["voice"][1]),
            },
        )
        offset += duration
    for graphic in graphics:
        ET.SubElement(
            spine,
            "asset-clip",
            {
                "name": str(graphic["name"]),
                "ref": f"graphic-{graphic['index']}",
                "offset": _time(float(graphic["start"]), fps),
                "start": _time(0, fps),
                "duration": _time(float(graphic["duration"]), fps),
                "lane": str(SPURS["graphics"][1]),
            },
        )
    if srt.exists():
        for _index, (start, end, text) in enumerate(_parse_srt(srt)):
            ET.SubElement(
                spine,
                "title",
                {
                    "name": text,
                    "ref": "title-basic",
                    "lane": str(SPURS["subtitles"][1]),
                    "offset": _time(start, fps),
                    "duration": _time(end - start, fps),
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


def _write_otio(
    package_dir: Path,
    clips: list[dict[str, Any]],
    graphics: list[dict[str, Any]],
    srt: Path,
    fps: float,
) -> None:
    tracks: dict[str, list[dict[str, Any]]] = {
        name: []
        for name in (
            "V1 A-roll",
            "V2 B-roll",
            "V3 graphics",
            "V4 subtitles",
            "A1 voice",
            "A2 music",
        )
    }
    offset = 0
    for clip in clips:
        duration = _frames(clip["end"] - clip["start"], fps)
        track = "V1 A-roll" if clip["bucket"] == "aroll" else "V2 B-roll"
        video_clip = {
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
                    "value": 0,
                    "rate": fps,
                },
                "duration": {"OTIO_SCHEMA": "RationalTime.1", "value": duration, "rate": fps},
            },
            "timeline_start": offset,
        }
        tracks[track].append(video_clip)
        tracks["A1 voice"].append(
            {
                "OTIO_SCHEMA": "Clip.2",
                "name": f"Voice {clip['beat']}",
                "media_reference": {
                    "OTIO_SCHEMA": "ExternalReference.1",
                    "target_url": clip["audio_clip"],
                },
                "source_range": {
                    "OTIO_SCHEMA": "TimeRange.1",
                    "start_time": {
                        "OTIO_SCHEMA": "RationalTime.1",
                        "value": 0,
                        "rate": fps,
                    },
                    "duration": {
                        "OTIO_SCHEMA": "RationalTime.1",
                        "value": duration,
                        "rate": fps,
                    },
                },
                "timeline_start": offset,
            }
        )
        offset += duration
    for graphic in graphics:
        duration = _frames(float(graphic["duration"]), fps)
        tracks["V3 graphics"].append(
            {
                "OTIO_SCHEMA": "Clip.2",
                "name": graphic["name"],
                "media_reference": {
                    "OTIO_SCHEMA": "ExternalReference.1",
                    "target_url": graphic["clip"],
                },
                "source_range": {
                    "OTIO_SCHEMA": "TimeRange.1",
                    "start_time": {
                        "OTIO_SCHEMA": "RationalTime.1",
                        "value": 0,
                        "rate": fps,
                    },
                    "duration": {
                        "OTIO_SCHEMA": "RationalTime.1",
                        "value": duration,
                        "rate": fps,
                    },
                },
                "timeline_start": _frames(float(graphic["start"]), fps),
            }
        )
    payload = {
        "OTIO_SCHEMA": "Timeline.1",
        "name": package_dir.name,
        "global_start_time": {"OTIO_SCHEMA": "RationalTime.1", "value": 0, "rate": fps},
        "tracks": {
            "OTIO_SCHEMA": "Stack.1",
            "children": [
                {
                    "OTIO_SCHEMA": "Track.1",
                    "name": name,
                    "kind": "Audio" if name.startswith("A") else "Video",
                    "children": children,
                }
                for name, children in tracks.items()
            ],
        },
        "metadata": {
            "subtitle_source": "subtitles.srt",
            "duration_frames": offset,
            "fps": fps,
        },
    }
    (package_dir / "timeline.otio").write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
    )


def _write_edl(package_dir: Path, clips: list[dict[str, Any]], fps: float) -> None:
    lines = ["TITLE: PALANTUM EXPORT", "FCM: NON-DROP FRAME", ""]
    record = 0
    for index, clip in enumerate(clips, start=1):
        duration = _frames(clip["end"] - clip["start"], fps)
        source_in = 0
        source_out = source_in + duration
        record_out = record + duration
        lines.append(
            f"{index:03d}  {clip['source'][:8]:8s} V     C        "
            f"{_tc(source_in, fps)} {_tc(source_out, fps)} "
            f"{_tc(record, fps)} {_tc(record_out, fps)}"
        )
        lines.append(f"* FROM CLIP NAME: {clip['beat']} / {clip['quote']}")
        record = record_out
    (package_dir / "timeline.edl").write_text("\n".join(lines) + "\n")


def _tc(frame: int, fps: float) -> str:
    nominal = _nominal_fps(fps)
    hours, remainder = divmod(frame, nominal * 3600)
    minutes, remainder = divmod(remainder, nominal * 60)
    seconds, frames = divmod(remainder, nominal)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}:{frames:02d}"


def export_project(edit_dir: Path, output_parent: Path | None = None) -> Path:
    """Build a separable NLE package from existing video-use intermediates."""
    edl = _read_json(edit_dir / "edl.json")
    rendered = edit_dir / "final.mp4"
    if not rendered.exists():
        raise FileNotFoundError(f"rendered output is required for export: {rendered}")
    fps = probe(rendered).fps
    if fps <= 0:
        raise ValueError(f"rendered output has invalid frame rate: {fps}")
    project_name = edit_dir.parent.name
    parent = output_parent or edit_dir.parent
    package_dir = parent / f"palantum_export_{project_name}"
    if package_dir.exists():
        shutil.rmtree(package_dir)
    for directory in ("aroll", "broll", "graphics", "audio"):
        (package_dir / "media" / directory).mkdir(parents=True, exist_ok=True)
    clips = _copy_media(edit_dir, package_dir, edl)
    graphics = _copy_graphics(edit_dir, package_dir, edl)
    srt = edit_dir / "master.srt"
    if srt.exists():
        shutil.copy2(srt, package_dir / "subtitles.srt")
    else:
        (package_dir / "subtitles.srt").write_text("")
    shutil.copy2(edit_dir / "coverage.json", package_dir / "coverage.json")
    _write_fcpxml(package_dir, clips, graphics, srt, fps)
    _write_otio(package_dir, clips, graphics, srt, fps)
    _write_edl(package_dir, clips, fps)
    (package_dir / "README.txt").write_text(
        "Dieses Paket ist der auftrennbare Palantum-Schnitt für "
        f"{project_name}.\n\n"
        "Öffne timeline.fcpxml in DaVinci Resolve oder Final Cut Pro. "
        "timeline.otio und timeline.edl sind Fallbacks für andere NLEs.\n"
        "V1 enthält A-Roll, V2 B-Roll, V3 Grafiken und V4 Untertitel als Titel.\n"
        "A1 enthält die separate Sprachspur; A2 bleibt für Musik reserviert.\n"
        "Die Untertitel bleiben editierbare Titel und sind nicht in das Video eingebrannt.\n"
        "CapCut wird nicht unterstützt.\n"
    )
    return package_dir
