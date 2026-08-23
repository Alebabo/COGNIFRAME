from __future__ import annotations

from typing import Any
from unittest.mock import Mock, patch
from unittest.mock import call as mock_call

import pytest
import requests

from palantum.agents.backends.devin import call


def _response(
    payload: dict[str, Any], *, status_code: int = 200, text: str = ""
) -> Mock:
    response = Mock()
    response.json.return_value = payload
    response.status_code = status_code
    response.text = text
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
        patch(
            "palantum.agents.backends.devin.requests.get", return_value=finished
        ) as get,
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
    post.assert_called_once_with(
        "https://api.devin.ai/v1/sessions",
        headers={"Authorization": "Bearer key", "Content-Type": "application/json"},
        json=payload,
        timeout=60,
    )
    get.assert_called_once_with(
        "https://api.devin.ai/v1/sessions/session-a1",
        headers={"Authorization": "Bearer key", "Content-Type": "application/json"},
        timeout=30,
    )


def test_cog_pat_discovers_org_and_uses_v3_with_fallback_app_url() -> None:
    self_response = _response({"org_id": "org-video"})
    created = _response({"session_id": "session-v3"})
    finished = _response(
        {
            "status": "running",
            "status_detail": "finished",
            "structured_output": {"beats": ["hook"]},
        }
    )
    session_created = Mock()
    headers = {
        "Authorization": "Bearer cog_test-pat",
        "Content-Type": "application/json",
    }
    with (
        patch.dict(
            "os.environ",
            {
                "DEVIN_PAT": "cog_test-pat",
                "DEVIN_API_KEY": "legacy-key",
                "DEVIN_SNAPSHOT_ID": "v1-only-snapshot",
            },
            clear=True,
        ),
        patch(
            "palantum.agents.backends.devin.requests.get",
            side_effect=[self_response, finished],
        ) as get,
        patch("palantum.agents.backends.devin.requests.post", return_value=created) as post,
    ):
        result = call(
            "A2",
            "direct",
            {"run_number": 8},
            {"type": "object"},
            on_session_created=session_created,
        )

    session_base_url = "https://api.devin.ai/v3/organizations/org-video/sessions"
    assert result.output == {"beats": ["hook"]}
    assert result.url == "https://app.devin.ai/sessions/session-v3"
    session_created.assert_called_once_with("session-v3", result.url)
    assert get.call_args_list == [
        mock_call("https://api.devin.ai/v3/self", headers=headers, timeout=10),
        mock_call(f"{session_base_url}/session-v3", headers=headers, timeout=30),
    ]
    assert post.call_args.args == (session_base_url,)
    assert post.call_args.kwargs["headers"] == headers
    payload = post.call_args.kwargs["json"]
    assert payload["structured_output_required"] is True
    assert "snapshot_id" not in payload


@pytest.mark.parametrize("legacy_key", ["apk_legacy-fallback", "apk_user_fallback"])
def test_failed_cog_org_discovery_uses_separate_legacy_key_for_v1(
    legacy_key: str,
) -> None:
    self_response = _response({}, status_code=503)
    created = _response({"session_id": "session-v1"})
    finished = _response({"status": "finished", "structured_output": {}})
    with (
        patch.dict(
            "os.environ",
            {
                "DEVIN_PAT": "cog_fallback-pat",
                "DEVIN_API_KEY": legacy_key,
                "DEVIN_API_URL": "https://devin-proxy.example/v1/",
            },
            clear=True,
        ),
        patch(
            "palantum.agents.backends.devin.requests.get",
            side_effect=[self_response, finished],
        ) as get,
        patch("palantum.agents.backends.devin.requests.post", return_value=created) as post,
    ):
        result = call("A3", "challenge", {}, {"type": "object"})

    assert result.url == "https://app.devin.ai/sessions/session-v1"
    assert post.call_args.args == ("https://devin-proxy.example/v1/sessions",)
    assert post.call_args.kwargs["headers"] == {
        "Authorization": f"Bearer {legacy_key}",
        "Content-Type": "application/json",
    }
    assert "structured_output_required" not in post.call_args.kwargs["json"]
    assert get.call_args_list[1].args == (
        "https://devin-proxy.example/v1/sessions/session-v1",
    )


