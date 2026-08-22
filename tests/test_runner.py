from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

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


def test_openai_backend_must_be_selected_explicitly() -> None:
    valid = {"findings": [], "verdict": "pass"}
    with (
        patch.dict("os.environ", {"PALANTUM_AGENT_BACKEND": "openai"}),
        patch("palantum.agents.backends.openai.call", return_value=valid) as call,
    ):
        output = run_role("A7", "prompt", {}, None)
    assert output == valid
    call.assert_called_once()


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
