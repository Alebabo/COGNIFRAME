from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from unittest.mock import patch

import pytest


def _load_script_module() -> ModuleType:
    path = Path(__file__).parents[1] / "pitchcraft" / "web" / "script.py"
    spec = importlib.util.spec_from_file_location("pitchcraft_canvas_script_tests", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


script_module = _load_script_module()
CanvasAgentResponseError = script_module.CanvasAgentResponseError
CanvasAgentUnavailableError = script_module.CanvasAgentUnavailableError
assist_canvas_agent = script_module.assist_canvas_agent
build_canvas_metadata = script_module.build_canvas_metadata
create_script_stream = script_module.create_script_stream
orchestrate_canvas = script_module.orchestrate_canvas
parse_canvas_beats = script_module.parse_canvas_beats


def test_parse_canvas_beats_supports_numbered_headers_and_aliases() -> None:
    parsed = parse_canvas_beats(
        "01 HOOK: Start directly.\n"
        "02 PROBLEM\nToo much manual work.\n"
        "03 SOLUTION — Automatic editing.\n"
        "04 DEMO: One click shows the result.\n"
        "05 TRACTION: 12 teams.\n"
        "06 TEAM: Two founders.\n"
        "07 CTA: Test today."
    )

    assert parsed == {
        "HOOK": "Start directly.",
        "PROBLEM": "Too much manual work.",
        "SOLUTION": "Automatic editing.",
        "DEMO": "One click shows the result.",
        "TRACTION": "12 teams.",
        "TEAM": "Two founders.",
        "ASK": "Test today.",
    }
    assert parse_canvas_beats("Free-form pitch text without a heading.")["HOOK"].startswith(
        "Free-form pitch text"
    )


def test_build_canvas_metadata_is_local_and_returns_isolated_defaults() -> None:
    with patch.object(script_module, "_call_devin") as call_devin:
        first = build_canvas_metadata(text="HOOK: A clear opening.")
        second = build_canvas_metadata()

    call_devin.assert_not_called()
    first["beats"]["HOOK"] = "changed"
    first["beat_info"]["HOOK"]["title"] = "changed"
    first["agent_cursors"][0]["beat"] = "ASK"

    assert second["beats"]["HOOK"] == ""
    assert second["title"] == "My Startup Pitch"
    assert second["beat_info"]["HOOK"]["title"] == "Hook"
    assert second["beat_info"]["SOLUTION"]["title"] == "Solution"
    assert second["agent_cursors"][0]["beat"] == "HOOK"


def test_canvas_improvements_and_generated_scripts_are_requested_in_english() -> None:
    assert "Always write message and ghost_text in English" in script_module._ORCHESTRATION_PROMPT
    assert "Always write message and ghost_text in English" in script_module._ASSIST_PROMPT
    assert "Write the complete script in English" in script_module._SCRIPT_PROMPT


def test_orchestrate_canvas_uses_one_devin_call_and_normalizes_ghosts() -> None:
    text = "We edit videos. Teams save time."
    output = {
        "agents": [
            {
                "agent": "A1",
                "beat": "ASK",
                "anchor_text": "time.",
                "message": "   Close   with specifics. ",
                "ghost_text": " start today. ",
            },
            {
                "agent": "A3",
                "beat": "TRACTION",
                "anchor_text": "Teams",
                "message": "Substantiate the time savings.",
                "ghost_text": " Already   used. ",
            },
            {
                "agent": "A2",
                "beat": "HOOK",
                "anchor_text": "We edit",
                "message": "Show the result first.",
                "ghost_text": "Edit with AI.",
            },
        ]
    }
    with (
        patch.dict("os.environ", {"DEVIN_API_KEY": "test-token"}, clear=True),
        patch.object(
            script_module,
            "_call_devin",
            return_value=(output, "session-1", "https://app.devin.ai/sessions/session-1"),
        ) as call_devin,
    ):
        result = orchestrate_canvas(
            text,
            cursor_offset=len(text),
            accepted_ghost_texts=[" Already used. ", "ALREADY   USED."],
            request_id="request-7",
        )

    call_devin.assert_called_once()
    role_id, _prompt, context, schema = call_devin.call_args.args
    assert role_id == "CANVAS"
    assert context["request_id"] == "request-7"
    assert context["cursor_offset"] == len(text)
    assert context["accepted_ghost_texts"] == ["Already used."]
    assert schema["properties"]["agents"]["minItems"] == 3
    assert result["request_id"] == "request-7"
    assert result["session_id"] == "session-1"
    assert [item["agent"] for item in result["agents"]] == ["A2", "A3", "A1"]
    assert result["agents"][0]["ghost_text"] == "with AI."
    assert result["agents"][1]["ghost_text"] == ""
    assert result["agents"][2]["ghost_text"] == "Start today."
    assert result["agents"][2]["message"] == "Close with specifics."


def test_orchestrate_canvas_rejects_non_verbatim_anchor() -> None:
    output = {
        "agents": [
            {
                "agent": agent,
                "beat": beat,
                "anchor_text": "not in the canvas",
                "message": "Note",
                "ghost_text": "",
            }
            for agent, beat in (("A2", "HOOK"), ("A3", "PROBLEM"), ("A1", "ASK"))
        ]
    }
    with (
        patch.dict("os.environ", {"DEVIN_PAT": "test-token"}, clear=True),
        patch.object(
            script_module,
            "_call_devin",
            return_value=(output, "session-1", "https://example.test/session-1"),
        ),
        pytest.raises(CanvasAgentResponseError, match="verbatim substring"),
    ):
        orchestrate_canvas("This text has valid content.")


def test_assist_canvas_agent_returns_only_the_json_contract() -> None:
    output = {
        "agent": "A3",
        "beat": "TRACTION",
        "anchor_text": "teams",
        "message": "Name a substantiated time period.",
        "ghost_text": "Teams save time every day",
    }
    with (
        patch.dict("os.environ", {"DEVIN_API_KEY": "test-token"}, clear=True),
        patch.object(
            script_module,
            "_call_devin",
            return_value=(output, "session-2", "https://example.test/session-2"),
        ),
    ):
        result = assist_canvas_agent(
            "a3",
            "Our platform helps teams",
            beat="traction",
            request_id="assist-1",
        )

    assert result == {
        "agent": "A3",
        "beat": "TRACTION",
        "anchor_text": "teams",
        "message": "Name a substantiated time period.",
        "ghost_text": "save time every day",
    }


def test_missing_or_unreachable_devin_raises_typed_unavailable_error() -> None:
    with (
        patch.dict("os.environ", {}, clear=True),
        pytest.raises(CanvasAgentUnavailableError, match="not configured"),
    ):
        orchestrate_canvas("A sufficiently long canvas text.")

    with (
        patch.dict("os.environ", {"DEVIN_API_KEY": "test-token"}, clear=True),
        patch.object(script_module, "_call_devin", side_effect=TimeoutError("too slow")),
        pytest.raises(CanvasAgentUnavailableError, match="too slow"),
    ):
        orchestrate_canvas("A sufficiently long canvas text.")


def test_create_script_stream_is_devin_only() -> None:
    with (
        patch.dict("os.environ", {"DEVIN_API_KEY": "test-token"}, clear=True),
        patch.object(
            script_module,
            "_call_devin",
            return_value=(
                {"script": "HOOK\nDirect opening.\n\nASK\nTest today."},
                "session-3",
                "https://example.test/session-3",
            ),
        ),
    ):
        chunks, source = create_script_stream("A video editing product")

    assert source == "devin"
    assert "Direct opening" in "".join(chunks)