@pytest.mark.parametrize(
    "environment",
    [
        {"DEVIN_PAT": "cog_no-legacy-key"},
        {
            "DEVIN_PAT": "cog_nonlegacy-fallback",
            "DEVIN_API_KEY": "not-a-legacy-key",
        },
        {"DEVIN_API_KEY": "cog_v3-key-in-api-slot"},
    ],
)
def test_failed_v3_org_discovery_never_sends_v3_token_to_v1(
    environment: dict[str, str],
) -> None:
    self_response = _response({}, status_code=503)
    with (
        patch.dict("os.environ", environment, clear=True),
        patch(
            "palantum.agents.backends.devin.requests.get",
            return_value=self_response,
        ) as get,
        patch("palantum.agents.backends.devin.requests.post") as post,
        pytest.raises(RuntimeError, match="Could not resolve a Devin v3 organization"),
    ):
        call("A3", "challenge", {}, {"type": "object"})

    get.assert_called_once()
    post.assert_not_called()


def test_v3_exit_with_structured_output_is_complete() -> None:
    created = _response({"session_id": "session-v3-exit"})
    exited = _response(
        {
            "status": "exit",
            "status_detail": "user_requested",
            "structured_output": {"must_not_be_accepted": True},
        }
    )
    with (
        patch.dict(
            "os.environ",
            {"DEVIN_PAT": "cog_exit", "DEVIN_ORG_ID": "org-video"},
            clear=True,
        ),
        patch("palantum.agents.backends.devin.requests.post", return_value=created),
        patch("palantum.agents.backends.devin.requests.get", return_value=exited),
        patch("palantum.agents.backends.devin.requests.delete") as delete,
    ):
        result = call("A2", "direct", {}, {"type": "object"})

    assert result.output == {"must_not_be_accepted": True}
    delete.assert_not_called()


def test_v3_exit_without_structured_output_is_terminal() -> None:
    created = _response({"session_id": "session-v3-exit"})
    exited = _response({"status": "exit", "status_detail": "user_requested"})
    with (
        patch.dict(
            "os.environ",
            {"DEVIN_PAT": "cog_exit", "DEVIN_ORG_ID": "org-video"},
            clear=True,
        ),
        patch("palantum.agents.backends.devin.requests.post", return_value=created),
        patch("palantum.agents.backends.devin.requests.get", return_value=exited),
        pytest.raises(RuntimeError, match=r"ended exit \(user_requested\)"),
    ):
        call("A2", "direct", {}, {"type": "object"})


@pytest.mark.parametrize("status", ["error", "suspended"])
def test_v3_error_and_suspended_are_terminal_and_redacted(status: str) -> None:
    token = f"cog_secret-{status}"
    created = _response({"session_id": f"session-v3-{status}"})
    terminal = _response(
        {
            "status": status,
            "status_detail": f"credential {token}",
        }
    )
    with (
        patch.dict(
            "os.environ",
            {"DEVIN_PAT": token, "DEVIN_ORG_ID": "org-video"},
            clear=True,
        ),
        patch("palantum.agents.backends.devin.requests.post", return_value=created),
        patch("palantum.agents.backends.devin.requests.get", return_value=terminal),
        patch("palantum.agents.backends.devin.requests.delete") as delete,
        pytest.raises(RuntimeError, match=f"ended {status}") as exc_info,
    ):
        call("A2", "direct", {}, {"type": "object"})

    assert token not in str(exc_info.value)
    assert "[redacted]" in str(exc_info.value)
    delete.assert_not_called()


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


def test_blocked_session_with_structured_output_is_complete() -> None:
    created = _response({"session_id": "session-a1", "url": "https://app/session-a1"})
    blocked = _response(
        {
            "status": "running",
            "status_enum": "blocked",
            "structured_output": {"takes": [], "beat_candidates": []},
        }
    )
    with (
        patch.dict("os.environ", {"DEVIN_API_KEY": "key"}, clear=True),
        patch("palantum.agents.backends.devin.requests.post", return_value=created),
        patch("palantum.agents.backends.devin.requests.get", return_value=blocked),
    ):
        result = call("A1", "measure", {}, {"type": "object"})

    assert result.output == {"takes": [], "beat_candidates": []}


