from __future__ import annotations

import os
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from pitchcraft.orchestrator import (
    _alpha_coverage,
    _cached_role,
    _graphics_overlays,
    _merge_motion_qc,
    _npm_install_command,
    _render_motion_job,
    _selectable_scenes,
    _single_motion_scenes,
    _validate_motion_props,
    _validate_overlay_plan,
)


def _scene(scene_id: str, beat: str) -> dict[str, object]:
    return {
        "id": scene_id,
        "type": beat,
        "status": "ok",
        "confidence": 0.9,
        "duration_s": 3.0,
        "slots": [{"key": "title", "max_chars": 20, "default": "Default"}],
    }


def test_overlay_plan_enforces_catalog_slots_timing_and_non_overlap() -> None:
    scenes = [_scene("hero-stat-callout", "PROBLEM")]
    timeline = [
        {
            "beat": "PROBLEM",
            "start_in_output": 2.0,
            "end_in_output": 6.0,
            "duration": 4.0,
            "quote": "Pain",
        }
    ]
    overlay = {
        "slot_id": "problem-01",
        "template_id": "hero-stat-callout",
        "beat": "PROBLEM",
        "start_in_output": 2.5,
        "duration": 2.0,
        "props": {"title": "Pain costs millions"},
        "reason": "Quantifies the pain",
    }

    assert _validate_overlay_plan([overlay], scenes, timeline)[0]["scene"] == scenes[0]

    overlong = dict(overlay, duration=10.0)
    assert _validate_overlay_plan([overlong], scenes, timeline)[0]["duration"] == 3.0

    stale_timing = dict(overlay, start_in_output=20.0)
    fitted = _validate_overlay_plan([stale_timing], scenes, timeline)[0]
    assert fitted["start_in_output"] == 2.0

    invalid = dict(overlay, props={"title": "x" * 21})
    with pytest.raises(ValueError, match="max_chars"):
        _validate_overlay_plan([invalid], scenes, timeline)

    overlapping = dict(overlay, slot_id="problem-02", start_in_output=4.0)
    with pytest.raises(ValueError, match="overlap"):
        _validate_overlay_plan([overlay, overlapping], scenes, timeline)


def test_structured_motion_gets_longer_reading_time_inside_its_beat() -> None:
    scene = _scene("flowchart", "SOLUTION") | {
        "content_kind": "structured",
        "min_visible_s": 4.5,
    }
    overlay = {
        "slot_id": "solution-01",
        "template_id": "flowchart",
        "beat": "SOLUTION",
        "start_in_output": 5.0,
        "duration": 2.0,
        "props": {"title": "Flow"},
        "reason": "Explain the process",
    }
    timeline = [
        {
            "beat": "SOLUTION",
            "start_in_output": 2.0,
            "end_in_output": 8.0,
            "duration": 6.0,
            "quote": "Process",
        }
    ]

    result = _validate_overlay_plan([overlay], [scene], timeline)[0]

    assert result["start_in_output"] == 3.5
    assert result["duration"] == 4.5


def test_hero_props_require_numeric_value_and_readable_contrast() -> None:
    scene = {
        "id": "hero-stat-callout",
        "fixed_text_color": "#171717",
        "slots": [
            {"key": "heroValue", "default": "17%"},
            {"key": "bgColor", "default": "#FDD835"},
        ],
    }

    checked = _validate_motion_props(
        scene, {"heroValue": "$4.2M", "bgColor": "#FDD835"}
    )
    assert checked["prop_checks"] == "pass"
    assert checked["contrast_ratio"] > 4.5
    with pytest.raises(ValueError, match="parseable number"):
        _validate_motion_props(scene, {"heroValue": "?", "bgColor": "#FDD835"})
    with pytest.raises(ValueError, match="contrast"):
        _validate_motion_props(scene, {"heroValue": "17%", "bgColor": "#0B0B0B"})


def test_alpha_coverage_samples_three_points(tmp_path: Path) -> None:
    output = tmp_path / "overlay.mov"
    output.write_bytes(b"mov")
    completed = type("Completed", (), {"stdout": "lavfi.signalstats.YAVG=63.75", "stderr": ""})()
    with patch("pitchcraft.orchestrator.subprocess.run", return_value=completed) as run:
        assert _alpha_coverage(output, 4.0) == pytest.approx(0.25)

    assert run.call_count == 3


