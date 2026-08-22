from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

from palantum.motion.catalog import PackReader, build_scene_catalog

REMOTION_VERSION = "4.0.0"


def _scene(catalog: dict[str, Any], scene_id: str) -> dict[str, Any]:
    scenes = catalog.get("scenes", [])
    if not isinstance(scenes, list):
        raise ValueError("scene catalog has no scenes array")
    for value in scenes:
        if isinstance(value, dict) and value.get("id") == scene_id:
            return value
    raise KeyError(f"scene is not curated: {scene_id}")


def _props(scene: dict[str, Any], supplied: dict[str, Any] | None) -> dict[str, Any]:
    slots = scene.get("slots", [])
    if not isinstance(slots, list):
        raise ValueError("scene has no valid slots")
    defaults = {
        str(slot["key"]): slot.get("default", "")
        for slot in slots
        if isinstance(slot, dict) and isinstance(slot.get("key"), str)
    }
    limits = {
        str(slot["key"]): int(slot["max_chars"])
        for slot in slots
        if isinstance(slot, dict)
        and isinstance(slot.get("key"), str)
        and isinstance(slot.get("max_chars"), int)
    }
    supplied = supplied or {}
    unknown = sorted(set(supplied) - set(defaults))
    if unknown:
        raise ValueError(f"undeclared scene props: {', '.join(unknown)}")
    merged = defaults | supplied
    for key, value in merged.items():
        if not isinstance(value, str):
            raise ValueError(f"scene prop {key} must be a string")
        if len(value) > limits[key]:
            raise ValueError(f"scene prop {key} exceeds max_chars={limits[key]}")
    return merged


def _package_json(needs_lucide: bool) -> dict[str, Any]:
    dependencies = {
        "@remotion/cli": REMOTION_VERSION,
        "remotion": REMOTION_VERSION,
        "react": "18.2.0",
        "react-dom": "18.2.0",
    }
    if needs_lucide:
        dependencies["lucide-react"] = "0.468.0"
    return {
        "name": "palantum-motion-slot",
        "version": "0.0.0",
        "private": True,
        "scripts": {"render": "remotion render src/index.ts Scene output.mov"},
        "dependencies": dependencies,
        "devDependencies": {
            "@types/react": "18.2.79",
            "@types/react-dom": "18.2.25",
            "typescript": "5.5.4",
        },
    }


def materialize_scene(
    source: Path,
    scene_id: str,
    edit_dir: Path,
    props: dict[str, Any] | None = None,
    slot_id: str | None = None,
    target_width: int | None = None,
    target_height: int | None = None,
) -> Path:
    """Copy one curated scene into an isolated, renderable edit/animations slot."""
    if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", scene_id):
        raise ValueError(f"unsafe scene id: {scene_id}")
    slot_id = slot_id or scene_id
    if not re.fullmatch(r"[a-z0-9]+(?:[-_][a-z0-9]+)*", slot_id):
        raise ValueError(f"unsafe slot id: {slot_id}")
    catalog_path = edit_dir / "scene-catalog.json"
    catalog = build_scene_catalog(source, catalog_path)
    scene = _scene(catalog, scene_id)
    parse = scene.get("static_parse")
    if not isinstance(parse, dict) or parse.get("status") != "ok":
        reason = parse.get("reason") if isinstance(parse, dict) else "unknown parse failure"
        raise ValueError(f"scene {scene_id} is broken: {reason}")
    slot = edit_dir / "animations" / f"slot_{slot_id}"
    src = slot / "src"
    src.mkdir(parents=True, exist_ok=True)
    reader = PackReader.open(source)
    reader.copy(str(scene["component_path"]), src / "Composition.tsx")
    reader.copy(str(scene["metadata_path"]), slot / "metadata.json")
    resolved_props = _props(scene, props)
    (slot / "props.json").write_text(
        json.dumps(resolved_props, indent=2, ensure_ascii=False) + "\n"
    )
    component_name = str(scene["component_name"])
    scene_width = int(scene["width"])
    scene_height = int(scene["height"])
    target_width = target_width or scene_width
    target_height = target_height or scene_height
    if target_width <= 0 or target_height <= 0:
        raise ValueError("target dimensions must be positive")
    scale = min(target_width / scene_width, target_height / scene_height)
    root = f"""import React from 'react';
import {{AbsoluteFill, Composition}} from 'remotion';
import props from '../props.json';
import {{{component_name}}} from './Composition';

const SceneCanvas: React.FC = () => (
  <AbsoluteFill style={{{{
    backgroundColor: 'transparent',
    alignItems: 'center',
    justifyContent: 'center',
  }}}}>
    <div style={{{{
      width: {scene_width},
      height: {scene_height},
      position: 'relative',
      flex: '0 0 auto',
      overflow: 'hidden',
      transform: 'scale({scale:.8f})',
      transformOrigin: 'center center',
    }}}}>
      <{component_name} {{...props}} />
    </div>
  </AbsoluteFill>
);

export const RemotionRoot: React.FC = () => (
  <Composition
    id="Scene"
    component={{SceneCanvas}}
    durationInFrames={{{int(scene["duration_in_frames"])}}}
    fps={{{int(scene["fps"])}}}
    width={{{target_width}}}
    height={{{target_height}}}
  />
);
"""
    (src / "Root.tsx").write_text(root)
    (src / "index.ts").write_text(
        "import {registerRoot} from 'remotion';\n"
        "import {RemotionRoot} from './Root';\n\n"
        "registerRoot(RemotionRoot);\n"
    )
    package = _package_json("lucide-react" in (src / "Composition.tsx").read_text())
    (slot / "package.json").write_text(json.dumps(package, indent=2) + "\n")
    tsconfig = {
        "compilerOptions": {
            "target": "ES2020",
            "module": "ESNext",
            "moduleResolution": "Bundler",
            "jsx": "react-jsx",
            "strict": True,
            "esModuleInterop": True,
            "resolveJsonModule": True,
            "skipLibCheck": True,
        },
        "include": ["src", "props.json"],
    }
    (slot / "tsconfig.json").write_text(json.dumps(tsconfig, indent=2) + "\n")
    manifest = {
        "scene_id": scene_id,
        "source_sha256": catalog["source"]["sha256"],
        "catalog": str(catalog_path),
        "output": "render.mov",
        "target_width": target_width,
        "target_height": target_height,
    }
    (slot / "palantum-slot.json").write_text(json.dumps(manifest, indent=2) + "\n")
    return slot


def build_render_command(slot: Path, output: Path | None = None) -> list[str]:
    """Return the deterministic Remotion CLI invocation for a ProRes 4444 alpha MOV."""
    output = output or slot / "render.mov"
    launcher = (
        [os.environ.get("COMSPEC", "cmd.exe"), "/d", "/s", "/c", "npx"]
        if os.name == "nt"
        else ["npx"]
    )
    return [
        *launcher,
        "--no-install",
        "remotion",
        "render",
        str(slot / "src" / "index.ts"),
        "Scene",
        str(output),
        "--codec=prores",
        "--prores-profile=4444",
        "--pixel-format=yuva444p10le",
        "--image-format=png",
        f"--props={slot / 'props.json'}",
    ]
