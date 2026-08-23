from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from palantum.agents.backends.devin import DEVIN_PROMPT_BUDGET_CHARS, serialize_prompt
from palantum.agents.runner import run_role
from palantum.engine.transcribe import transcribe
from palantum.engine.videouse import (
    ProbeResult,
    pack_transcripts,
    probe,
    render,
    timeline_view,
)
from palantum.engine.visual import classify_visual
from palantum.motion import build_render_command, build_scene_catalog, materialize_scene
from palantum.state import coverage_score, load, save


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected object in {path}")
    return value


def _write_json(path: Path, value: dict[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f"{path.suffix}.tmp")
    temporary.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    temporary.replace(path)
    return path


def _cached_role(
    edit_dir: Path,
    cache_key: str,
    role_id: str,
    prompt: str,
    context: dict[str, Any],
    *,
    resume: bool,
    semantic_validator: Callable[[dict[str, Any]], list[str]] | None = None,
    max_feedback_rounds: int = 0,
) -> dict[str, Any]:
    if not re.fullmatch(r"[A-Za-z0-9]+(?:[-_.][A-Za-z0-9]+)*", cache_key):
        raise ValueError(f"unsafe role cache key: {cache_key}")
    path = edit_dir / "role-cache" / f"{cache_key}.json"
    if resume and path.exists():
        cached = _read_json(path)
        if semantic_validator is None or not semantic_validator(cached):
            return cached
    output = run_role(
        role_id,
        prompt,
        context,
        None,
        semantic_validator=semantic_validator,
        max_feedback_rounds=max_feedback_rounds,
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(output, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return output


def _schema() -> dict[str, Any]:
    path = Path(__file__).resolve().parent / "schemas" / "yc_pitch_60s.json"
    return _read_json(path)


def _prompt(role_id: str) -> str:
    names = {
        "A1": "a1_script_supervisor",
        "A0": "a0_template_scout",
        "A2": "a2_director",
        "A3": "a3_strategist",
        "A4": "a4_cutter",
        "A5": "a5_variant_supervisor",
        "A6": "a6_graphics_director",
        "A6W": "a6_slot_worker",
        "A7": "a7_qc",
    }
    return (
        Path(__file__).resolve().parent / "agents" / "prompts" / f"{names[role_id]}.md"
    ).read_text()


def _beat_map(items: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {str(item["id"]): item for item in items}


def _ruling_map(items: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {str(item["beat"]): item for item in items}


def _probe_context(result: ProbeResult) -> dict[str, Any]:
    return {
        "path": str(result.path),
        "duration_s": result.duration_s,
        "width": result.width,
        "height": result.height,
        "fps": result.fps,
        "has_audio": result.has_audio,
        "streams": result.streams,
    }


def _file_free_probe_context(result: ProbeResult) -> dict[str, Any]:
    """Return compact render measurements without exposing a filesystem path."""
    return {
        "duration_s": result.duration_s,
        "width": result.width,
        "height": result.height,
        "fps": result.fps,
        "has_audio": result.has_audio,
    }


def _compact_word_index(word_index: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    """Round ASR timestamp noise before embedding the index in an agent prompt."""
    return {
        str(source): [
            {
                "text": str(item["text"]),
                "start": round(float(item["start"]), 3),
                "end": round(float(item["end"]), 3),
            }
            for item in words
            if isinstance(item, dict)
        ]
        for source, words in word_index.items()
        if isinstance(words, list)
    }


def _volume_value(stderr: str, label: str) -> float:
    match = re.search(rf"{label}:\s*(-?(?:\d+(?:\.\d+)?|inf))\s+dB", stderr)
    if not match:
        raise ValueError(f"ffmpeg volumedetect did not report {label}")
    value = match.group(1)
    return -100.0 if value in {"-inf", "inf"} else float(value)


def _audio_check(output: Path, ranges: list[dict[str, Any]]) -> dict[str, Any]:
    """Measure segment loudness; both mean and peak below -45 dBFS mean silence.

    The threshold leaves substantial headroom below normal speech while requiring
    both measures to be quiet, avoiding false positives for sparse speech with a
    low average level but audible peaks.
    """
    threshold = -45.0
    segments: list[dict[str, Any]] = []
    silent_segments: list[int] = []
    offset = 0.0
    for index, item in enumerate(ranges):
        duration = float(item["end"]) - float(item["start"])
        try:
            raw = subprocess.run(
                [
                    "ffmpeg",
                    "-hide_banner",
                    "-nostats",
                    "-i",
                    str(output),
                    "-ss",
                    f"{offset:.3f}",
                    "-t",
                    f"{duration:.3f}",
                    "-af",
                    "volumedetect",
                    "-f",
                    "null",
                    "-",
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            mean_dbfs = _volume_value(raw.stderr, "mean_volume")
            max_dbfs = _volume_value(raw.stderr, "max_volume")
        except (subprocess.CalledProcessError, ValueError):
            mean_dbfs = -100.0
            max_dbfs = -100.0
        silent = mean_dbfs <= threshold and max_dbfs <= threshold
        segments.append(
            {
                "index": index,
                "start_s": offset,
                "end_s": offset + duration,
                "mean_dbfs": mean_dbfs,
                "max_dbfs": max_dbfs,
                "silent": silent,
            }
        )
        if silent:
            silent_segments.append(index)
        offset += duration
    return {
        "segments": segments,
        "silent_segments": silent_segments,
        "threshold_dbfs": threshold,
    }


def _duration_check(expected_total: float, measured: float) -> dict[str, Any]:
    delta = abs(measured - expected_total)
    return {
        "expected_total_s": expected_total,
        "measured_s": measured,
        "delta_s": delta,
        "tolerance_s": 0.30,
        "within_tolerance": delta <= 0.30,
    }


def _timeline_views(output: Path, ranges: list[dict[str, Any]], edit_dir: Path) -> list[str]:
    views: list[str] = []
    offset = 0.0
    for index, item in enumerate(ranges):
        duration = float(item["end"]) - float(item["start"])
        view = edit_dir / "verify" / f"cut_{index:02d}.png"
        try:
            timeline_view(output, max(0, offset - 1.5), offset + duration + 1.5, view)
            views.append(str(view))
        except (subprocess.CalledProcessError, FileNotFoundError):
            pass
        offset += duration
    return views


def _normalize_qc_verdict(report: dict[str, Any]) -> dict[str, Any]:
    """Make the aggregate verdict follow the structured per-check verdicts."""
    findings = report.get("findings", [])
    if isinstance(findings, list):
        report["verdict"] = (
            "fail"
            if any(isinstance(item, dict) and item.get("verdict") == "fail" for item in findings)
            else "pass"
        )
    return report


def _merge_motion_qc(
    report: dict[str, Any], overlays: list[dict[str, Any]]
) -> dict[str, Any]:
    findings = list(report.get("findings", []))
    for overlay in overlays:
        visual = overlay.get("visual_qc")
        where = f"{float(overlay.get('start_in_output', 0)):.3f}s"
        if not isinstance(visual, dict):
            findings.append(
                {
                    "check": "Motion overlay visibility",
                    "verdict": "unmeasurable",
                    "measured": "legacy overlay has no local visual measurements",
                    "expected": "alpha coverage <=35% and validated props",
                    "where": where,
                    "fix": "re-render the overlay with the current motion harness",
                }
            )
            continue
        verdict = "pass" if visual.get("verdict") == "pass" else "fail"
        findings.append(
            {
                "check": "Motion overlay visibility",
                "verdict": verdict,
                "measured": (
                    f"presentation={visual.get('presentation')}, "
                    f"alpha={float(visual.get('max_alpha_coverage', 1)):.1%}, "
                    f"contrast={visual.get('contrast_ratio')}"
                ),
                "expected": "alpha coverage <=35%, contrast >=4.5:1 when applicable",
                "where": where,
                "fix": "render as an inset with valid numeric and color props"
                if verdict == "fail"
                else "",
            }
        )
    report["findings"] = findings
    return _normalize_qc_verdict(report)


def _apply_coverage(
    state: dict[str, Any],
    director: dict[str, Any],
    strategist: dict[str, Any],
    schema: dict[str, Any],
) -> None:
    director_beats = _beat_map(director["beats"])
    strategist_beats = _ruling_map(strategist["rulings"])
    beats: list[dict[str, Any]] = []
    for definition in schema["beats"]:
        beat_id = str(definition["id"])
        item = dict(director_beats[beat_id])
        item["owner"] = "A2"
        ruling = strategist_beats.get(beat_id)
        if ruling and item["status"] in {"weak", "covered"}:
            verdict = str(ruling["verdict"])
            item["status"] = item["status"] if verdict == "agree" else verdict
            item["reason"] = str(ruling["reason"])
        beats.append(item)

    old_notes = {str(note["beat"]): note for note in state.get("director_notes", [])}
    notes = [
        note
        for note in director["director_notes"]
        if str(note["beat"]) in {b["id"] for b in beats if b["status"] != "covered"}
    ]
    resolved = list(state.get("resolved_notes", []))
    now = datetime.now(UTC).isoformat()
    for beat_id, old_note in old_notes.items():
        current = next((b for b in beats if b["id"] == beat_id), None)
        if (
            current
            and current["status"] in {"weak", "covered"}
            and not any(str(item.get("beat")) == beat_id for item in resolved)
        ):
            resolved_note = dict(old_note)
            resolved_note["closed_by"] = current.get("source")
            resolved_note["closed_at"] = now
            resolved.append(resolved_note)
    state["beats"] = beats
    state["director_notes"] = notes
    state["resolved_notes"] = resolved
    state["coverage_score"] = coverage_score(beats, schema)
    state["meta"]["iteration"] = int(state["meta"].get("iteration", 0)) + 1


def _debate(state: dict[str, Any], director: dict[str, Any], strategist: dict[str, Any]) -> None:
    d_map = _beat_map(director["beats"])
    s_map = _ruling_map(strategist["rulings"])
    for beat_id, director_beat in d_map.items():
        ruling = s_map.get(beat_id)
        if not ruling or director_beat["status"] not in {"weak", "covered"}:
            continue
        strategist_status = (
            director_beat["status"] if ruling["verdict"] == "agree" else ruling["verdict"]
        )
        if strategist_status != director_beat["status"]:
            state["debate_log"].append(
                {
                    "round": 1,
                    "beat": beat_id,
                    "a2": director_beat["status"],
                    "a3": strategist_status,
                    "resolved": strategist_status,
                    "rule": "A3 gewinnt bei Inhalt",
                }
            )


def _context(
    edit_dir: Path, schema: dict[str, Any], state: dict[str, Any], run_number: int
) -> dict[str, Any]:
    transcripts = sorted((edit_dir / "transcripts").glob("*.json"))
    return {
        "run_number": run_number,
        "_session_state_path": str(edit_dir / "sessions.json"),
        "schema": schema,
        "previous_coverage": state,
        "takes_packed": (edit_dir / "takes_packed.md").read_text(),
        "transcripts": {path.stem: _read_json(path) for path in transcripts},
        "word_index": _read_json(edit_dir / "word_index.json")
        if (edit_dir / "word_index.json").exists()
        else {},
        "probe": {},
    }


def _motion_source(explicit: Path | None, state: dict[str, Any]) -> Path | None:
    configured = explicit or (
        Path(value) if (value := os.getenv("PALANTUM_TEMPLATE_SOURCE")) else None
    )
    if configured is None and (saved := state.get("meta", {}).get("template_source")):
        configured = Path(str(saved))
    if configured is None:
        return None
    configured = configured.resolve()
    if not configured.exists():
        raise FileNotFoundError(f"motion template source does not exist: {configured}")
    return configured


def _merge_template_assessment(
    catalog_path: Path, catalog: dict[str, Any], assessment: dict[str, Any]
) -> dict[str, Any]:
    assessed = {str(item["id"]): item for item in assessment["scenes"]}
    known = {str(item["id"]) for item in catalog["scenes"]}
    unknown = sorted(set(assessed) - known)
    if unknown:
        raise ValueError(f"A0 assessed unknown templates: {', '.join(unknown)}")
    for scene in catalog["scenes"]:
        item = assessed.get(str(scene["id"]))
        static = scene.get("static_parse", {})
        if item is None:
            item = {
                "id": scene["id"],
                "type": "unclassified",
                "confidence": 0.0,
                "status": "broken",
                "reason": "A0 omitted the scene",
            }
        scene["agent_assessment"] = item
        scene["type"] = item["type"]
        scene["confidence"] = min(float(scene.get("confidence", 0)), float(item["confidence"]))
        scene["status"] = (
            "ok" if static.get("status") == "ok" and item["status"] == "ok" else "broken"
        )
    catalog_path.write_text(
        json.dumps(catalog, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    return catalog


def analyze(
    edit_dir: Path,
    sources: list[Path],
    template_source: Path | None = None,
    brief: str | None = None,
) -> dict[str, Any]:
    """Ingest sources and run the deterministic coverage state machine."""
    schema = _schema()
    state = load(edit_dir, schema)
    previous_sources = [Path(str(source)) for source in state["meta"].get("sources", [])]
    source_map = {str(source.resolve()): source.resolve() for source in previous_sources + sources}
    all_sources = sorted(source_map.values(), key=lambda path: path.name)
    for source in all_sources:
        transcribe(source, edit_dir)
    pack_transcripts(edit_dir)
    source_facts = []
    for source in all_sources:
        result = probe(source)
        source_facts.append(
            {
                "id": source.stem,
                "path": str(source),
                "duration_s": result.duration_s,
                "width": result.width,
                "height": result.height,
                "fps": result.fps,
                "has_audio": result.has_audio,
                "visual": classify_visual(source, edit_dir),
            }
        )
    context = _context(edit_dir, schema, state, int(state["meta"]["iteration"]) + 1)
    context["probe"] = source_facts
    context["project_brief"] = brief or state["meta"].get("brief", "")
    a1_context = {
        "run_number": context["run_number"],
        "_session_state_path": context["_session_state_path"],
        "schema": context["schema"],
        "takes_packed": context["takes_packed"],
        "probe": context["probe"],
    }
    motion_source = _motion_source(template_source, state)
    catalog_path = edit_dir / "scene-catalog.json"
    catalog = build_scene_catalog(motion_source, catalog_path) if motion_source else None
    with ThreadPoolExecutor(max_workers=2) as executor:
        a1_future = executor.submit(run_role, "A1", _prompt("A1"), a1_context, None)
        a0_future = None
        if catalog is not None:
            a0_context = {
                "run_number": context["run_number"],
                "_session_state_path": context["_session_state_path"],
                "catalog": catalog,
                "project_brief": context["project_brief"],
            }
            a0_future = executor.submit(run_role, "A0", _prompt("A0"), a0_context, None)
        a1 = a1_future.result()
        (edit_dir / "script-supervisor.json").write_text(
            json.dumps(a1, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        if a0_future is not None and catalog is not None:
            catalog = _merge_template_assessment(catalog_path, catalog, a0_future.result())
    a2_context = {
        "run_number": context["run_number"],
        "_session_state_path": context["_session_state_path"],
        "schema": context["schema"],
        "previous_coverage": context["previous_coverage"],
        "takes_packed": context["takes_packed"],
        "probe": context["probe"],
        "project_brief": context["project_brief"],
        "script_supervisor": a1,
    }
    a2 = run_role("A2", _prompt("A2"), a2_context, None)
    (edit_dir / "director.json").write_text(
        json.dumps(a2, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    a3_context = {
        "run_number": context["run_number"],
        "_session_state_path": context["_session_state_path"],
        "schema": context["schema"],
        "takes_packed": context["takes_packed"],
        "project_brief": context["project_brief"],
        "director": a2,
    }
    a3 = run_role("A3", _prompt("A3"), a3_context, None)
    _debate(state, a2, a3)
    _apply_coverage(state, a2, a3, schema)
    state["meta"]["sources"] = [str(source) for source in all_sources]
    if motion_source:
        assert catalog is not None
        state["meta"]["template_source"] = str(motion_source)
        state["meta"]["template_pack_sha256"] = catalog["source"]["sha256"]
    if brief:
        state["meta"]["brief"] = brief
    save(edit_dir, state)
    return state


def gate(state: dict[str, Any], schema: dict[str, Any]) -> tuple[bool, list[dict[str, Any]]]:
    """Allow cutting only when every required beat is at least weak."""
    statuses = {str(beat["id"]): str(beat["status"]) for beat in state["beats"]}
    blocked = [
        beat
        for beat in schema["beats"]
        if beat.get("required") and statuses.get(str(beat["id"])) == "missing"
    ]
    return not blocked, state["director_notes"]


def _output_ranges(ranges: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    offset = 0.0
    for item in ranges:
        duration = float(item["end"]) - float(item["start"])
        if duration <= 0:
            raise ValueError(f"invalid A4 range duration for {item.get('beat')}")
        result.append(
            {
                "beat": str(item["beat"]),
                "start_in_output": offset,
                "end_in_output": offset + duration,
                "duration": duration,
                "quote": str(item.get("quote", "")),
            }
        )
        offset += duration
    return result


def _selectable_scenes(catalog: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        scene
        for scene in catalog.get("scenes", [])
        if isinstance(scene, dict)
        and scene.get("status") == "ok"
        and float(scene.get("confidence", 0)) >= 0.7
        and scene.get("type") in {"PROBLEM", "SOLUTION", "DEMO", "TRACTION", "ASK"}
    ]


_GENERIC_MOTION_SCENES = {
    "HOOK": ("brand-statement", "hero-stat-callout"),
    "PROBLEM": ("hero-stat-callout", "brand-statement"),
    "SOLUTION": ("step-explainer", "flowchart"),
    "DEMO": ("screen-showcase", "ui-walkthrough"),
    "TRACTION": ("saas-metrics-board", "bar-chart-reveal"),
    "TEAM": ("brand-statement", "hero-stat-callout"),
    "ASK": ("brand-statement", "hero-stat-callout"),
}


def _single_motion_scenes(catalog: dict[str, Any], beat: str) -> list[dict[str, Any]]:
    exact = [scene for scene in _selectable_scenes(catalog) if scene.get("type") == beat]
    if exact:
        return exact
    candidates = {
        str(scene.get("id")): scene
        for scene in catalog.get("scenes", [])
        if isinstance(scene, dict)
        and scene.get("status") == "ok"
        and scene.get("static_parse", {}).get("status") == "ok"
    }
    for scene_id in _GENERIC_MOTION_SCENES.get(beat, ()):
        if scene_id not in candidates:
            continue
        adapted = dict(candidates[scene_id])
        adapted["motion_variant_fallback"] = True
        adapted["adapted_from_type"] = adapted.get("type", "unclassified")
        adapted["type"] = beat
        adapted["confidence"] = max(0.7, float(adapted.get("confidence", 0)))
        return [adapted]
    return []


def _validate_overlay_plan(
    overlays: list[dict[str, Any]],
    scenes: list[dict[str, Any]],
    timeline: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    by_id = {str(scene["id"]): scene for scene in scenes}
    seen_slots: set[str] = set()
    validated: list[dict[str, Any]] = []
    for overlay in overlays:
        slot_id = str(overlay["slot_id"])
        if not re.fullmatch(r"[a-z0-9]+(?:[-_][a-z0-9]+)*", slot_id):
            raise ValueError(f"unsafe A6 slot_id: {slot_id}")
        if slot_id in seen_slots:
            raise ValueError(f"duplicate A6 slot_id: {slot_id}")
        seen_slots.add(slot_id)
        template_id = str(overlay["template_id"])
        if template_id not in by_id:
            raise ValueError(f"A6 selected unavailable template: {template_id}")
        scene = by_id[template_id]
        beat = str(overlay["beat"])
        if scene["type"] != beat:
            raise ValueError(f"A6 template {template_id} does not match beat {beat}")
        start = float(overlay["start_in_output"])
        duration = float(overlay["duration"])
        windows = [item for item in timeline if item["beat"] == beat]
        containing_windows = [
            item
            for item in windows
            if start >= float(item["start_in_output"]) - 1e-6
            and start < float(item["end_in_output"]) - 1e-6
        ]
        if not containing_windows and windows:
            window = min(
                windows,
                key=lambda item: abs(float(item["start_in_output"]) - start),
            )
            start = float(window["start_in_output"])
            containing_windows = [window]
        if containing_windows:
            window = max(
                containing_windows,
                key=lambda item: float(item["end_in_output"]) - start,
            )
            window_start = float(window["start_in_output"])
            window_end = float(window["end_in_output"])
            min_visible = float(scene.get("min_visible_s", 0))
            if min_visible > 0 and window_end - start < min_visible:
                start = max(window_start, window_end - min_visible)
            available = window_end - start
            if min_visible > 0:
                duration = min(
                    max(duration, min_visible),
                    max(float(scene["duration_s"]), min_visible),
                    available,
                )
            else:
                duration = min(duration, float(scene["duration_s"]), available)
        end = start + duration
        if duration <= 0:
            raise ValueError(f"A6 overlay {slot_id} has no usable duration")
        if not any(
            start >= float(item["start_in_output"]) - 1e-6
            and end <= float(item["end_in_output"]) + 1e-6
            for item in windows
        ):
            raise ValueError(f"A6 overlay {slot_id} falls outside its beat")
        slots = {
            str(item["key"]): int(item["max_chars"])
            for item in scene.get("slots", [])
            if isinstance(item, dict)
        }
        props = overlay["props"]
        unknown = sorted(set(props) - set(slots))
        if unknown:
            raise ValueError(f"A6 overlay {slot_id} has undeclared props: {', '.join(unknown)}")
        for key, value in props.items():
            if len(str(value)) > slots[key]:
                raise ValueError(f"A6 overlay {slot_id} prop {key} exceeds max_chars")
        enriched = dict(overlay)
        enriched["start_in_output"] = start
        enriched["duration"] = duration
        enriched["timeline_quote"] = " ".join(
            str(item.get("quote", "")) for item in containing_windows
        ).strip()
        enriched["scene"] = scene
        validated.append(enriched)
    ordered = sorted(validated, key=lambda item: float(item["start_in_output"]))
    for left, right in zip(ordered, ordered[1:], strict=False):
        if (
            float(left["start_in_output"]) + float(left["duration"])
            > float(right["start_in_output"]) + 1e-6
        ):
            raise ValueError("A6 overlays overlap")
    return validated


_ALPHA_COVERAGE_LIMIT = 0.35
_HERO_VALUE_PATTERN = re.compile(r"^[^0-9.]*[0-9]+(?:\.[0-9]+)?[^0-9.]*$")


def _scene_default(scene: dict[str, Any], key: str) -> str:
    return next(
        (
            str(item.get("default", ""))
            for item in scene.get("slots", [])
            if isinstance(item, dict) and item.get("key") == key
        ),
        "",
    )


def _hex_rgb(value: str) -> tuple[int, int, int]:
    if not re.fullmatch(r"#[0-9a-fA-F]{6}", value):
        raise ValueError(f"expected #RRGGBB color, got {value!r}")
    return (
        int(value[1:3], 16),
        int(value[3:5], 16),
        int(value[5:7], 16),
    )


def _relative_luminance(color: tuple[int, int, int]) -> float:
    channels = []
    for raw in color:
        value = raw / 255
        channels.append(value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4)
    return 0.2126 * channels[0] + 0.7152 * channels[1] + 0.0722 * channels[2]


def _contrast_ratio(foreground: str, background: str) -> float:
    left = _relative_luminance(_hex_rgb(foreground))
    right = _relative_luminance(_hex_rgb(background))
    lighter, darker = max(left, right), min(left, right)
    return (lighter + 0.05) / (darker + 0.05)


def _validate_motion_props(scene: dict[str, Any], props: dict[str, Any]) -> dict[str, Any]:
    contrast: float | None = None
    if str(scene.get("id")) == "hero-stat-callout":
        if "heroValue" not in props:
            raise ValueError("hero-stat-callout heroValue must come from the timeline claim")
        hero_value = str(props["heroValue"])
        if not _HERO_VALUE_PATTERN.fullmatch(hero_value):
            raise ValueError(
                "hero-stat-callout heroValue must contain exactly one parseable number"
            )
        background = str(props.get("bgColor", _scene_default(scene, "bgColor")))
        foreground = str(scene.get("fixed_text_color") or "#171717")
        contrast = _contrast_ratio(foreground, background)
        if contrast < 4.5:
            raise ValueError(
                f"hero-stat-callout bgColor contrast is {contrast:.2f}:1; expected >=4.5:1"
            )
    return {
        "prop_checks": "pass",
        "contrast_ratio": round(contrast, 3) if contrast is not None else None,
    }


def _alpha_coverage(output: Path, duration_s: float) -> float:
    coverages: list[float] = []
    for fraction in (0.25, 0.5, 0.75):
        raw = subprocess.run(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "info",
                "-ss",
                f"{duration_s * fraction:.3f}",
                "-i",
                str(output),
                "-frames:v",
                "1",
                "-vf",
                "alphaextract,format=gray,signalstats,metadata=print:file=-",
                "-f",
                "null",
                "-",
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        values = [
            float(value) / 255
            for value in re.findall(
                r"lavfi\.signalstats\.YAVG=([0-9]+(?:\.[0-9]+)?)",
                f"{raw.stdout}\n{raw.stderr}",
            )
        ]
        if not values:
            raise ValueError(f"ffmpeg reported no alpha measurements for {output}")
        coverages.append(max(values))
    return max(coverages)


def _render_motion_job(
    edit_dir: Path,
    source: Path,
    overlay: dict[str, Any],
    props: dict[str, Any],
    target_size: tuple[int, int],
    *,
    resume: bool = False,
) -> dict[str, Any]:
    scene = overlay["scene"]
    if not isinstance(scene, dict):
        raise ValueError("motion overlay has no validated scene")
    prop_qc = _validate_motion_props(scene, props)
    expected_presentation = str(scene.get("presentation", "overlay"))
    slot_id = str(overlay["slot_id"])
    existing_slot = edit_dir / "animations" / f"slot_{slot_id}"
    existing_output = existing_slot / "render.mov"
    existing_manifest_path = existing_slot / "palantum-slot.json"
    existing_presentation = None
    if existing_manifest_path.exists():
        with suppress(OSError, ValueError, json.JSONDecodeError):
            existing_presentation = _read_json(existing_manifest_path).get("presentation")
    slot = materialize_scene(
        source,
        str(overlay["template_id"]),
        edit_dir,
        props,
        slot_id=slot_id,
        target_width=target_size[0],
        target_height=target_size[1],
        render_duration_s=float(overlay["duration"]),
    )
    output = slot / "render.mov"
    reuse = (
        resume
        and existing_output.is_file()
        and existing_presentation in {expected_presentation, "inset"}
    )
    if not reuse:
        subprocess.run(
            _npm_install_command(),
            cwd=slot,
            check=True,
        )
        subprocess.run(build_render_command(slot, output), cwd=slot, check=True)
    if not output.is_file():
        raise RuntimeError(f"Remotion did not create {output}")
    presentation = str(existing_presentation) if reuse else expected_presentation
    coverage = _alpha_coverage(output, float(scene["duration_s"]))
    if coverage > _ALPHA_COVERAGE_LIMIT and presentation != "inset":
        slot = materialize_scene(
            source,
            str(overlay["template_id"]),
            edit_dir,
            props,
            slot_id=slot_id,
            target_width=target_size[0],
            target_height=target_size[1],
            presentation="inset",
            render_duration_s=float(overlay["duration"]),
        )
        output = slot / "render.mov"
        subprocess.run(build_render_command(slot, output), cwd=slot, check=True)
        presentation = "inset"
        coverage = _alpha_coverage(output, float(scene["duration_s"]))
    if coverage > _ALPHA_COVERAGE_LIMIT:
        raise ValueError(
            f"motion overlay {slot_id} covers {coverage:.1%} of the frame; "
            f"expected <= {_ALPHA_COVERAGE_LIMIT:.0%}"
        )
    return {
        "file": output.relative_to(edit_dir).as_posix(),
        "start_in_output": float(overlay["start_in_output"]),
        "duration": float(overlay["duration"]),
        "slot_id": str(overlay["slot_id"]),
        "template_id": str(overlay["template_id"]),
        "beat": str(overlay["beat"]),
        "visual_qc": {
            "presentation": presentation,
            "max_alpha_coverage": round(coverage, 4),
            "contrast_ratio": prop_qc["contrast_ratio"],
            "prop_checks": prop_qc["prop_checks"],
            "verdict": "pass",
        },
    }


def _npm_install_command() -> list[str]:
    launcher = (
        [os.environ.get("COMSPEC", "cmd.exe"), "/d", "/s", "/c", "npm"]
        if os.name == "nt"
        else ["npm"]
    )
    return [*launcher, "install", "--ignore-scripts", "--no-audit", "--no-fund"]


def _graphics_overlays(
    edit_dir: Path,
    state: dict[str, Any],
    ranges: list[dict[str, Any]],
    source: Path,
    target_size: tuple[int, int],
    *,
    resume: bool = False,
    approved_catalog: dict[str, Any] | None = None,
    required_overlays: int | None = None,
) -> list[dict[str, Any]]:
    catalog_path = edit_dir / "scene-catalog.json"
    if approved_catalog is None:
        catalog = build_scene_catalog(source, catalog_path)
    else:
        catalog = approved_catalog
        _write_json(catalog_path, catalog)
    timeline = _output_ranges(ranges)
    if required_overlays == 1:
        beats = {str(item["beat"]) for item in timeline}
        if len(beats) != 1:
            raise RuntimeError("a single-motion variant must contain exactly one beat")
        scenes = _single_motion_scenes(catalog, beats.pop())
    else:
        scenes = _selectable_scenes(catalog)
    has_numeric_claim = any(re.search(r"\d", str(item.get("quote", ""))) for item in timeline)
    scenes = [
        scene
        for scene in scenes
        if not scene.get("requires_numeric_claim") or has_numeric_claim
    ]
    if not scenes:
        if required_overlays:
            raise RuntimeError("the motion variant has no usable scene")
        return []
    context = {
        "_session_state_path": str(edit_dir / "sessions.json"),
        "catalog": {"source": catalog["source"], "scenes": scenes},
        "timeline": timeline,
        "brand": state.get("brand", {}),
        "project_brief": state.get("meta", {}).get("brief", ""),
        "motion_policy": {"required_overlays": required_overlays},
    }
    cache_key = "A6-single" if required_overlays == 1 else "A6"
    plan = _cached_role(edit_dir, cache_key, "A6", _prompt("A6"), context, resume=resume)
    overlays = _validate_overlay_plan(plan["overlays"], scenes, timeline)
    if required_overlays is not None and len(overlays) != required_overlays:
        raise RuntimeError(
            f"the motion variant requires {required_overlays} overlay, got {len(overlays)}"
        )
    eligible_beats = {str(item["beat"]) for item in timeline} & {
        str(scene["type"]) for scene in scenes
    }
    if eligible_beats and not overlays:
        raise RuntimeError("A6 returned no motion graphics for eligible pitch beats")

    def worker(overlay: dict[str, Any]) -> dict[str, Any]:
        slot_id = str(overlay["slot_id"])
        worker_context = {
            "_session_state_path": str(edit_dir / "sessions.json"),
            "_session_key": f"A6.{slot_id}",
            "job": {key: value for key, value in overlay.items() if key != "scene"},
            "scene": overlay["scene"],
            "brand": state.get("brand", {}),
        }
        last_error: ValueError | None = None
        for attempt in range(2):
            cache_key = f"A6W-{slot_id}" + ("-retry" if attempt else "")
            checked = _cached_role(
                edit_dir,
                cache_key,
                "A6W",
                _prompt("A6W"),
                worker_context,
                resume=resume,
            )
            if (
                checked["slot_id"] != slot_id
                or checked["template_id"] != overlay["template_id"]
            ):
                raise ValueError(f"A6 slot worker changed the identity of {slot_id}")
            try:
                _validate_motion_props(overlay["scene"], checked["props"])
            except ValueError as error:
                last_error = error
                worker_context["_validation_error"] = str(error)
                continue
            return _render_motion_job(
                edit_dir,
                source,
                overlay,
                checked["props"],
                target_size,
                resume=resume,
            )
        assert last_error is not None
        raise last_error

    with ThreadPoolExecutor(max_workers=min(4, max(1, len(overlays)))) as executor:
        return list(executor.map(worker, overlays))


_CHUNK_VARIANTS = (
    {
        "id": "a",
        "label": "Version A",
        "name": "Clean Cut · ohne Motion Graphic",
        "direction": (
            "Prioritize clarity, natural pauses, and the strongest complete delivery. "
            "Keep the pacing controlled, leave the speaker visually dominant, and use no "
            "motion graphic."
        ),
        "motion": False,
    },
    {
        "id": "b",
        "label": "Version B",
        "name": "Motion Cut · eine Motion Graphic",
        "direction": (
            "Build a tighter, more energetic alternative. Prefer concise phrasing and "
            "leave clean visual room for exactly one integrated motion graphic."
        ),
        "motion": True,
    },
)


_A4_WINDOW_PADDING_S = 0.5
_A4_MAX_CANDIDATES_PER_BEAT = 4


def _candidate_key(candidate: dict[str, Any]) -> tuple[str, float, float]:
    return (
        str(candidate.get("source", "")),
        round(float(candidate.get("start", 0)), 3),
        round(float(candidate.get("end", 0)), 3),
    )


def _selected_candidate_index(
    candidates: list[dict[str, Any]], director_beat: dict[str, Any]
) -> int | None:
    source = str(director_beat.get("source") or "")
    selected_range = director_beat.get("range")
    if not source or not isinstance(selected_range, list) or len(selected_range) != 2:
        return None
    selected_start = float(selected_range[0])
    selected_end = float(selected_range[1])
    matches = [
        (index, abs(float(item.get("start", 0)) - selected_start))
        for index, item in enumerate(candidates)
        if str(item.get("source", "")) == source
        and float(item.get("end", 0)) >= selected_start - 1e-3
        and float(item.get("start", 0)) <= selected_end + 1e-3
    ]
    return min(matches, key=lambda item: item[1])[0] if matches else None


def _director_candidate(
    base_context: dict[str, Any], director_beat: dict[str, Any]
) -> dict[str, Any] | None:
    source = str(director_beat.get("source") or "")
    selected_range = director_beat.get("range")
    if not source or not isinstance(selected_range, list) or len(selected_range) != 2:
        return None
    start = float(selected_range[0])
    end = float(selected_range[1])
    if end <= start:
        return None
    raw_index = base_context.get("word_index", {})
    words = raw_index.get(source, []) if isinstance(raw_index, dict) else []
    quote = " ".join(
        str(item.get("text", ""))
        for item in words
        if isinstance(item, dict)
        and float(item.get("end", 0)) >= start
        and float(item.get("start", 0)) <= end
    ).strip()
    return {"source": source, "start": start, "end": end, "quote": quote}


def _a4_candidates(
    base_context: dict[str, Any], *, beat: str | None, candidate_limit: int
) -> tuple[list[dict[str, Any]], dict[str, list[dict[str, Any]]]]:
    director = base_context.get("director", {})
    director_beats = director.get("beats", []) if isinstance(director, dict) else []
    supervisor = base_context.get("script_supervisor", {})
    raw_groups = (
        supervisor.get("beat_candidates", []) if isinstance(supervisor, dict) else []
    )
    candidates_by_beat = {
        str(group.get("beat")): [
            dict(item)
            for item in group.get("candidates", [])
            if isinstance(item, dict)
        ]
        for group in raw_groups
        if isinstance(group, dict) and isinstance(group.get("candidates"), list)
    }
    reduced_beats: list[dict[str, Any]] = []
    selected_by_beat: dict[str, list[dict[str, Any]]] = {}
    for value in director_beats:
        if not isinstance(value, dict):
            continue
        beat_id = str(value.get("id", ""))
        if not beat_id or (beat is not None and beat_id != beat):
            continue
        reduced = {
            key: value[key]
            for key in ("id", "status", "source", "range", "reason")
            if key in value
        }
        reduced_beats.append(reduced)
        raw_candidates = candidates_by_beat.get(beat_id, [])
        selected_index = _selected_candidate_index(raw_candidates, value)
        if selected_index is not None:
            ordered = [raw_candidates[selected_index]] + [
                item for index, item in enumerate(raw_candidates) if index != selected_index
            ]
        else:
            selected = _director_candidate(base_context, value)
            ordered = ([selected] if selected is not None else []) + raw_candidates
        unique: list[dict[str, Any]] = []
        seen: set[tuple[str, float, float]] = set()
        for candidate in ordered:
            key = _candidate_key(candidate)
            if not key[0] or key in seen or key[2] <= key[1]:
                continue
            seen.add(key)
            unique.append(
                {
                    "source": key[0],
                    "start": key[1],
                    "end": key[2],
                    "quote": str(candidate.get("quote", "")),
                }
            )
            if len(unique) >= candidate_limit:
                break
        selected_by_beat[beat_id] = unique
    return reduced_beats, selected_by_beat


def _a4_word_index(
    word_index: dict[str, Any], candidates: dict[str, list[dict[str, Any]]]
) -> dict[str, list[dict[str, Any]]]:
    windows_by_source: dict[str, list[tuple[float, float]]] = {}
    for values in candidates.values():
        for candidate in values:
            windows_by_source.setdefault(str(candidate["source"]), []).append(
                (
                    float(candidate["start"]) - _A4_WINDOW_PADDING_S,
                    float(candidate["end"]) + _A4_WINDOW_PADDING_S,
                )
            )
    filtered: dict[str, list[dict[str, Any]]] = {}
    for source, windows in windows_by_source.items():
        words = word_index.get(source, [])
        if not isinstance(words, list):
            continue
        filtered[source] = [
            item
            for item in words
            if isinstance(item, dict)
            and any(
                float(item.get("end", 0)) >= start
                and float(item.get("start", 0)) <= end
                for start, end in windows
            )
        ]
    return _compact_word_index(filtered)


def _a4_packed_candidates(candidates: dict[str, list[dict[str, Any]]]) -> str:
    lines = ["# Packed candidate transcripts", ""]
    by_source: dict[str, list[dict[str, Any]]] = {}
    for values in candidates.values():
        for candidate in values:
            by_source.setdefault(str(candidate["source"]), []).append(candidate)
    for source, values in by_source.items():
        lines.append(f"## {source}")
        for candidate in values:
            lines.append(
                f"  [{float(candidate['start']):.3f}-{float(candidate['end']):.3f}] "
                f"{str(candidate.get('quote', ''))}"
            )
        lines.append("")
    return "\n".join(lines)


def _a4_supervisor_context(
    supervisor: dict[str, Any], candidates: dict[str, list[dict[str, Any]]]
) -> dict[str, Any]:
    windows_by_source: dict[str, list[tuple[float, float]]] = {}
    for values in candidates.values():
        for candidate in values:
            windows_by_source.setdefault(str(candidate["source"]), []).append(
                (float(candidate["start"]), float(candidate["end"]))
            )
    takes = []
    for value in supervisor.get("takes", []):
        if not isinstance(value, dict):
            continue
        source = str(value.get("id", ""))
        windows = windows_by_source.get(source)
        if not windows:
            continue
        slips = [
            dict(item)
            for item in value.get("slips", [])
            if isinstance(item, dict)
            and any(
                float(item.get("end", 0)) >= start
                and float(item.get("start", 0)) <= end
                for start, end in windows
            )
        ]
        take = {"id": source, "slips": slips}
        if "duration_s" in value:
            take["duration_s"] = value["duration_s"]
        takes.append(take)
    return {
        "takes": takes,
        "beat_candidates": [
            {"beat": beat, "candidates": values}
            for beat, values in candidates.items()
        ],
    }


def _compact_a4_context(
    base_context: dict[str, Any], *, beat: str | None, candidate_limit: int
) -> dict[str, Any]:
    reduced_beats, candidates = _a4_candidates(
        base_context, beat=beat, candidate_limit=candidate_limit
    )
    director = base_context.get("director", {})
    supervisor = base_context.get("script_supervisor", {})
    word_index = base_context.get("word_index", {})
    context: dict[str, Any] = {
        key: base_context[key]
        for key in ("run_number", "_session_state_path", "_manifest_generation", "schema")
        if key in base_context
    }
    context.update(
        {
            "director": {
                "beats": reduced_beats,
                "beat_order": [str(item["id"]) for item in reduced_beats],
                "order_reason": (
                    str(director.get("order_reason", ""))
                    if isinstance(director, dict)
                    else ""
                ),
            },
            "script_supervisor": _a4_supervisor_context(
                supervisor if isinstance(supervisor, dict) else {}, candidates
            ),
            "takes_packed": _a4_packed_candidates(candidates),
            "word_index": _a4_word_index(
                word_index if isinstance(word_index, dict) else {}, candidates
            ),
        }
    )
    return context


def _budgeted_a4_context(
    base_context: dict[str, Any],
    prompt: str,
    *,
    beat: str | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    last_size = 0
    for candidate_limit in range(_A4_MAX_CANDIDATES_PER_BEAT, 0, -1):
        context = _compact_a4_context(
            base_context, beat=beat, candidate_limit=candidate_limit
        )
        if extra:
            context.update(extra)
        last_size = len(serialize_prompt(prompt, context))
        if last_size <= DEVIN_PROMPT_BUDGET_CHARS:
            return context
    scope = beat or "all beats"
    raise ValueError(
        f"A4 prompt for {scope} has {last_size} characters after compaction; "
        f"the safe budget is {DEVIN_PROMPT_BUDGET_CHARS}"
    )


def _a4_range_findings(
    output: dict[str, Any],
    *,
    allowed_beats: list[str],
    allowed_sources: set[str],
    expected_beat: str | None = None,
) -> list[str]:
    """Return concise deterministic findings for one bounded A4 revision."""
    ranges = output.get("ranges")
    if not isinstance(ranges, list) or not ranges:
        return ["ranges must contain at least one cut"]

    findings: list[str] = []
    beat_positions = {beat: index for index, beat in enumerate(allowed_beats)}
    last_position = -1
    measured_duration = 0.0
    for index, item in enumerate(ranges):
        if not isinstance(item, dict):
            findings.append(f"range {index} is not an object")
            continue
        beat = str(item.get("beat", ""))
        source = str(item.get("source", ""))
        start = float(item.get("start", 0))
        end = float(item.get("end", 0))
        if expected_beat is not None and beat != expected_beat:
            findings.append(f"range {index} must keep beat {expected_beat}, got {beat}")
        elif beat not in beat_positions:
            findings.append(f"range {index} uses unknown or missing beat {beat}")
        else:
            position = beat_positions[beat]
            if position < last_position:
                findings.append(f"range {index} puts beat {beat} out of Director order")
            last_position = max(last_position, position)
        if source not in allowed_sources:
            findings.append(f"range {index} uses unknown source {source}")
        if start < 0 or end <= start:
            findings.append(f"range {index} has invalid interval {start:g}-{end:g}")
        else:
            measured_duration += end - start
        if len(findings) >= 8:
            return findings

    reported_duration = float(output.get("total_duration_s", 0))
    if abs(reported_duration - measured_duration) > 0.25:
        findings.append(
            "total_duration_s does not match the sum of the selected ranges "
            f"({reported_duration:g} vs {measured_duration:g})"
        )
    return findings[:8]


def _chunk_groups(ranges: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: list[dict[str, Any]] = []
    for item in ranges:
        beat = str(item["beat"])
        if groups and groups[-1]["beat"] == beat:
            groups[-1]["seed_ranges"].append(item)
            continue
        slug = re.sub(r"[^a-z0-9]+", "-", beat.lower()).strip("-") or "segment"
        groups.append(
            {
                "id": f"chunk-{len(groups):02d}-{slug}",
                "order": len(groups),
                "beat": beat,
                "seed_ranges": [item],
            }
        )
    return groups


def _copy_chunk_transcripts(
    edit_dir: Path, variant_dir: Path, ranges: list[dict[str, Any]]
) -> None:
    target = variant_dir / "transcripts"
    target.mkdir(parents=True, exist_ok=True)
    for source in {str(item["source"]) for item in ranges}:
        transcript = edit_dir / "transcripts" / f"{source}.json"
        if transcript.exists():
            shutil.copy2(transcript, target / transcript.name)


def _validate_chunk_ranges(
    chunk: dict[str, Any], ranges: list[dict[str, Any]], source_map: dict[str, str]
) -> list[dict[str, Any]]:
    if not ranges:
        raise ValueError(f"{chunk['id']} variant returned no ranges")
    validated: list[dict[str, Any]] = []
    for item in ranges:
        if str(item.get("beat")) != str(chunk["beat"]):
            raise ValueError(f"{chunk['id']} variant returned a different beat")
        source = str(item.get("source"))
        if source not in source_map:
            raise ValueError(f"{chunk['id']} variant selected unknown source {source}")
        start = float(item["start"])
        end = float(item["end"])
        if start < 0 or end <= start:
            raise ValueError(f"{chunk['id']} variant returned an invalid range")
        validated.append(dict(item))
    return validated


def _build_chunk_variant(
    edit_dir: Path,
    state: dict[str, Any],
    chunk: dict[str, Any],
    variant: dict[str, Any],
    source_map: dict[str, str],
    base_context: dict[str, Any],
    motion_source: Path | None,
    approved_catalog: dict[str, Any] | None,
    target_size: tuple[int, int],
    *,
    resume: bool,
) -> dict[str, Any]:
    variant_id = str(variant["id"])
    variant_dir = edit_dir / "chunks" / str(chunk["id"]) / variant_id
    prompt = _prompt("A4")
    context = _budgeted_a4_context(
        base_context,
        prompt,
        beat=str(chunk["beat"]),
        extra={
        "_session_key": f"{chunk['id']}.{variant_id}",
        "chunk": chunk,
        "variant": {
            "id": variant_id,
            "name": variant["name"],
            "direction": variant["direction"],
        },
        },
    )
    decision = _cached_role(
        edit_dir,
        f"A4-{chunk['id']}-{variant_id}",
        "A4",
        prompt,
        context,
        resume=resume,
        semantic_validator=lambda output: _a4_range_findings(
            output,
            allowed_beats=[str(chunk["beat"])],
            allowed_sources=set(source_map),
            expected_beat=str(chunk["beat"]),
        ),
        max_feedback_rounds=1,
    )
    ranges = _validate_chunk_ranges(chunk, decision["ranges"], source_map)
    overlays: list[dict[str, Any]] = []
    if bool(variant["motion"]):
        if motion_source is None:
            raise RuntimeError("the motion variant requires a configured template source")
        overlays = _graphics_overlays(
            variant_dir,
            state,
            ranges,
            motion_source,
            target_size,
            resume=resume,
            approved_catalog=approved_catalog,
            required_overlays=1,
        )
        if len(overlays) != 1:
            raise RuntimeError(
                f"the motion variant requires exactly one overlay, got {len(overlays)}"
            )
    _copy_chunk_transcripts(edit_dir, variant_dir, ranges)
    preview_edl = {
        "sources": source_map,
        "ranges": ranges,
        "grade": state["brand"].get("grade", "neutral_punch"),
        "overlays": overlays,
        "subtitles": "master.srt",
    }
    edl_path = variant_dir / "edl.json"
    preview = variant_dir / "preview.mp4"
    reuse_preview = (
        resume
        and preview.exists()
        and edl_path.exists()
        and _read_json(edl_path) == preview_edl
    )
    _write_json(edl_path, preview_edl)
    if not reuse_preview:
        render(variant_dir, preview, preview=True)
    measured = probe(preview)
    duration = sum(float(item["end"]) - float(item["start"]) for item in ranges)
    final_overlays = []
    for overlay in overlays:
        item = dict(overlay)
        item["file"] = str((variant_dir / str(overlay["file"])).resolve())
        final_overlays.append(item)
    return {
        "id": variant_id,
        "label": variant["label"],
        "name": variant["name"],
        "strategy": variant["direction"],
        "status": "ready",
        "duration_s": measured.duration_s,
        "expected_duration_s": duration,
        "probe": _file_free_probe_context(measured),
        "ranges": ranges,
        "overlays": final_overlays,
        "preview": preview.relative_to(edit_dir).as_posix(),
        "notes": str(decision.get("notes", "")),
    }


def _variant_supervisor_context(
    chunk: dict[str, Any], variants: list[dict[str, Any]], base_context: dict[str, Any]
) -> dict[str, Any]:
    """Build a compact, file-free comparison payload for the optional A5 review."""
    comparable = []
    for variant in variants:
        comparable.append(
            {
                "id": str(variant["id"]),
                "name": str(variant.get("name", "")),
                "strategy": str(variant.get("strategy", "")),
                "duration_s": float(variant.get("duration_s", 0)),
                "expected_duration_s": float(variant.get("expected_duration_s", 0)),
                "probe": (
                    dict(variant["probe"])
                    if isinstance(variant.get("probe"), dict)
                    else {}
                ),
                "ranges": [
                    {
                        "source": str(item.get("source", "")),
                        "start": float(item.get("start", 0)),
                        "end": float(item.get("end", 0)),
                        "quote": str(item.get("quote", "")),
                        "reason": str(item.get("reason", "")),
                    }
                    for item in variant.get("ranges", [])
                    if isinstance(item, dict)
                ],
                "overlays": [
                    {
                        "template_id": str(item.get("template_id", "")),
                        "beat": str(item.get("beat", "")),
                        "duration": float(item.get("duration", 0)),
                        "reason": str(item.get("reason", "")),
                    }
                    for item in variant.get("overlays", [])
                    if isinstance(item, dict)
                ],
                "notes": str(variant.get("notes", "")),
            }
        )
    return {
        "run_number": base_context["run_number"],
        "_session_state_path": base_context["_session_state_path"],
        "_session_key": ".".join(
            filter(
                None,
                (
                    str(base_context.get("_manifest_generation", "")),
                    str(chunk["id"]),
                    "recommendation",
                ),
            )
        ),
        "chunk": {
            "id": str(chunk["id"]),
            "beat": str(chunk["beat"]),
            "variants": comparable,
        },
    }


def _recommend_chunk_variant(
    edit_dir: Path,
    chunk: dict[str, Any],
    variants: list[dict[str, Any]],
    base_context: dict[str, Any],
    *,
    resume: bool,
) -> dict[str, Any]:
    """Ask A5 for a preference without making review depend on agent availability."""
    try:
        generation = str(base_context.get("_manifest_generation", ""))
        cache_key = "-".join(filter(None, ("A5", generation, str(chunk["id"]))))
        recommendation = _cached_role(
            edit_dir,
            cache_key,
            "A5",
            _prompt("A5"),
            _variant_supervisor_context(chunk, variants, base_context),
            resume=resume,
        )
        variant_id = str(recommendation["variant_id"])
        if variant_id not in {str(item["id"]) for item in variants}:
            raise ValueError(f"A5 selected unknown variant {variant_id}")
        return {
            "status": "ready",
            "variant_id": variant_id,
            "reason": str(recommendation["reason"]),
        }
    except Exception:
        return {
            "status": "unavailable",
            "variant_id": None,
            "reason": "Die KI-Empfehlung ist derzeit nicht verfügbar.",
        }


def _chunk_base_context(
    edit_dir: Path, state: dict[str, Any], *, manifest_generation: str = ""
) -> dict[str, Any]:
    supervisor_path = edit_dir / "script-supervisor.json"
    director_path = edit_dir / "director.json"
    director = (
        _read_json(director_path)
        if director_path.exists()
        else {
            "beats": state["beats"],
            "beat_order": [beat["id"] for beat in state["beats"]],
        }
    )
    return {
        "run_number": int(state["meta"]["iteration"]),
        "_session_state_path": str(edit_dir / "sessions.json"),
        "_manifest_generation": manifest_generation,
        "schema": _schema(),
        "director": director,
        "script_supervisor": _read_json(supervisor_path) if supervisor_path.exists() else {},
        "takes_packed": (edit_dir / "takes_packed.md").read_text(encoding="utf-8"),
        "word_index": _read_json(edit_dir / "word_index.json"),
    }


def build_chunk_variants(
    edit_dir: Path,
    sources: list[Path],
    template_source: Path | None = None,
    *,
    resume: bool = False,
) -> dict[str, Any]:
    """Create two independently agent-edited previews for every semantic chunk."""
    schema = _schema()
    state = load(edit_dir, schema)
    if not sources:
        raise RuntimeError("no source videos available for chunking")
    generation = uuid.uuid4().hex
    base_context = _chunk_base_context(
        edit_dir, state, manifest_generation=generation
    )
    source_map = {source.stem: str(source.resolve()) for source in sources}
    allowed_beats = [
        str(beat["id"])
        for beat in base_context["director"].get("beats", [])
        if isinstance(beat, dict) and str(beat.get("status", "")) != "missing"
    ]
    a4_prompt = _prompt("A4")
    chunk_plan = _cached_role(
        edit_dir,
        "A4-chunk-plan",
        "A4",
        a4_prompt,
        _budgeted_a4_context(base_context, a4_prompt),
        resume=resume,
        semantic_validator=lambda output: _a4_range_findings(
            output,
            allowed_beats=allowed_beats,
            allowed_sources=set(source_map),
        ),
        max_feedback_rounds=1,
    )
    chunks = _chunk_groups(chunk_plan["ranges"])
    if not chunks:
        raise RuntimeError("A4 produced no semantic chunks")
    first_source = probe(sources[0])
    target_size = (1080, 1920) if first_source.height > first_source.width else (1920, 1080)
    motion_source = _motion_source(template_source, state)
    if motion_source is None:
        raise RuntimeError(
            "PALANTUM_TEMPLATE_SOURCE fehlt; Variante B benötigt genau eine "
            "validierte Motion Graphic."
        )
    catalog_path = edit_dir / "scene-catalog.json"
    approved_catalog = (
        _read_json(catalog_path) if motion_source is not None and catalog_path.exists() else None
    )
    manifest: dict[str, Any] = {
        "version": 1,
        "generation_id": generation,
        "status": "building",
        "recommendations_status": "pending",
        "sources": source_map,
        "chunks": [
            {
                "id": chunk["id"],
                "order": chunk["order"],
                "beat": chunk["beat"],
                "selected": None,
                "variants": [],
            }
            for chunk in chunks
        ],
    }
    _write_json(edit_dir / "chunks.json", manifest)
    jobs: dict[tuple[str, str], Any] = {}
    with ThreadPoolExecutor(max_workers=min(4, len(chunks) * 2)) as executor:
        for chunk in chunks:
            for variant in _CHUNK_VARIANTS:
                jobs[(str(chunk["id"]), str(variant["id"]))] = executor.submit(
                    _build_chunk_variant,
                    edit_dir,
                    state,
                    chunk,
                    variant,
                    source_map,
                    base_context,
                    motion_source,
                    approved_catalog,
                    target_size,
                    resume=resume,
                )
        for manifest_chunk in manifest["chunks"]:
            manifest_chunk["variants"] = [
                jobs[(str(manifest_chunk["id"]), str(variant["id"]))].result()
                for variant in _CHUNK_VARIANTS
            ]
    manifest["status"] = "review"
    _write_json(edit_dir / "chunks.json", manifest)
    return manifest


def recommend_chunk_variants(
    edit_dir: Path, manifest: dict[str, Any], *, resume: bool = False
) -> dict[str, dict[str, Any]]:
    """Compute optional A5 recommendations without mutating the chunk manifest."""
    chunks = [item for item in manifest.get("chunks", []) if isinstance(item, dict)]
    if not chunks:
        return {}
    generation = str(manifest.get("generation_id", ""))
    state = load(edit_dir, _schema())
    base_context = _chunk_base_context(
        edit_dir, state, manifest_generation=generation
    )
    with ThreadPoolExecutor(max_workers=min(4, len(chunks))) as executor:
        pending = {
            str(chunk["id"]): executor.submit(
                _recommend_chunk_variant,
                edit_dir,
                chunk,
                chunk.get("variants", []),
                base_context,
                resume=resume,
            )
            for chunk in chunks
        }
        return {chunk_id: future.result() for chunk_id, future in pending.items()}


def finalize_chunk_variants(edit_dir: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    """Render the final timeline from the variant selected for every chunk."""
    manifest_path = edit_dir / "chunks.json"
    manifest = _read_json(manifest_path)
    chunks = manifest.get("chunks", [])
    if not chunks:
        raise RuntimeError("no chunk variants are available")
    ranges: list[dict[str, Any]] = []
    overlays: list[dict[str, Any]] = []
    offset = 0.0
    for chunk in chunks:
        selected_id = chunk.get("selected")
        selected = next(
            (item for item in chunk.get("variants", []) if item.get("id") == selected_id),
            None,
        )
        if selected is None:
            raise RuntimeError(f"{chunk.get('id')} has no selected variant")
        selected_ranges = [dict(item) for item in selected.get("ranges", [])]
        ranges.extend(selected_ranges)
        for overlay in selected.get("overlays", []):
            item = dict(overlay)
            item["start_in_output"] = offset + float(item["start_in_output"])
            overlays.append(item)
        offset += sum(float(item["end"]) - float(item["start"]) for item in selected_ranges)
    state = load(edit_dir, _schema())
    edl = {
        "sources": manifest["sources"],
        "ranges": ranges,
        "grade": state["brand"].get("grade", "neutral_punch"),
        "overlays": overlays,
        "subtitles": "master.srt",
    }
    manifest["status"] = "finalizing"
    _write_json(manifest_path, manifest)
    _write_json(edit_dir / "edl.json", edl)
    output = edit_dir / "final.mp4"
    render(edit_dir, output)
    result = probe(output)
    expected_total = sum(float(item["end"]) - float(item["start"]) for item in ranges)
    qc_context = {
        "_session_state_path": str(edit_dir / "sessions.json"),
        "probe": _probe_context(result),
        "duration_check": _duration_check(expected_total, result.duration_s),
        "audio_check": _audio_check(output, ranges),
        "edl": edl,
        "timeline_views": _timeline_views(output, ranges, edit_dir),
        "subtitle_file": str(edit_dir / "master.srt"),
    }
    qc = _merge_motion_qc(
        run_role("A7", _prompt("A7"), qc_context, None), edl["overlays"]
    )
    _write_json(edit_dir / "qc.json", qc)
    manifest["status"] = "done"
    _write_json(manifest_path, manifest)
    return edl, qc


def cut(
    edit_dir: Path,
    sources: list[Path],
    template_source: Path | None = None,
    *,
    force: bool = False,
    resume: bool = False,
) -> tuple[dict[str, Any], dict[str, Any]]:
    schema = _schema()
    state = load(edit_dir, schema)
    allowed, notes = gate(state, schema)
    if not allowed and not force:
        raise RuntimeError(
            json.dumps({"gate": "blocked", "director_notes": notes}, ensure_ascii=False)
        )
    supervisor_path = edit_dir / "script-supervisor.json"
    director_path = edit_dir / "director.json"
    context = {
        "run_number": int(state["meta"]["iteration"]),
        "_session_state_path": str(edit_dir / "sessions.json"),
        "schema": schema,
        "director": (
            _read_json(director_path)
            if director_path.exists()
            else {
                "beats": state["beats"],
                "beat_order": [beat["id"] for beat in state["beats"]],
            }
        ),
        "script_supervisor": _read_json(supervisor_path) if supervisor_path.exists() else {},
        "takes_packed": (edit_dir / "takes_packed.md").read_text(encoding="utf-8"),
        "word_index": _read_json(edit_dir / "word_index.json"),
    }
    a4 = _cached_role(edit_dir, "A4", "A4", _prompt("A4"), context, resume=resume)
    motion_source = _motion_source(template_source, state)
    if not sources:
        raise RuntimeError("no source videos available for cutting")
    first_source = probe(sources[0])
    target_size = (1080, 1920) if first_source.height > first_source.width else (1920, 1080)
    source_map = {source.stem: str(source) for source in sources}
    edl = {
        "sources": source_map,
        "ranges": a4["ranges"],
        "grade": state["brand"].get("grade", "neutral_punch"),
        "overlays": (
            _graphics_overlays(
                edit_dir,
                state,
                a4["ranges"],
                motion_source,
                target_size,
                resume=resume,
            )
            if motion_source is not None
            else []
        ),
        "subtitles": "master.srt",
    }
    (edit_dir / "edl.json").write_text(
        json.dumps(edl, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    output = edit_dir / "final.mp4"
    render(edit_dir, output)
    result = probe(output)
    expected_total = sum(float(item["end"]) - float(item["start"]) for item in edl["ranges"])
    measured = result.duration_s
    views = _timeline_views(output, a4["ranges"], edit_dir)
    qc_context = {
        "probe": _probe_context(result),
        "duration_check": _duration_check(expected_total, measured),
        "audio_check": _audio_check(output, edl["ranges"]),
        "edl": edl,
        "timeline_views": views,
        "subtitle_file": str(edit_dir / "master.srt"),
    }
    a7 = _merge_motion_qc(
        run_role("A7", _prompt("A7"), qc_context, None), edl["overlays"]
    )
    if a7.get("verdict") == "fail":
        retry_context = dict(context)
        retry_context["qc_findings"] = a7
        a4 = _cached_role(
            edit_dir,
            "A4-retry",
            "A4",
            _prompt("A4"),
            retry_context,
            resume=resume,
        )
        edl["ranges"] = a4["ranges"]
        edl["overlays"] = (
            _graphics_overlays(
                edit_dir,
                state,
                a4["ranges"],
                motion_source,
                target_size,
                resume=resume,
            )
            if motion_source is not None
            else []
        )
        (edit_dir / "edl.json").write_text(
            json.dumps(edl, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        render(edit_dir, output)
        result = probe(output)
        qc_context["probe"] = _probe_context(result)
        qc_context["edl"] = edl
        measured = result.duration_s
        expected_total = sum(float(item["end"]) - float(item["start"]) for item in edl["ranges"])
        qc_context["duration_check"] = _duration_check(expected_total, measured)
        qc_context["audio_check"] = _audio_check(output, edl["ranges"])
        qc_context["timeline_views"] = _timeline_views(output, a4["ranges"], edit_dir)
        a7 = _merge_motion_qc(
            run_role("A7", _prompt("A7"), qc_context, None), edl["overlays"]
        )
    (edit_dir / "qc.json").write_text(
        json.dumps(a7, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    return edl, a7
