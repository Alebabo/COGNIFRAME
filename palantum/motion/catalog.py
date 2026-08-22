from __future__ import annotations

import hashlib
import json
import re
import zipfile
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, cast

CATALOG_SCHEMA_VERSION = 1
CURATION_VERSION = 1

# This is deliberately a small allowlist. The original template sources remain in the
# user-supplied pack and are not redistributed with Palantum.
CURATED_SCENES: dict[str, dict[str, Any]] = {
    "hero-stat-callout": {
        "beat_type": "PROBLEM",
        "confidence": 0.95,
        "max_chars": {"heroValue": 12, "heroLabel": 40, "stats": 120, "bgColor": 32},
    },
    "bar-chart-reveal": {
        "beat_type": "PROBLEM",
        "confidence": 0.9,
        "max_chars": {"title": 48, "barLabels": 120, "barValues": 80},
    },
    "step-explainer": {
        "beat_type": "SOLUTION",
        "confidence": 0.95,
        "max_chars": {"title": 48, "steps": 180},
    },
    "flowchart": {
        "beat_type": "SOLUTION",
        "confidence": 0.9,
        "max_chars": {"title": 48, "steps": 240},
    },
    "screen-showcase": {
        "beat_type": "DEMO",
        "confidence": 0.9,
        "max_chars": {"title": 40, "features": 160, "bgColor": 32},
    },
    "ui-walkthrough": {
        "beat_type": "DEMO",
        "confidence": 0.9,
        "max_chars": {"stepLabels": 120, "textColor": 32},
    },
    "saas-metrics-board": {
        "beat_type": "TRACTION",
        "confidence": 0.95,
        "max_chars": {
            "title": 48,
            "labels": 120,
            "values": 80,
            "trend": 24,
            "accentColor": 32,
        },
    },
    "brand-statement": {
        "beat_type": "ASK",
        "confidence": 0.6,
        "max_chars": {
            "statement": 100,
            "highlight": 32,
            "bgColor": 32,
            "textColor": 32,
        },
    },
}


def _json_object(raw: str, source: str) -> dict[str, Any]:
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError(f"expected a JSON object in {source}")
    return cast(dict[str, Any], value)


def _json_array(raw: str, source: str) -> list[dict[str, Any]]:
    value = json.loads(raw)
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise ValueError(f"expected a JSON object array in {source}")
    return cast(list[dict[str, Any]], value)


def _safe_relative(path: str) -> str:
    normalized = PurePosixPath(path.replace("\\", "/"))
    if normalized.is_absolute() or ".." in normalized.parts:
        raise ValueError(f"unsafe template path: {path}")
    return normalized.as_posix()


@dataclass(frozen=True)
class PackReader:
    source: Path
    kind: str
    sha256: str
    prefix: str = ""

    @classmethod
    def open(cls, source: Path) -> PackReader:
        source = source.resolve()
        if source.is_file() and zipfile.is_zipfile(source):
            digest = hashlib.sha256(source.read_bytes()).hexdigest()
            with zipfile.ZipFile(source) as archive:
                names = [name for name in archive.namelist() if not name.endswith("/")]
            candidates = sorted(
                name[: -len("index.json")]
                for name in names
                if name == "index.json" or name.endswith("/index.json")
            )
            if len(candidates) != 1:
                raise ValueError("template ZIP must contain exactly one index.json")
            return cls(source=source, kind="zip", sha256=digest, prefix=candidates[0])
        if source.is_dir():
            root = _find_directory_root(source)
            digest = _directory_hash(root)
            return cls(source=root, kind="directory", sha256=digest)
        raise ValueError(f"motion pack is neither a ZIP nor a directory: {source}")

    def read_bytes(self, relative: str) -> bytes:
        relative = _safe_relative(relative)
        if self.kind == "zip":
            with zipfile.ZipFile(self.source) as archive:
                try:
                    return archive.read(f"{self.prefix}{relative}")
                except KeyError as error:
                    raise FileNotFoundError(relative) from error
        path = (self.source / Path(relative)).resolve()
        if not path.is_relative_to(self.source):
            raise ValueError(f"template path escapes pack root: {relative}")
        return path.read_bytes()

    def read_text(self, relative: str) -> str:
        return self.read_bytes(relative).decode("utf-8")

    def copy(self, relative: str, destination: Path) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(self.read_bytes(relative))


def _find_directory_root(source: Path) -> Path:
    direct = source / "index.json"
    if direct.is_file():
        return source.resolve()
    candidates = sorted(path.parent for path in source.rglob("index.json"))
    if len(candidates) != 1:
        raise ValueError("template directory must contain exactly one index.json")
    return candidates[0].resolve()


def _directory_files(root: Path) -> Iterator[Path]:
    yield from sorted(
        (path for path in root.rglob("*") if path.is_file()), key=lambda p: p.as_posix()
    )


def _directory_hash(root: Path) -> str:
    digest = hashlib.sha256()
    for path in _directory_files(root):
        relative = path.relative_to(root).as_posix().encode("utf-8")
        data = path.read_bytes()
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        digest.update(len(data).to_bytes(8, "big"))
        digest.update(data)
    return digest.hexdigest()


