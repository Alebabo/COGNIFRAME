from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest

from pitchcraft.cli import _confirm_rough_cut, _cut_sources
from pitchcraft.engine.transcribe import whisper_to_scribe
from pitchcraft.orchestrator import _apply_coverage, _audio_check, _debate, cut, gate
from pitchcraft.state import coverage_score, empty_state

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = json.loads((ROOT / "pitchcraft/schemas/yc_pitch_60s.json").read_text())


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


@pytest.mark.parametrize("answer", ["y", "yes", "j", "ja", " JA "])
def test_rough_cut_confirmation_accepts_english_and_german_yes(answer: str) -> None:
    with patch("builtins.input", return_value=answer):
        assert _confirm_rough_cut()


def test_rough_cut_confirmation_defaults_to_no_without_input() -> None:
    with patch("builtins.input", side_effect=EOFError):
        assert not _confirm_rough_cut()


def test_cut_uses_only_sources_recorded_during_ingest(tmp_path: Path) -> None:
    selected = tmp_path / "selected.mp4"
    unrelated = tmp_path / "unrelated.mp4"
    selected.touch()
    unrelated.touch()
    state: dict[str, object] = {"meta": {"sources": [str(selected)]}}

    assert _cut_sources(tmp_path, state) == [selected.resolve()]


def test_forced_cut_skips_gate_and_motion_when_no_template_is_configured(
    tmp_path: Path,
) -> None:
    state = empty_state(SCHEMA)
    source = tmp_path / "take.mp4"
    (tmp_path / "takes_packed.md").write_text("# takes\n", encoding="utf-8")
    (tmp_path / "word_index.json").write_text("{}", encoding="utf-8")
    ranges = [
        {
            "source": source.stem,
            "start": 0.0,
            "end": 1.0,
            "beat": "HOOK",
            "quote": "Hello",
            "reason": "best available take",
        }
    ]
    probe_result = SimpleNamespace(width=1920, height=1080, duration_s=1.0)

    def run(role: str, *_args: object, **_kwargs: object) -> dict[str, object]:
        if role == "A4":
            return {"ranges": ranges}
        return {"verdict": "pass", "findings": [{"check": "duration ≥ target"}]}

    with (
        patch("pitchcraft.orchestrator.load", return_value=state),
        patch("pitchcraft.orchestrator.run_role", side_effect=run),
        patch("pitchcraft.orchestrator._motion_source", return_value=None),
        patch("pitchcraft.orchestrator.probe", return_value=probe_result),
        patch("pitchcraft.orchestrator.render"),
        patch("pitchcraft.orchestrator._probe_context", return_value={}),
        patch("pitchcraft.orchestrator._timeline_views", return_value=[]),
        patch("pitchcraft.orchestrator._audio_check", return_value={}),
    ):
        edl, qc = cut(tmp_path, [source], force=True)

    assert edl["overlays"] == []
    assert qc["verdict"] == "pass"
    assert (tmp_path / "edl.json").exists()
    assert "≥" in (tmp_path / "qc.json").read_text(encoding="utf-8")


def test_whisper_adapter_inserts_spacing_and_constant_speaker() -> None:
    fixture = json.loads((ROOT / "tests/fixtures/whisper_verbose.json").read_text())
    result: Any = whisper_to_scribe(fixture)
    assert [item["type"] for item in result["words"]] == ["word", "spacing", "word"]
    assert result["words"][1]["start"] == 0.4
    assert result["words"][1]["end"] == 1.0
    assert {item["speaker_id"] for item in result["words"]} == {"speaker_0"}


def test_audio_check_reports_loud_and_silent_segments(tmp_path: Path, monkeypatch: Any) -> None:
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

    monkeypatch.setattr("pitchcraft.orchestrator.subprocess.run", run)
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
    strategist: dict[str, Any] = {"rulings": [], "content_findings": []}
    _apply_coverage(state, director, strategist, SCHEMA)
    assert state["resolved_notes"][0]["beat"] == "TRACTION"
    assert state["resolved_notes"][0]["closed_by"] == "take_traction"
    assert state["resolved_notes"][0]["shot"]["line"] == "Say a number"
