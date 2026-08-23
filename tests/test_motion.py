from __future__ import annotations

import json
import os
import zipfile
from pathlib import Path

import pytest

from pitchcraft.motion.catalog import CURATED_SCENES, build_scene_catalog
from pitchcraft.motion.harness import build_render_command, materialize_scene


def _pack(root: Path, *, broken: str | None = None) -> Path:
    index = []
    for scene_id in CURATED_SCENES:
        component_name = "".join(part.title() for part in scene_id.split("-"))
        folder = root / scene_id
        folder.mkdir(parents=True)
        source = (
            "import {AbsoluteFill} from 'remotion';\n"
            f"export const {component_name}: React.FC<{{title?: string}}> = "
            "({title = 'Default'}) => <AbsoluteFill>{title}</AbsoluteFill>;\n"
        )
        if scene_id == broken:
            source += "const invalid = <div>{isDark && }</div>;\n"
        (folder / "Composition.tsx").write_text(source)
        metadata = {
            "id": scene_id,
            "name": scene_id,
            "componentName": component_name,
            "description": "fixture",
            "duration": 3,
            "category": "Data",
            "tags": [],
            "fps": 30,
            "durationInFrames": 90,
            "width": 1920,
            "height": 1080,
            "isFree": True,
            "previewVideo": f"/videos/{scene_id}.mp4",
            "editableProps": [
                {"key": "title", "type": "string", "label": "Title", "defaultValue": "Default"}
            ],
        }
        (folder / "metadata.json").write_text(json.dumps(metadata))
        index.append(
            {
                "id": scene_id,
                "name": scene_id,
                "componentName": component_name,
                "category": "Data",
                "duration": 3,
                "durationInFrames": 90,
                "fps": 30,
                "file": f"{scene_id}/Composition.tsx",
            }
        )
    (root / "index.json").write_text(json.dumps(index))
    return root


def _zip(source: Path, output: Path) -> Path:
    with zipfile.ZipFile(output, "w") as archive:
        for path in sorted(source.rglob("*")):
            if path.is_file():
                archive.write(path, f"pack/{path.relative_to(source).as_posix()}")
    return output


def test_catalog_scans_folder_and_reuses_hash_cache(tmp_path: Path) -> None:
    source = _pack(tmp_path / "pack")
    output = tmp_path / "edit/scene-catalog.json"
    first = build_scene_catalog(source, output)
    before = output.stat().st_mtime_ns
    second = build_scene_catalog(source, output)

    assert first == second
    assert output.stat().st_mtime_ns == before
    assert len(first["scenes"]) == 8
    assert first["source"]["kind"] == "directory"
    assert len(first["source"]["sha256"]) == 64
    assert first["source_sha256"] == first["source"]["sha256"]
    assert first["scenes"][0]["status"] == "ok"
    assert first["scenes"][0]["broken_reason"] is None
    assert first["scenes"][0]["slots"][0]["default"] == "Default"
    assert first["scenes"][0]["slots"][0]["max_chars"] >= len("Default")
    assert first["scenes"][0]["presentation"] == "inset"
    assert first["scenes"][0]["content_kind"] == "structured"
    assert first["scenes"][0]["min_visible_s"] == 4.5
    assert first["scenes"][0]["requires_numeric_claim"] is True
    assert next(scene for scene in first["scenes"] if scene["id"] == "brand-statement")[
        "content_kind"
    ] == "text"


def test_catalog_scans_prefixed_zip_and_marks_broken_source(tmp_path: Path) -> None:
    source = _pack(tmp_path / "pack", broken="flowchart")
    archive = _zip(source, tmp_path / "templates.zip")
    catalog = build_scene_catalog(archive, tmp_path / "catalog.json")
    scenes = {scene["id"]: scene for scene in catalog["scenes"]}

    assert catalog["source"]["kind"] == "zip"
    assert scenes["flowchart"]["status"] == "broken"
    assert "conditional" in scenes["flowchart"]["broken_reason"]
    assert scenes["flowchart"]["static_parse"]["status"] == "broken"
    assert "conditional" in scenes["flowchart"]["static_parse"]["reason"]


def test_materialize_creates_isolated_harness_and_validates_props(tmp_path: Path) -> None:
    archive = _zip(_pack(tmp_path / "pack"), tmp_path / "templates.zip")
    edit = tmp_path / "project/edit"
    slot = materialize_scene(archive, "hero-stat-callout", edit, {"title": "Traction"})

    assert slot == edit / "animations/slot_hero-stat-callout"
    assert (slot / "src/Composition.tsx").exists()
    assert "HeroStatCallout" in (slot / "src/Root.tsx").read_text()
    assert json.loads((slot / "props.json").read_text()) == {"title": "Traction"}
    assert json.loads((slot / "pitchcraft-slot.json").read_text())["source_sha256"]
    assert json.loads((slot / "pitchcraft-slot.json").read_text())["presentation"] == "inset"
    command = build_render_command(slot)
    if os.name == "nt":
        assert command[1:5] == ["/d", "/s", "/c", "npx"]
    else:
        assert command[0] == "npx"
    assert "--prores-profile=4444" in command
    assert "--pixel-format=yuva444p10le" in command
    assert "--image-format=png" in command

    named = materialize_scene(
        archive, "hero-stat-callout", edit, {"title": "Traction"}, slot_id="problem-01"
    )
    assert named == edit / "animations/slot_problem-01"
    assert json.loads((named / "pitchcraft-slot.json").read_text())["output"] == "render.mov"

    portrait = materialize_scene(
        archive,
        "hero-stat-callout",
        edit,
        {"title": "Traction"},
        slot_id="portrait-01",
        target_width=1080,
        target_height=1920,
    )
    portrait_manifest = json.loads((portrait / "pitchcraft-slot.json").read_text())
    assert (portrait_manifest["target_width"], portrait_manifest["target_height"]) == (1080, 1920)
    root = (portrait / "src/Root.tsx").read_text()
    assert "scale(0.29250000)" in root
    assert "top: '76.80px'" in root
    assert "right: '43.20px'" in root

    slowed = materialize_scene(
        archive,
        "flowchart",
        edit,
        {"title": "Process"},
        slot_id="flow-01",
        render_duration_s=4.5,
    )
    slowed_root = (slowed / "src/Root.tsx").read_text()
    assert "const OUTPUT_DURATION_FRAMES = 135;" in slowed_root
    assert "const HOLD_FRAMES = 30;" in slowed_root
    assert "<Freeze frame={sourceFrame}>" in slowed_root

    with pytest.raises(ValueError, match="undeclared"):
        materialize_scene(archive, "hero-stat-callout", edit, {"other": "value"})
