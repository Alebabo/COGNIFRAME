from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from unittest.mock import patch

from palantum.engine.videouse import ProbeResult
from palantum.orchestrator import build_chunk_variants, finalize_chunk_variants


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

    def cached_role(
        _edit_dir: Path,
        cache_key: str,
        _role_id: str,
        _prompt: str,
        context: dict[str, object],
        *,
        resume: bool,
    ) -> dict[str, object]:
        del resume
        if cache_key == "A4-chunk-plan":
            return {"ranges": seed, "total_duration_s": 4.0, "notes": "plan"}
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

    with (
        patch("palantum.orchestrator._cached_role", side_effect=cached_role),
        patch("palantum.orchestrator._motion_source", return_value=template),
        patch("palantum.orchestrator._graphics_overlays", return_value=[]) as graphics,
        patch("palantum.orchestrator.render", side_effect=fake_render),
        patch("palantum.orchestrator.probe", side_effect=_probe),
    ):
        manifest = build_chunk_variants(edit_dir, [source], template)

    assert manifest["status"] == "review"
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
    assert (edit_dir / "chunks" / "chunk-00-hook" / "a" / "preview.mp4").exists()


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
