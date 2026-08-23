from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

import pytest

from palantum.agents.backends.devin import DevinCallResult
from palantum.agents.runner import _record_session, run_role


def test_devin_backend_is_the_default_and_records_response_url(tmp_path: Path) -> None:
    valid = {"findings": [], "verdict": "pass"}
    url = "https://app.devin.ai/sessions/session-a7"
    result = DevinCallResult(output=valid, session_id="session-a7", url=url)
    state_path = tmp_path / "sessions.json"
    with (
        patch.dict("os.environ", {}, clear=True),
        patch("palantum.agents.backends.devin.call", return_value=result) as call,
    ):
        output = run_role("A7", "prompt", {"_session_state_path": str(state_path)}, None)
    assert output == valid
    call.assert_called_once()
    assert json.loads(state_path.read_text())["A7"] == {"status": "done", "url": url}


def test_non_devin_agent_backend_is_rejected() -> None:
    with (
        patch.dict("os.environ", {"PALANTUM_AGENT_BACKEND": "openai"}),
        pytest.raises(ValueError, match="require the Devin backend"),
    ):
        run_role("A7", "prompt", {}, None)


def test_a4_can_revise_once_in_the_same_devin_session(tmp_path: Path) -> None:
    initial = {
        "ranges": [
            {
                "source": "take",
                "start": 2.0,
                "end": 1.0,
                "beat": "HOOK",
                "quote": "hook",
                "reason": "first pass",
            }
        ],
        "total_duration_s": 0.0,
        "notes": "draft",
    }
    revised = {
        "ranges": [
            {
                "source": "take",
                "start": 1.0,
                "end": 2.0,
                "beat": "HOOK",
                "quote": "hook",
                "reason": "corrected",
            }
        ],
        "total_duration_s": 1.0,
        "notes": "revised",
    }
    url = "https://app.devin.ai/sessions/session-a4"
    state_path = tmp_path / "sessions.json"

    def validator(output: dict[str, object]) -> list[str]:
        ranges = output["ranges"]
        assert isinstance(ranges, list)
        item = ranges[0]
        assert isinstance(item, dict)
        return (
            ["range 0 has invalid interval"]
            if float(item["end"]) <= float(item["start"])
            else []
        )
    with (
        patch(
            "palantum.agents.backends.devin.call",
            return_value=DevinCallResult(initial, "session-a4", url),
        ) as call,
        patch(
            "palantum.agents.backends.devin.continue_session",
            return_value=DevinCallResult(revised, "session-a4", url),
        ) as revise,
    ):
        output = run_role(
            "A4",
            "prompt",
            {"_session_state_path": str(state_path)},
            semantic_validator=validator,
            max_feedback_rounds=1,
        )

    assert output == revised
    call.assert_called_once()
    assert revise.call_args.args[:3] == ("A4", "session-a4", url)
    assert "range 0 has invalid interval" in revise.call_args.args[3]
    assert json.loads(state_path.read_text())["A4"] == {
        "status": "done",
        "url": url,
        "reasoning_steps": 2,
        "validation_findings": ["range 0 has invalid interval"],
    }


def test_a4_stops_after_one_failed_revision(tmp_path: Path) -> None:
    invalid = {
        "ranges": [
            {
                "source": "take",
                "start": 2.0,
                "end": 1.0,
                "beat": "HOOK",
                "quote": "hook",
                "reason": "invalid",
            }
        ],
        "total_duration_s": 0.0,
        "notes": "still invalid",
    }
    result = DevinCallResult(invalid, "session-a4", "https://app/session-a4")
    with (
        patch("palantum.agents.backends.devin.call", return_value=result),
        patch("palantum.agents.backends.devin.continue_session", return_value=result) as revise,
        pytest.raises(ValueError, match="after one revision"),
    ):
        run_role(
            "A4",
            "prompt",
            {"_session_state_path": str(tmp_path / "sessions.json")},
            semantic_validator=lambda _output: ["invalid interval"],
            max_feedback_rounds=1,
        )

    revise.assert_called_once()


def test_session_state_updates_are_thread_safe_and_atomic(tmp_path: Path) -> None:
    path = tmp_path / "sessions.json"
    context = {"_session_state_path": str(path)}
    roles = [f"slot-{index:02d}" for index in range(24)]

    with ThreadPoolExecutor(max_workers=8) as executor:
        list(
            executor.map(
                lambda role: _record_session(
                    context, role, "running", f"https://app.devin.ai/sessions/{role}"
                ),
                roles,
            )
        )

    stored = json.loads(path.read_text())
    assert set(stored) == set(roles)
    assert not list(tmp_path.glob(".sessions.json.*.tmp"))


def test_parallel_role_instances_get_distinct_session_keys(tmp_path: Path) -> None:
    path = tmp_path / "sessions.json"
    _record_session(
        {"_session_state_path": str(path), "_session_key": "A6.problem-01"},
        "A6W",
        "done",
        "https://app.devin.ai/sessions/problem-01",
    )

    assert json.loads(path.read_text())["A6.problem-01"] == {
        "role": "A6W",
        "status": "done",
        "url": "https://app.devin.ai/sessions/problem-01",
    }
