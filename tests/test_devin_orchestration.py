from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from pitchcraft.web.app import create_app
from pitchcraft.web.script import CanvasAgentUnavailableError


def test_devin_config_endpoint_is_read_only(tmp_path: Path) -> None:
    client = TestClient(create_app(tmp_path))

    # Initial config check
    res = client.get("/api/devin/config")
    assert res.status_code == 200
    data = res.json()
    assert "devin_configured" in data
    assert data["agent_backend"] == "devin"
    assert client.post("/api/devin/config", json={"devin_api_key": "secret"}).status_code == 405


def test_devin_orchestrate_endpoint_evaluates_beats_and_ghost_text(tmp_path: Path) -> None:
    client = TestClient(create_app(tmp_path))
    result = {
        "request_id": "orch-1",
        "session_id": "session-1",
        "session_url": "https://app.devin.ai/sessions/session-1",
        "agents": [
            {
                "agent": "A2",
                "beat": "HOOK",
                "anchor_text": "Hallo",
                "message": "Start with the value.",
                "ghost_text": "Two hours of editing become two minutes.",
            },
            {
                "agent": "A3",
                "beat": "TRACTION",
                "anchor_text": "Entwickler",
                "message": "Substantiate the value.",
                "ghost_text": "",
            },
            {
                "agent": "A1",
                "beat": "ASK",
                "anchor_text": "video editing",
                "message": "Add a CTA.",
                "ghost_text": "Test the beta now.",
            },
        ],
    }
    with patch("pitchcraft.web.app.orchestrate_canvas", return_value=result) as orchestrate:
        res = client.post(
            "/api/devin/orchestrate",
            json={
                "text": "Hello, we are building automated video editing for developers.",
                "cursor_offset": 15,
                "accepted_ghost_texts": ["Already used"],
                "request_id": "orch-1",
            },
        )
    assert res.status_code == 200
    data = res.json()
    assert data["request_id"] == "orch-1"
    assert [item["agent"] for item in data["agents"]] == ["A2", "A3", "A1"]
    orchestrate.assert_called_once_with(
        "Hello, we are building automated video editing for developers.",
        cursor_offset=15,
        accepted_ghost_texts=["Already used"],
        request_id="orch-1",
    )


def test_missing_devin_disables_agents_without_blocking_canvas(tmp_path: Path) -> None:
    client = TestClient(create_app(tmp_path))
    saved = client.post("/api/canvas", json={"text": "My pitch remains saved."})
    assert saved.status_code == 200

    with patch(
        "pitchcraft.web.app.orchestrate_canvas",
        side_effect=CanvasAgentUnavailableError("Devin is not configured."),
    ):
        response = client.post(
            "/api/devin/orchestrate",
            json={"text": "My pitch remains saved.", "request_id": "offline-1"},
        )

    assert response.status_code == 503
    assert client.get("/api/canvas").json()["text"] == "My pitch remains saved."


def test_invalid_orchestration_request_maps_to_422(tmp_path: Path) -> None:
    client = TestClient(create_app(tmp_path))
    with patch(
        "pitchcraft.web.app.orchestrate_canvas",
        side_effect=ValueError("request_id must not be blank"),
    ):
        response = client.post(
            "/api/devin/orchestrate",
            json={"text": "A valid pitch", "request_id": " "},
        )

    assert response.status_code == 422
    assert response.json()["detail"] == "request_id must not be blank"


def test_invalid_assist_agent_maps_to_422(tmp_path: Path) -> None:
    client = TestClient(create_app(tmp_path))
    with patch(
        "pitchcraft.web.app.assist_canvas_agent",
        side_effect=ValueError("unsupported canvas agent: A9"),
    ):
        response = client.post(
            "/api/canvas/assist",
            json={"text": "A valid pitch", "agent_id": "A9"},
        )

    assert response.status_code == 422
    assert response.json()["detail"] == "unsupported canvas agent: A9"


def test_config_ignores_whitespace_only_keys(tmp_path: Path) -> None:
    client = TestClient(create_app(tmp_path))
    with patch.dict(
        "os.environ",
        {"DEVIN_PAT": "  ", "DEVIN_API_KEY": "\t", "OPENAI_API_KEY": " "},
    ):
        response = client.get("/api/devin/config")

    assert response.status_code == 200
    assert response.json()["devin_configured"] is False
    assert response.json()["openai_configured"] is False