def test_opaque_unknown_scene_is_rerendered_as_inset(tmp_path: Path) -> None:
    source = tmp_path / "templates.zip"
    source.write_bytes(b"pack")
    slot = tmp_path / "edit/animations/slot_problem-01"
    slot.mkdir(parents=True)
    (slot / "render.mov").write_bytes(b"mov")
    overlay = {
        "slot_id": "problem-01",
        "template_id": "bar-chart-reveal",
        "beat": "PROBLEM",
        "start_in_output": 0.0,
        "duration": 2.0,
        "scene": {
            "id": "bar-chart-reveal",
            "duration_s": 3.0,
            "presentation": "overlay",
            "slots": [],
        },
    }
    with (
        patch("pitchcraft.orchestrator.materialize_scene", return_value=slot) as materialize,
        patch("pitchcraft.orchestrator._alpha_coverage", side_effect=[1.0, 0.2]),
        patch("pitchcraft.orchestrator.subprocess.run"),
    ):
        result = _render_motion_job(
            tmp_path / "edit", source, overlay, {}, (1920, 1080), resume=False
        )

    assert materialize.call_count == 2
    assert materialize.call_args_list[1].kwargs["presentation"] == "inset"
    assert all(
        call.kwargs["render_duration_s"] == 2.0
        for call in materialize.call_args_list
    )
    assert result["visual_qc"] == {
        "presentation": "inset",
        "max_alpha_coverage": 0.2,
        "contrast_ratio": None,
        "prop_checks": "pass",
        "verdict": "pass",
    }


def test_local_motion_qc_can_veto_a7_pass() -> None:
    report = _merge_motion_qc(
        {"findings": [], "verdict": "pass"},
        [
            {
                "start_in_output": 1.0,
                "visual_qc": {
                    "presentation": "overlay",
                    "max_alpha_coverage": 1.0,
                    "contrast_ratio": 1.1,
                    "verdict": "fail",
                },
            }
        ],
    )

    assert report["verdict"] == "fail"
    assert report["findings"][-1]["verdict"] == "fail"


def test_graphics_director_runs_parallel_slot_workers_and_returns_edl_entries(
    tmp_path: Path,
) -> None:
    source = tmp_path / "templates.zip"
    source.write_bytes(b"fixture")
    scene = _scene("bar-chart-reveal", "PROBLEM")
    catalog = {"source": {"sha256": "abc"}, "scenes": [scene]}
    ranges = [
        {
            "source": "take",
            "start": 1.0,
            "end": 5.0,
            "beat": "PROBLEM",
            "quote": "Pain",
            "reason": "best take",
        }
    ]
    plan: dict[str, object] = {
        "overlays": [
            {
                "slot_id": "problem-01",
                "template_id": "bar-chart-reveal",
                "beat": "PROBLEM",
                "start_in_output": 0.5,
                "duration": 2.0,
                "props": {"title": "Pain"},
                "reason": "show pain",
            }
        ]
    }

    def run(role: str, *_args: object, **_kwargs: object) -> dict[str, object]:
        if role == "A6":
            return plan
        return {
            "slot_id": "problem-01",
            "template_id": "bar-chart-reveal",
            "props": {"title": "Pain"},
            "checks": ["identity", "limits", "brand", "timing"],
        }

    rendered = {
        "file": "animations/slot_problem-01/render.mov",
        "start_in_output": 0.5,
        "duration": 2.0,
        "slot_id": "problem-01",
        "template_id": "bar-chart-reveal",
        "beat": "PROBLEM",
    }
    with (
        patch("pitchcraft.orchestrator.build_scene_catalog") as build_catalog,
        patch("pitchcraft.orchestrator.run_role", side_effect=run) as roles,
        patch("pitchcraft.orchestrator._render_motion_job", return_value=rendered) as render,
    ):
        result = _graphics_overlays(
            tmp_path / "edit",
            {"brand": {}},
            ranges,
            source,
            (1920, 1080),
            approved_catalog=catalog,
            required_overlays=1,
        )

    assert result == [rendered]
    build_catalog.assert_not_called()
    assert [call.args[0] for call in roles.call_args_list] == ["A6", "A6W"]
    render.assert_called_once()


def test_graphics_overlays_skip_motion_when_no_scene_is_approved(tmp_path: Path) -> None:
    source = tmp_path / "templates.zip"
    source.write_bytes(b"fixture")

    with patch("pitchcraft.orchestrator.run_role") as roles:
        result = _graphics_overlays(
            tmp_path / "edit",
            {"brand": {}},
            [],
            source,
            (1920, 1080),
            approved_catalog={"source": {"sha256": "abc"}, "scenes": []},
        )

    assert result == []
    roles.assert_not_called()


def test_single_motion_scene_adapts_generic_scene_for_team() -> None:
    generic = _scene("brand-statement", "unclassified") | {
        "confidence": 0.5,
        "static_parse": {"status": "ok"},
    }

    scenes = _single_motion_scenes({"scenes": [generic]}, "TEAM")

    assert len(scenes) == 1
    assert scenes[0]["id"] == "brand-statement"
    assert scenes[0]["type"] == "TEAM"
    assert scenes[0]["motion_variant_fallback"] is True
    assert scenes[0]["adapted_from_type"] == "unclassified"


def test_required_motion_fails_when_pack_has_no_usable_scene(tmp_path: Path) -> None:
    source = tmp_path / "templates.zip"
    source.write_bytes(b"fixture")
    ranges = [
        {
            "source": "take",
            "start": 0.0,
            "end": 2.0,
            "beat": "TEAM",
            "quote": "team",
        }
    ]

    with pytest.raises(RuntimeError, match="no usable scene"):
        _graphics_overlays(
            tmp_path / "edit",
            {"brand": {}},
            ranges,
            source,
            (1920, 1080),
            approved_catalog={"source": {"sha256": "abc"}, "scenes": []},
            required_overlays=1,
        )


