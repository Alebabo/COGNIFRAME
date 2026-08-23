from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pytest

from palantum.orchestrator import (
    _cached_role,
    _graphics_overlays,
    _npm_install_command,
    _selectable_scenes,
    _single_motion_scenes,
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


def test_graphics_director_runs_parallel_slot_workers_and_returns_edl_entries(
    tmp_path: Path,
) -> None:
    source = tmp_path / "templates.zip"
    source.write_bytes(b"fixture")
    scene = _scene("hero-stat-callout", "PROBLEM")
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
    plan = {
        "overlays": [
            {
                "slot_id": "problem-01",
                "template_id": "hero-stat-callout",
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
            "template_id": "hero-stat-callout",
            "props": {"title": "Pain"},
            "checks": ["identity", "limits", "brand", "timing"],
        }

    rendered = {
        "file": "animations/slot_problem-01/render.mov",
        "start_in_output": 0.5,
        "duration": 2.0,
        "slot_id": "problem-01",
        "template_id": "hero-stat-callout",
        "beat": "PROBLEM",
    }
    with (
        patch("palantum.orchestrator.build_scene_catalog") as build_catalog,
        patch("palantum.orchestrator.run_role", side_effect=run) as roles,
        patch("palantum.orchestrator._render_motion_job", return_value=rendered) as render,
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

    with patch("palantum.orchestrator.run_role") as roles:
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
    with patch("palantum.orchestrator.run_role", return_value={"ranges": []}) as run:
        assert _cached_role(tmp_path, "A4", "A4", "prompt", {}, resume=False) == {"ranges": []}
    run.assert_called_once()

    with patch("palantum.orchestrator.run_role") as run:
        assert _cached_role(tmp_path, "A4", "A4", "prompt", {}, resume=True) == {"ranges": []}
    run.assert_not_called()
