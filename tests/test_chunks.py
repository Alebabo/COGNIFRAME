from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from palantum.agents.backends.devin import DEVIN_PROMPT_BUDGET_CHARS, serialize_prompt
from palantum.engine.videouse import ProbeResult
from palantum.orchestrator import (
    _a4_range_findings,
    _budgeted_a4_context,
    _compact_word_index,
    _recommend_chunk_variant,
    _variant_supervisor_context,
    build_chunk_variants,
    finalize_chunk_variants,
    recommend_chunk_variants,
)


def test_compact_word_index_rounds_timestamp_noise() -> None:
    assert _compact_word_index(
        {
            "take": [
                {"text": "hello", "start": 0.1234567, "end": 1.9876543},
            ]
        }
    ) == {"take": [{"text": "hello", "start": 0.123, "end": 1.988}]}


def test_a4_context_keeps_selected_candidate_and_drops_irrelevant_words() -> None:
    candidates = [
        {
            "source": "take",
            "start": float(index * 10),
            "end": float(index * 10 + 2),
            "quote": f"candidate {index}",
        }
        for index in range(5)
    ]
    base = {
        "run_number": 1,
        "schema": {"target_runtime_s": 60},
        "director": {
            "beats": [
                {
                    "id": "HOOK",
                    "status": "covered",
                    "source": "take",
                    "range": [20.0, 22.0],
                    "reason": "strongest",
                },
                {"id": "PROBLEM", "status": "missing"},
            ],
            "beat_order": ["HOOK", "PROBLEM"],
        },
        "script_supervisor": {
            "takes": [
                {
                    "id": "take",
                    "duration_s": 120,
                    "slips": [
                        {"start": 20.2, "end": 20.4, "kind": "misspeak"},
                        {"start": 118.0, "end": 119.0, "kind": "false_start"},
                    ],
                }
            ],
            "beat_candidates": [{"beat": "HOOK", "candidates": candidates}],
        },
        "takes_packed": "irrelevant " * 10_000,
        "word_index": {
            "take": [
                {"text": "near", "start": 20.123456, "end": 20.456789},
                {"text": "far", "start": 119.0, "end": 119.5},
            ]
        },
    }

    context = _budgeted_a4_context(base, "cut", beat="HOOK")
    kept = context["script_supervisor"]["beat_candidates"][0]["candidates"]

    assert [item["start"] for item in kept] == [20.0, 0.0, 10.0, 30.0]
    assert context["word_index"] == {
        "take": [{"text": "near", "start": 20.123, "end": 20.457}]
    }
    assert context["director"]["beat_order"] == ["HOOK"]
    assert context["script_supervisor"]["takes"][0]["slips"] == [
        {"start": 20.2, "end": 20.4, "kind": "misspeak"}
    ]
    assert len(serialize_prompt("cut", context)) <= DEVIN_PROMPT_BUDGET_CHARS


def test_a4_context_reports_minimal_prompt_that_cannot_fit_budget() -> None:
    oversized = "x" * DEVIN_PROMPT_BUDGET_CHARS
    base = {
        "schema": {},
        "director": {
            "beats": [
                {
                    "id": "HOOK",
                    "status": "covered",
                    "source": "take",
                    "range": [0.0, 1.0],
                }
            ]
        },
        "script_supervisor": {
            "beat_candidates": [
                {
                    "beat": "HOOK",
                    "candidates": [
                        {"source": "take", "start": 0.0, "end": 1.0, "quote": oversized}
                    ],
                }
            ]
        },
        "word_index": {},
    }

    with pytest.raises(ValueError, match="A4 prompt for HOOK"):
        _budgeted_a4_context(base, "cut", beat="HOOK")