@pytest.mark.parametrize(
    ("token", "environment", "expected_url"),
    [
        (
            "DEVIN-SECRET-v1",
            {"DEVIN_API_KEY": "DEVIN-SECRET-v1"},
            "https://api.devin.ai/v1/sessions",
        ),
        (
            "cog_DEVIN-SECRET-v3",
            {"DEVIN_PAT": "cog_DEVIN-SECRET-v3", "DEVIN_ORG_ID": "org-video"},
            "https://api.devin.ai/v3/organizations/org-video/sessions",
        ),
    ],
)
def test_session_creation_error_includes_devin_response_without_exposing_token(
    token: str, environment: dict[str, str], expected_url: str
) -> None:
    rejected = _response(
        {"detail": "prompt is too large"},
        status_code=400,
        text=f'{{"detail":"{token}: prompt is too large"}}',
    )
    rejected.raise_for_status.side_effect = requests.HTTPError("bad request")
    session_created = Mock()
    with (
        patch.dict("os.environ", environment, clear=True),
        patch(
            "palantum.agents.backends.devin.requests.post", return_value=rejected
        ) as post,
        pytest.raises(RuntimeError, match="prompt is too large") as exc_info,
    ):
        call(
            "A2",
            "direct",
            {},
            {"type": "object"},
            on_session_created=session_created,
        )

    assert "(400)" in str(exc_info.value)
    assert "[redacted]" in str(exc_info.value)
    assert token not in str(exc_info.value)
    session_created.assert_not_called()
    assert post.call_args.args == (expected_url,)


@pytest.mark.parametrize("status", ["blocked", "expired", "stopped"])
def test_terminal_failure_status_is_reported_without_retry(status: str) -> None:
    token = "DEVIN-SECRET-456"
    created = _response({"session_id": "session-a2", "url": "https://app/session-a2"})
    terminal_payload: dict[str, Any] = {
        "status_enum": status,
        "detail": f"credential {token}",
    }
    if status != "blocked":
        terminal_payload["structured_output"] = {"must_not_be_accepted": True}
    terminal = _response(terminal_payload)
    with (
        patch.dict(
            "os.environ",
            {
                "DEVIN_API_KEY": token,
                "PALANTUM_DEVIN_POLL_INTERVAL_S": "0",
            },
            clear=True,
        ),
        patch("palantum.agents.backends.devin.requests.post", return_value=created) as post,
        patch("palantum.agents.backends.devin.requests.get", return_value=terminal),
        patch("palantum.agents.backends.devin.requests.delete") as delete,
        pytest.raises(RuntimeError, match=status) as exc_info,
    ):
        call("A2", "direct", {}, {"type": "object"})

    post.assert_called_once()
    delete.assert_not_called()
    assert token not in str(exc_info.value)


def test_finished_session_without_structured_output_is_rejected() -> None:
    created = _response({"session_id": "session-a3"})
    finished = _response({"status_enum": "finished", "structured_output": "not-json"})
    with (
        patch.dict("os.environ", {"DEVIN_API_KEY": "key"}, clear=True),
        patch("palantum.agents.backends.devin.requests.post", return_value=created),
        patch("palantum.agents.backends.devin.requests.get", return_value=finished),
        pytest.raises(ValueError, match="returned no structured output"),
    ):
        call("A3", "challenge", {}, {"type": "object"})


def test_missing_or_blank_token_is_rejected_before_network_access() -> None:
    with (
        patch.dict(
            "os.environ", {"DEVIN_PAT": "  ", "DEVIN_API_KEY": ""}, clear=True
        ),
        patch("palantum.agents.backends.devin.requests.post") as post,
        pytest.raises(ValueError, match="Missing DEVIN_PAT or DEVIN_API_KEY"),
    ):
        call("A1", "measure", {}, {"type": "object"})

    post.assert_not_called()


def test_timeout_deletes_each_session_and_retries_once() -> None:
    first = _response({"session_id": "first", "url": "https://app/first"})
    second = _response({"session_id": "second", "url": "https://app/second"})
    terminated = _response({"detail": "terminated"})
    session_created = Mock()
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
        call(
            "A4",
            "cut",
            {},
            {"type": "object"},
            on_session_created=session_created,
        )

    assert post.call_count == 2
    get.assert_not_called()
    assert session_created.call_args_list == [
        mock_call("first", "https://app/first"),
        mock_call("second", "https://app/second"),
    ]
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
