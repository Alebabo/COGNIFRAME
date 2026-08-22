from __future__ import annotations

import json
from pathlib import Path

from palantum.engine.transcribe import whisper_to_scribe
from palantum.orchestrator import _apply_coverage, _audio_check, _debate, gate
from palantum.state import coverage_score, empty_state

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = json.loads((ROOT / "palantum/schemas/yc_pitch_60s.json").read_text())


def test_coverage_score_weights_required_beats() -> None:
    state = empty_state(SCHEMA)
    state["beats"][0]["status"] = "covered"
    assert coverage_score(state["beats"], SCHEMA) == 0.1667


def test_gate_requires_required_beats_at_least_weak() -> None:
    state = empty_state(SCHEMA)
    assert not gate(state, SCHEMA)[0]
    for beat in state["beats"]:
        if beat["id"] != "DEMO":
            beat["status"] = "weak"
    assert not gate(state, SCHEMA)[0]
    state["beats"][3]["status"] = "weak"
    assert gate(state, SCHEMA)[0]


def test_whisper_adapter_inserts_spacing_and_constant_speaker() -> None:
    fixture = json.loads((ROOT / "tests/fixtures/whisper_verbose.json").read_text())
    result = whisper_to_scribe(fixture)
    assert [item["type"] for item in result["words"]] == ["word", "spacing", "word"]
    assert result["words"][1]["start"] == 0.4
    assert result["words"][1]["end"] == 1.0
    assert {item["speaker_id"] for item in result["words"]} == {"speaker_0"}


def test_audio_check_reports_loud_and_silent_segments(tmp_path: Path, monkeypatch: object) -> None:
    class Completed:
        stderr = "mean_volume: -14.0 dB\nmax_volume: -1.0 dB\n"

    calls = 0

    def run(*args: object, **kwargs: object) -> Completed:
        nonlocal calls
        calls += 1
        result = Completed()
        if calls == 2:
            result.stderr = "mean_volume: -100.0 dB\nmax_volume: -100.0 dB\n"
        return result

    monkeypatch.setattr("palantum.orchestrator.subprocess.run", run)
    result = _audio_check(
        tmp_path / "final.mp4",
        [
            {"start": 0.0, "end": 1.0},
            {"start": 1.0, "end": 2.0},
        ],
    )
    assert result["silent_segments"] == [1]
    assert result["segments"][0]["start_s"] == 0.0
    assert result["segments"][1]["end_s"] == 2.0


def test_strategist_wins_weak_vs_covered_and_logs_debate() -> None:
    state = empty_state(SCHEMA)
    director = {
        "beats": [
            {
                "id": beat["id"],
                "status": "covered",
                "source": "take",
                "range": [0, 1],
                "reason": "director",
            }
            for beat in SCHEMA["beats"]
        ],
        "director_notes": [],
        "beat_order": [beat["id"] for beat in SCHEMA["beats"]],
        "order_reason": "schema",
    }
    strategist = {
        "rulings": [
            {"beat": "HOOK", "verdict": "weak", "quote": "claim", "reason": "not concrete"}
        ],
        "content_findings": [],
    }
    _debate(state, director, strategist)
    _apply_coverage(state, director, strategist, SCHEMA)
    assert state["beats"][0]["status"] == "weak"
    assert state["debate_log"][0]["resolved"] == "weak"


def test_resolved_note_is_preserved_with_closing_source() -> None:
    state = empty_state(SCHEMA)
    state["director_notes"] = [
        {
            "beat": "TRACTION",
            "status": "missing",
            "why": "numbers",
            "shot": {
                "line": "Say a number",
                "framing": "take",
                "duration_s": 4,
                "delivery": "calm",
            },
            "impact": "high",
            "slot_in_timeline": 4,
        }
    ]
    director = {
        "beats": [
            {
                "id": beat["id"],
                "status": "covered" if beat["id"] == "TRACTION" else "missing",
                "source": "take_traction" if beat["id"] == "TRACTION" else None,
                "range": [0, 1] if beat["id"] == "TRACTION" else None,
                "reason": "measured",
            }
            for beat in SCHEMA["beats"]
        ],
        "director_notes": [],
        "beat_order": [beat["id"] for beat in SCHEMA["beats"]],
        "order_reason": "schema",
    }
    strategist = {"rulings": [], "content_findings": []}
    _apply_coverage(state, director, strategist, SCHEMA)
    assert state["resolved_notes"][0]["beat"] == "TRACTION"
    assert state["resolved_notes"][0]["closed_by"] == "take_traction"
    assert state["resolved_notes"][0]["shot"]["line"] == "Say a number"