def test_a4_range_findings_cover_order_source_interval_and_duration() -> None:
    findings = _a4_range_findings(
        {
            "ranges": [
                {
                    "source": "unknown",
                    "start": 2.0,
                    "end": 1.0,
                    "beat": "PROBLEM",
                },
                {
                    "source": "take",
                    "start": 3.0,
                    "end": 4.0,
                    "beat": "HOOK",
                },
            ],
            "total_duration_s": 9.0,
        },
        allowed_beats=["HOOK", "PROBLEM"],
        allowed_sources={"take"},
    )

    assert findings == [
        "range 0 uses unknown source unknown",
        "range 0 has invalid interval 2-1",
        "range 1 puts beat HOOK out of Director order",
        "total_duration_s does not match the sum of the selected ranges (9 vs 1)",
    ]


def _state(edit_dir: Path, source: Path) -> None:
    (edit_dir / "coverage.json").write_text(
        json.dumps(
            {
                "schema": "yc_pitch_60s",
                "meta": {"iteration": 1, "sources": [str(source)]},
                "beats": [
                    {"id": "HOOK", "status": "covered"},
                    {"id": "PROBLEM", "status": "covered"},
                ],
                "director_notes": [],
                "resolved_notes": [],
                "debate_log": [],
                "brand": {"grade": "neutral_punch"},
                "coverage_score": 1.0,
            }
        ),
        encoding="utf-8",
    )
    (edit_dir / "director.json").write_text(
        json.dumps(
            {
                "beats": [
                    {"id": "HOOK", "status": "covered"},
                    {"id": "PROBLEM", "status": "covered"},
                ],
                "beat_order": ["HOOK", "PROBLEM"],
            }
        ),
        encoding="utf-8",
    )
    (edit_dir / "script-supervisor.json").write_text("{}", encoding="utf-8")
    (edit_dir / "takes_packed.md").write_text("packed", encoding="utf-8")
    (edit_dir / "word_index.json").write_text("{}", encoding="utf-8")


def _probe(path: Path) -> ProbeResult:
    return ProbeResult(path, 4.0, 1920, 1080, 24.0, True, [])


