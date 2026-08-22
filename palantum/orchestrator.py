from __future__ import annotations

import json
import re
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

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
from palantum.state import coverage_score, load, save


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise ValueError(f"expected object in {path}")
    return value


def _schema() -> dict[str, Any]:
    path = Path(__file__).resolve().parent / "schemas" / "yc_pitch_60s.json"
    return _read_json(path)


def _prompt(role_id: str) -> str:
    names = {
        "A1": "a1_script_supervisor",
        "A2": "a2_director",
        "A3": "a3_strategist",
        "A4": "a4_cutter",
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


def analyze(edit_dir: Path, sources: list[Path]) -> dict[str, Any]:
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
    a1_context = {
        "run_number": context["run_number"],
        "_session_state_path": context["_session_state_path"],
        "schema": context["schema"],
        "takes_packed": context["takes_packed"],
        "probe": context["probe"],
    }
    a1 = run_role("A1", _prompt("A1"), a1_context, None)
    context["a1"] = a1
    context["script_supervisor"] = a1
    context["beat_candidates"] = a1["beat_candidates"]
    a2 = run_role("A2", _prompt("A2"), context, None)
    context["a2"] = a2
    context["director"] = a2
    a3 = run_role("A3", _prompt("A3"), context, None)
    _debate(state, a2, a3)
    _apply_coverage(state, a2, a3, schema)
    state["meta"]["sources"] = [str(source) for source in all_sources]
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


def cut(edit_dir: Path, sources: list[Path]) -> tuple[dict[str, Any], dict[str, Any]]:
    schema = _schema()
    state = load(edit_dir, schema)
    allowed, notes = gate(state, schema)
    if not allowed:
        raise RuntimeError(
            json.dumps({"gate": "blocked", "director_notes": notes}, ensure_ascii=False)
        )
    context = _context(edit_dir, schema, state, int(state["meta"]["iteration"]))
    a4 = run_role("A4", _prompt("A4"), context, None)
    source_map = {source.stem: str(source) for source in sources}
    edl = {
        "sources": source_map,
        "ranges": a4["ranges"],
        "grade": state["brand"].get("grade", "neutral_punch"),
        "overlays": [],
        "subtitles": "master.srt",
    }
    (edit_dir / "edl.json").write_text(json.dumps(edl, indent=2, ensure_ascii=False))
    output = edit_dir / "final.mp4"
    render(edit_dir, output)
    result = probe(output)
    expected_total = sum(float(item["end"]) - float(item["start"]) for item in edl["ranges"])
    measured = result.duration_s
    views: list[str] = []
    offset = 0.0
    for index, item in enumerate(a4["ranges"]):
        duration = float(item["end"]) - float(item["start"])
        view = edit_dir / "verify" / f"cut_{index:02d}.png"
        try:
            timeline_view(output, max(0, offset - 1.5), offset + duration + 1.5, view)
            views.append(str(view))
        except (subprocess.CalledProcessError, FileNotFoundError):
            pass
        offset += duration
    qc_context = {
        "probe": _probe_context(result),
        "duration_check": _duration_check(expected_total, measured),
        "audio_check": _audio_check(output, edl["ranges"]),
        "edl": edl,
        "timeline_views": views,
        "subtitle_file": str(edit_dir / "master.srt"),
    }
    a7 = _normalize_qc_verdict(run_role("A7", _prompt("A7"), qc_context, None))
    if a7.get("verdict") == "fail":
        retry_context = dict(context)
        retry_context["qc_findings"] = a7
        a4 = run_role("A4", _prompt("A4"), retry_context, None)
        edl["ranges"] = a4["ranges"]
        (edit_dir / "edl.json").write_text(json.dumps(edl, indent=2, ensure_ascii=False))
        render(edit_dir, output)
        result = probe(output)
        qc_context["probe"] = _probe_context(result)
        qc_context["edl"] = edl
        measured = result.duration_s
        expected_total = sum(float(item["end"]) - float(item["start"]) for item in edl["ranges"])
        qc_context["duration_check"] = _duration_check(expected_total, measured)
        qc_context["audio_check"] = _audio_check(output, edl["ranges"])
        a7 = _normalize_qc_verdict(run_role("A7", _prompt("A7"), qc_context, None))
    (edit_dir / "qc.json").write_text(json.dumps(a7, indent=2, ensure_ascii=False))
    return edl, a7
