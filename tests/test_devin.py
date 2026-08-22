from __future__ import annotations

from typing import Any
from unittest.mock import Mock, patch
from unittest.mock import call as mock_call

import pytest

from palantum.agents.backends.devin import call


def _response(payload: dict[str, Any]) -> Mock:
    response = Mock()
    response.json.return_value = payload
    return response


def test_finished_session_returns_structured_output_and_creation_url() -> None:
    created = _response(
        {
            "session_id": "session-a1",
            "url": "https://app.devin.ai/sessions/session-a1",
        }
    )
    finished = _response(
        {
            "status_enum": "finished",
            "structured_output": {"takes": [], "beat_candidates": []},
        }
    )
    session_created = Mock()
    with (
        patch.dict(
            "os.environ",
            {
                "DEVIN_API_KEY": "key",
                "DEVIN_SNAPSHOT_ID": "snapshot-video-use",
                "PALANTUM_A1_MAX_ACU": "7",
                "PALANTUM_DEVIN_POLL_INTERVAL_S": "0",
            },
            clear=True,
        ),
        patch("palantum.agents.backends.devin.requests.post", return_value=created) as post,
        patch("palantum.agents.backends.devin.requests.get", return_value=finished),
        patch("palantum.agents.backends.devin.requests.delete") as delete,
    ):
        result = call(
            "A1",
            "measure takes",
            {"run_number": 3},
            {"type": "object"},
            on_session_created=session_created,
        )

    assert result.output == {"takes": [], "beat_candidates": []}
    assert result.session_id == "session-a1"
    assert result.url == "https://app.devin.ai/sessions/session-a1"
    session_created.assert_called_once_with("session-a1", result.url)
    delete.assert_not_called()
    payload = post.call_args.kwargs["json"]
    assert payload["snapshot_id"] == "snapshot-video-use"
    assert payload["title"] == "Palantum A1 · run 3"
    assert payload["tags"] == ["run-3", "A1"]
    assert payload["max_acu_limit"] == 7


def test_poll_interval_defaults_to_ten_seconds() -> None:
    created = _response({"session_id": "session-a3", "url": "https://app/session-a3"})
    working = _response({"status_enum": "working"})
    finished = _response({"status_enum": "finished", "structured_output": {}})
    with (
        patch.dict("os.environ", {"DEVIN_API_KEY": "key"}, clear=True),
        patch("palantum.agents.backends.devin.requests.post", return_value=created),
        patch(
            "palantum.agents.backends.devin.requests.get",
            side_effect=[working, finished],
        ),
        patch("palantum.agents.backends.devin.time.sleep") as sleep,
    ):
        call("A3", "challenge", {}, {"type": "object"})

    sleep.assert_called_once_with(10.0)


@pytest.mark.parametrize("status", ["blocked", "expired"])
def test_terminal_failure_status_is_reported_without_retry(status: str) -> None:
    created = _response({"session_id": "session-a2", "url": "https://app/session-a2"})
    terminal = _response({"status_enum": status})
    with (
        patch.dict(
            "os.environ",
            {"DEVIN_API_KEY": "key", "PALANTUM_DEVIN_POLL_INTERVAL_S": "0"},
            clear=True,
        ),
        patch("palantum.agents.backends.devin.requests.post", return_value=created) as post,
        patch("palantum.agents.backends.devin.requests.get", return_value=terminal),
        patch("palantum.agents.backends.devin.requests.delete") as delete,
        pytest.raises(RuntimeError, match=status),
    ):
        call("A2", "direct", {}, {"type": "object"})

    post.assert_called_once()
    delete.assert_not_called()


def test_timeout_deletes_each_session_and_retries_once() -> None:
    first = _response({"session_id": "first", "url": "https://app/first"})
    second = _response({"session_id": "second", "url": "https://app/second"})
    terminated = _response({"detail": "terminated"})
    with (
        patch.dict(
            "os.environ",
            {"DEVIN_API_KEY": "key", "PALANTUM_DEVIN_TIMEOUT_S": "1"},
            clear=True,
        ),
        patch(
            "palantum.agents.backends.devin.requests.post", side_effect=[first, second]
        ) as post,
        patch("palantum.agents.backends.devin.requests.get") as get,
        patch(
            "palantum.agents.backends.devin.requests.delete", return_value=terminated
        ) as delete,
        patch("palantum.agents.backends.devin.time.monotonic", side_effect=[0, 2, 3, 5]),
        pytest.raises(TimeoutError, match="exceeded 1s"),
    ):
        call("A4", "cut", {}, {"type": "object"})

    assert post.call_count == 2
    get.assert_not_called()
    assert delete.call_args_list == [
        mock_call(
            "https://api.devin.ai/v1/sessions/first",
            headers={"Authorization": "Bearer key", "Content-Type": "application/json"},
            timeout=30,
        ),
        mock_call(
            "https://api.devin.ai/v1/sessions/second",
            headers={"Authorization": "Bearer key", "Content-Type": "application/json"},
            timeout=30,
        ),
    ]