def _static_parse(source: str, expected_component: str) -> dict[str, Any]:
    incomplete = re.search(r"\{\s*[A-Za-z_$][\w.$]*\s*&&\s*\}", source)
    if incomplete:
        return {
            "status": "broken",
            "parser": "palantum-static-v1",
            "confidence": 1.0,
            "reason": "incomplete JSX conditional expression",
        }
    export = re.search(r"export\s+const\s+([A-Za-z_$][\w$]*)", source)
    if not export:
        return {
            "status": "broken",
            "parser": "palantum-static-v1",
            "confidence": 0.95,
            "reason": "no exported component declaration found",
        }
    if export.group(1) != expected_component:
        return {
            "status": "broken",
            "parser": "palantum-static-v1",
            "confidence": 0.95,
            "reason": (
                f"exported component {export.group(1)!r} does not match "
                f"metadata componentName {expected_component!r}"
            ),
        }
    if not re.search(r"from\s+['\"]remotion['\"]", source):
        return {
            "status": "broken",
            "parser": "palantum-static-v1",
            "confidence": 0.9,
            "reason": "component does not import Remotion",
        }
    return {
        "status": "ok",
        "parser": "palantum-static-v1",
        "confidence": 0.8,
        "reason": None,
    }


def _slots(metadata: dict[str, Any], limits: dict[str, int]) -> list[dict[str, Any]]:
    raw_slots = metadata.get("editableProps", [])
    if not isinstance(raw_slots, list):
        raise ValueError(f"editableProps must be an array for {metadata.get('id')}")
    result: list[dict[str, Any]] = []
    for raw in raw_slots:
        if not isinstance(raw, dict) or not isinstance(raw.get("key"), str):
            raise ValueError(f"invalid editable prop for {metadata.get('id')}")
        key = str(raw["key"])
        default = raw.get("defaultValue", "")
        result.append(
            {
                "key": key,
                "type": str(raw.get("type", "string")),
                "label": str(raw.get("label", key)),
                "default": default,
                "max_chars": int(limits.get(key, max(32, min(240, len(str(default)) * 2)))),
            }
        )
    return result


def _scene(reader: PackReader, entry: dict[str, Any], curated: dict[str, Any]) -> dict[str, Any]:
    scene_id = str(entry.get("id", ""))
    component_path = _safe_relative(str(entry.get("file", f"{scene_id}/Composition.tsx")))
    metadata_path = f"{PurePosixPath(component_path).parent.as_posix()}/metadata.json"
    metadata = _json_object(reader.read_text(metadata_path), metadata_path)
    component_name = str(metadata.get("componentName", entry.get("componentName", "")))
    source = reader.read_text(component_path)
    static_parse = _static_parse(source, component_name)
    return {
        "id": scene_id,
        "name": str(metadata.get("name", scene_id)),
        "beat_type": str(curated["beat_type"]),
        "confidence": float(curated["confidence"]),
        "engine": "remotion",
        "component_name": component_name,
        "component_path": component_path,
        "metadata_path": metadata_path,
        "duration_s": float(metadata["duration"]),
        "duration_in_frames": int(metadata["durationInFrames"]),
        "fps": int(metadata["fps"]),
        "width": int(metadata["width"]),
        "height": int(metadata["height"]),
        "slots": _slots(metadata, cast(dict[str, int], curated["max_chars"])),
        "status": static_parse["status"],
        "broken_reason": static_parse["reason"],
        "static_parse": static_parse,
    }


def build_scene_catalog(source: Path, output: Path) -> dict[str, Any]:
    """Scan the curated subset of an external pack and cache its catalog by source hash."""
    reader = PackReader.open(source)
    if output.exists():
        try:
            cached = _json_object(output.read_text(), str(output))
            cached_source = cached.get("source")
            if (
                cached.get("schema_version") == CATALOG_SCHEMA_VERSION
                and cached.get("curation_version") == CURATION_VERSION
                and isinstance(cached_source, dict)
                and cached_source.get("sha256") == reader.sha256
                and cached.get("source_sha256") == reader.sha256
            ):
                return cached
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            pass

    index = _json_array(reader.read_text("index.json"), "index.json")
    by_id = {str(item.get("id", "")): item for item in index}
    scenes: list[dict[str, Any]] = []
    for scene_id, curated in CURATED_SCENES.items():
        entry = by_id.get(scene_id)
        if entry is None:
            scenes.append(
                {
                    "id": scene_id,
                    "beat_type": curated["beat_type"],
                    "confidence": curated["confidence"],
                    "engine": "remotion",
                    "status": "broken",
                    "broken_reason": "curated scene is missing from index.json",
                    "static_parse": {
                        "status": "broken",
                        "parser": "palantum-static-v1",
                        "confidence": 1.0,
                        "reason": "curated scene is missing from index.json",
                    },
                }
            )
            continue
        try:
            scenes.append(_scene(reader, entry, curated))
        except (FileNotFoundError, KeyError, TypeError, ValueError, UnicodeDecodeError) as error:
            scenes.append(
                {
                    "id": scene_id,
                    "beat_type": curated["beat_type"],
                    "confidence": curated["confidence"],
                    "engine": "remotion",
                    "status": "broken",
                    "broken_reason": f"{type(error).__name__}: {error}",
                    "static_parse": {
                        "status": "broken",
                        "parser": "palantum-static-v1",
                        "confidence": 1.0,
                        "reason": f"{type(error).__name__}: {error}",
                    },
                }
            )
    catalog: dict[str, Any] = {
        "schema_version": CATALOG_SCHEMA_VERSION,
        "curation_version": CURATION_VERSION,
        "source_sha256": reader.sha256,
        "source": {
            "kind": reader.kind,
            "name": reader.source.name,
            "sha256": reader.sha256,
        },
        "scenes": scenes,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(catalog, indent=2, ensure_ascii=False) + "\n")
    return catalog