def test_build_chunk_variants_creates_two_options_in_parallel(tmp_path: Path) -> None:
    edit_dir = tmp_path / "edit"
    edit_dir.mkdir()
    source = tmp_path / "take.mp4"
    source.write_bytes(b"video")
    template = tmp_path / "templates.zip"
    template.write_bytes(b"templates")
    _state(edit_dir, source)
    approved_catalog = {
        "source": {"sha256": "abc"},
        "scenes": [
            {
                "id": "hero-stat-callout",
                "type": "PROBLEM",
                "status": "ok",
                "confidence": 0.9,
            }
        ],
    }
    (edit_dir / "scene-catalog.json").write_text(json.dumps(approved_catalog), encoding="utf-8")
    seed = [
        {
            "source": "take",
            "start": 0.0,
            "end": 2.0,
            "beat": "HOOK",
            "quote": "hook",
            "reason": "clear",
        },
        {
            "source": "take",
            "start": 2.0,
            "end": 4.0,
            "beat": "PROBLEM",
            "quote": "problem",
            "reason": "clear",
        },
    ]
    active = 0
    maximum = 0
    lock = threading.Lock()
    role_calls: list[str] = []
    reasoned_a4_calls: list[str] = []

    def cached_role(
        _edit_dir: Path,
        cache_key: str,
        _role_id: str,
        _prompt: str,
        context: dict[str, object],
        *,
        resume: bool,
        **kwargs: object,
    ) -> dict[str, object]:
        del resume
        role_calls.append(cache_key)
        if cache_key.startswith("A4"):
            assert callable(kwargs["semantic_validator"])
            assert kwargs["max_feedback_rounds"] == 1
            reasoned_a4_calls.append(cache_key)
        if cache_key == "A4-chunk-plan":
            return {"ranges": seed, "total_duration_s": 4.0, "notes": "plan"}
        if cache_key.startswith("A5-"):
            return {"variant_id": "b", "reason": "Die Motion-Fassung stützt den Beat."}
        nonlocal active, maximum
        with lock:
            active += 1
            maximum = max(maximum, active)
        time.sleep(0.04)
        with lock:
            active -= 1
        chunk = context["chunk"]
        assert isinstance(chunk, dict)
        ranges = chunk["seed_ranges"]
        return {"ranges": ranges, "total_duration_s": 2.0, "notes": cache_key}

    def fake_render(_edit_dir: Path, output: Path, preview: bool = False) -> Path:
        assert preview is True
        output.write_bytes(b"preview")
        return output

    def fake_graphics(*_args: object, **_kwargs: object) -> list[dict[str, object]]:
        return [
            {
                "file": "animations/slot-01/overlay.mov",
                "start_in_output": 0.25,
                "duration": 1.0,
                "slot_id": "slot-01",
                "template_id": "hero-stat-callout",
                "beat": "HOOK",
            }
        ]

    with (
        patch("palantum.orchestrator._cached_role", side_effect=cached_role),
        patch("palantum.orchestrator._motion_source", return_value=template),
        patch("palantum.orchestrator._graphics_overlays", side_effect=fake_graphics) as graphics,
        patch("palantum.orchestrator.render", side_effect=fake_render),
        patch("palantum.orchestrator.probe", side_effect=_probe),
    ):
        manifest = build_chunk_variants(edit_dir, [source], template)
        assert not any(key.startswith("A5-") for key in role_calls)
        persisted_before_a5 = json.loads(
            (edit_dir / "chunks.json").read_text(encoding="utf-8")
        )
        recommendations = recommend_chunk_variants(edit_dir, manifest)
    persisted = json.loads((edit_dir / "chunks.json").read_text(encoding="utf-8"))

    assert manifest["status"] == "review"
    assert sorted(reasoned_a4_calls) == sorted(
        [
            "A4-chunk-plan",
            "A4-chunk-00-hook-a",
            "A4-chunk-00-hook-b",
            "A4-chunk-01-problem-a",
            "A4-chunk-01-problem-b",
        ]
    )
    assert manifest["generation_id"]
    assert persisted_before_a5 == manifest
    assert persisted == manifest
    assert len(manifest["chunks"]) == 2
    assert all(
        [variant["id"] for variant in chunk["variants"]] == ["a", "b"]
        for chunk in manifest["chunks"]
    )
    assert maximum >= 2
    assert len(graphics.call_args_list) == 2
    assert all(
        call.kwargs["approved_catalog"] == approved_catalog for call in graphics.call_args_list
    )
    assert all(call.kwargs["required_overlays"] == 1 for call in graphics.call_args_list)
    assert all(
        len(next(item for item in chunk["variants"] if item["id"] == "a")["overlays"])
        == 0
        for chunk in manifest["chunks"]
    )
    assert all(
        len(next(item for item in chunk["variants"] if item["id"] == "b")["overlays"])
        == 1
        for chunk in manifest["chunks"]
    )
    assert recommendations == {
        str(chunk["id"]): {
            "status": "ready",
            "variant_id": "b",
            "reason": "Die Motion-Fassung stützt den Beat.",
        }
        for chunk in manifest["chunks"]
    }
    assert all("recommendation" not in chunk for chunk in manifest["chunks"])
    assert all(
        variant["probe"]
        == {
            "duration_s": 4.0,
            "width": 1920,
            "height": 1080,
            "fps": 24.0,
            "has_audio": True,
        }
        for chunk in persisted["chunks"]
        for variant in chunk["variants"]
    )
    assert all(
        "path" not in variant["probe"]
        for chunk in persisted["chunks"]
        for variant in chunk["variants"]
    )
    assert (edit_dir / "chunks" / "chunk-00-hook" / "a" / "preview.mp4").exists()


