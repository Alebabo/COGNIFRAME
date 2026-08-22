from __future__ import annotations

from unittest.mock import patch

from palantum.agents.runner import run_role


def test_devin_backend_is_selected_and_validated() -> None:
    valid = {"findings": [], "verdict": "pass"}
    with (
        patch.dict("os.environ", {"PALANTUM_AGENT_BACKEND": "devin"}),
        patch("palantum.agents.backends.devin.call", return_value=valid) as call,
    ):
        result = run_role("A7", "prompt", {}, None)
    assert result == valid
    call.assert_called_once()