def test_numeric_scene_is_rejected_for_non_numeric_quote(tmp_path: Path) -> None:
    source = tmp_path / "templates.zip"
    source.write_bytes(b"fixture")
    scene = _scene("hero-stat-callout", "PROBLEM") | {
        "requires_numeric_claim": True,
    }

    with (
        patch("pitchcraft.orchestrator.run_role") as roles,
        pytest.raises(RuntimeError, match="no usable scene"),
    ):
        _graphics_overlays(
            tmp_path / "edit",
            {"brand": {}},
            [
                {
                    "source": "take",
                    "start": 0.0,
                    "end": 2.0,
                    "beat": "PROBLEM",
                    "quote": "The workflow is painfully slow",
                }
            ],
            source,
            (1920, 1080),
            approved_catalog={"source": {"sha256": "abc"}, "scenes": [scene]},
            required_overlays=1,
        )

    roles.assert_not_called()


def test_invalid_worker_props_are_retried_once(tmp_path: Path) -> None:
    source = tmp_path / "templates.zip"
    source.write_bytes(b"fixture")
    scene = _scene("hero-stat-callout", "PROBLEM") | {
        "requires_numeric_claim": True,
        "fixed_text_color": "#171717",
        "slots": [
            {"key": "heroValue", "max_chars": 12, "default": "17%"},
            {"key": "bgColor", "max_chars": 32, "default": "#FDD835"},
        ],
    }
    plan: dict[str, object] = {
        "overlays": [
            {
                "slot_id": "problem-01",
                "template_id": "hero-stat-callout",
                "beat": "PROBLEM",
                "start_in_output": 0.0,
                "duration": 2.0,
                "props": {"heroValue": "?", "bgColor": "#0B0B0B"},
                "reason": "show the measured pain",
            }
        ]
    }
    worker_results: Any = iter(
        [
            {
                "slot_id": "problem-01",
                "template_id": "hero-stat-callout",
                "props": {"heroValue": "?", "bgColor": "#0B0B0B"},
                "checks": [],
            },
            {
                "slot_id": "problem-01",
                "template_id": "hero-stat-callout",
                "props": {"heroValue": "42%", "bgColor": "#FDD835"},
                "checks": [],
            },
        ]
    )

    def run(role: str, *_args: object, **_kwargs: object) -> dict[str, object]:
        return plan if role == "A6" else next(worker_results)

    rendered = {
        "file": "animations/slot_problem-01/render.mov",
        "start_in_output": 0.0,
        "duration": 2.0,
        "slot_id": "problem-01",
        "template_id": "hero-stat-callout",
        "beat": "PROBLEM",
    }
    with (
        patch("pitchcraft.orchestrator.run_role", side_effect=run) as roles,
        patch("pitchcraft.orchestrator._render_motion_job", return_value=rendered) as render,
    ):
        result = _graphics_overlays(
            tmp_path / "edit",
            {"brand": {}},
            [
                {
                    "source": "take",
                    "start": 0.0,
                    "end": 2.0,
                    "beat": "PROBLEM",
                    "quote": "42 percent of every day is lost",
                }
            ],
            source,
            (1920, 1080),
            approved_catalog={"source": {"sha256": "abc"}, "scenes": [scene]},
            required_overlays=1,
        )

    assert result == [rendered]
    assert [call.args[0] for call in roles.call_args_list] == ["A6", "A6W", "A6W"]
    assert "parseable number" in roles.call_args_list[-1].args[2]["_validation_error"]
    assert render.call_args.args[3]["heroValue"] == "42%"


def test_only_a0_approved_scenes_are_selectable() -> None:
    approved = _scene("hero-stat-callout", "PROBLEM")
    low_confidence = _scene("brand-statement", "ASK") | {"confidence": 0.6}
    broken = _scene("flowchart", "SOLUTION") | {"status": "broken"}

    assert _selectable_scenes({"scenes": [approved, low_confidence, broken]}) == [approved]


def test_npm_install_uses_windows_command_launcher() -> None:
    command = _npm_install_command()
    if os.name == "nt":
        assert command[1:5] == ["/d", "/s", "/c", "npm"]
    else:
        assert command[0] == "npm"


def test_role_cache_resumes_without_calling_backend(tmp_path: Path) -> None:
    with patch("pitchcraft.orchestrator.run_role", return_value={"ranges": []}) as run:
        assert _cached_role(tmp_path, "A4", "A4", "prompt", {}, resume=False) == {"ranges": []}
    run.assert_called_once()

    with patch("pitchcraft.orchestrator.run_role") as run:
        assert _cached_role(tmp_path, "A4", "A4", "prompt", {}, resume=True) == {"ranges": []}
    run.assert_not_called()