def test_variant_supervisor_context_forwards_file_free_probe_measurements(
    tmp_path: Path,
) -> None:
    measurements = {
        "duration_s": 3.75,
        "width": 1080,
        "height": 1920,
        "fps": 29.97,
        "has_audio": True,
    }
    context = _variant_supervisor_context(
        {"id": "chunk-00-hook", "beat": "HOOK"},
        [
            {
                "id": "a",
                "duration_s": 3.75,
                "expected_duration_s": 4.0,
                "probe": measurements,
            },
            {
                "id": "b",
                "duration_s": 4.0,
                "expected_duration_s": 4.0,
            },
        ],
        {
            "run_number": 1,
            "_session_state_path": str(tmp_path / "sessions.json"),
        },
    )

    variants = context["chunk"]["variants"]
    assert variants[0]["probe"] == measurements
    assert variants[0]["probe"] is not measurements
    assert "path" not in variants[0]["probe"]
    assert variants[1]["probe"] == {}


def test_variant_supervisor_failure_keeps_manual_review_available(tmp_path: Path) -> None:
    variants = [{"id": "a"}, {"id": "b"}]
    base_context = {
        "run_number": 1,
        "_session_state_path": str(tmp_path / "sessions.json"),
    }
    with patch("palantum.orchestrator._cached_role", side_effect=TimeoutError("offline")):
        recommendation = _recommend_chunk_variant(
            tmp_path,
            {"id": "chunk-00-hook", "beat": "HOOK"},
            variants,
            base_context,
            resume=False,
        )

    assert recommendation == {
        "status": "unavailable",
        "variant_id": None,
        "reason": "Die KI-Empfehlung ist derzeit nicht verfügbar.",
    }


def test_finalize_chunk_variants_offsets_selected_chunk_overlays(tmp_path: Path) -> None:
    edit_dir = tmp_path / "edit"
    edit_dir.mkdir()
    source = tmp_path / "take.mp4"
    source.write_bytes(b"video")
    _state(edit_dir, source)
    graphic = tmp_path / "graphic.mov"
    graphic.write_bytes(b"graphic")
    ranges = [
        {
            "source": "take",
            "start": 0.0,
            "end": 2.0,
            "beat": "HOOK",
            "quote": "hook",
            "reason": "clear",
        },
        {
            "source": "take",
            "start": 2.0,
            "end": 5.0,
            "beat": "PROBLEM",
            "quote": "problem",
            "reason": "clear",
        },
    ]
    chunks = []
    for index, item in enumerate(ranges):
        chunks.append(
            {
                "id": f"chunk-{index}",
                "selected": "b",
                "variants": [
                    {
                        "id": "b",
                        "ranges": [item],
                        "overlays": [
                            {
                                "file": str(graphic),
                                "start_in_output": 0.5,
                                "duration": 1.0,
                            }
                        ],
                    }
                ],
            }
        )
    (edit_dir / "chunks.json").write_text(
        json.dumps(
            {
                "version": 1,
                "status": "review",
                "sources": {"take": str(source)},
                "chunks": chunks,
            }
        ),
        encoding="utf-8",
    )

    def fake_render(_edit_dir: Path, output: Path, preview: bool = False) -> Path:
        assert preview is False
        output.write_bytes(b"final")
        return output

    with (
        patch("palantum.orchestrator.render", side_effect=fake_render),
        patch("palantum.orchestrator.probe", side_effect=_probe),
        patch("palantum.orchestrator._audio_check", return_value={}),
        patch("palantum.orchestrator._timeline_views", return_value=[]),
        patch(
            "palantum.orchestrator.run_role",
            return_value={"findings": [], "verdict": "pass"},
        ),
    ):
        edl, qc = finalize_chunk_variants(edit_dir)

    assert [item["start_in_output"] for item in edl["overlays"]] == [0.5, 2.5]
    assert qc["verdict"] == "pass"
    assert json.loads((edit_dir / "chunks.json").read_text())["status"] == "done"
