from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from pitchcraft.web.app import create_app
from pitchcraft.web.script import parse_canvas_beats


def test_parse_canvas_beats_structured_and_unstructured() -> None:
    structured = (
        "01 HOOK: Two hours become two minutes.\n"
        "02 PROBLEM: Developers lose a lot of time to flaky tests.\n"
        "03 SOLUTION: Automatic isolation of the root cause.\n"
        "04 DEMO: One click opens the fix.\n"
        "05 TRACTION: 120 Teams in 3 months.\n"
        "06 TEAM: 6 years at Google & Stripe.\n"
        "07 ASK: Test the beta at pitchcraft.dev."
    )
    parsed = parse_canvas_beats(structured)
    assert "Two hours" in parsed["HOOK"]
    assert "Developers lose" in parsed["PROBLEM"]
    assert "Automatic isolation" in parsed["SOLUTION"]
    assert "One click" in parsed["DEMO"]
    assert "120 Teams" in parsed["TRACTION"]
    assert "Google & Stripe" in parsed["TEAM"]
    assert "pitchcraft.dev" in parsed["ASK"]


def test_canvas_get_and_post_endpoints(tmp_path: Path) -> None:
    client = TestClient(create_app(tmp_path))

    # GET initial state
    res = client.get("/api/canvas")
    assert res.status_code == 200
    data = res.json()
    assert data["title"] == "My Startup Pitch"
    assert "HOOK" in data["beats"]
    assert len(data["agent_cursors"]) == 3

    # POST updated canvas
    update_payload = {
        "title": "SuperPitch AI",
        "beats": {
            "HOOK": "Two hours of test triage become two minutes.",
            "PROBLEM": "Developers lose valuable time.",
        },
        "attached_videos": {
            "HOOK": "take_1.mp4",
        },
    }
    with patch("pitchcraft.web.app.orchestrate_canvas") as orchestrate:
        res2 = client.post("/api/canvas", json=update_payload)
    assert res2.status_code == 200
    updated_data = res2.json()
    assert updated_data["title"] == "SuperPitch AI"
    assert "Two hours" in updated_data["beats"]["HOOK"]
    assert updated_data["attached_videos"]["HOOK"] == "take_1.mp4"
    orchestrate.assert_not_called()
    assert not list((tmp_path / "edit").glob("canvas.json.tmp"))


def test_canvas_text_parsing_wins_over_stale_frontend_beats(tmp_path: Path) -> None:
    client = TestClient(create_app(tmp_path))

    response = client.post(
        "/api/canvas",
        json={
            "text": "01 HOOK: The newly written opening.\n07 ASK: Test it now.",
            "beats": {
                "HOOK": "",
                "PROBLEM": "veraltet",
                "SOLUTION": "",
                "DEMO": "",
                "TRACTION": "",
                "TEAM": "",
                "ASK": "",
            },
            "attached_videos": {},
        },
    )

    assert response.status_code == 200
    assert response.json()["beats"]["HOOK"] == "The newly written opening."
    assert response.json()["beats"]["ASK"] == "Test it now."
    assert response.json()["beats"]["PROBLEM"] == ""


def test_canvas_assist_endpoint_returns_structured_devin_response(tmp_path: Path) -> None:
    client = TestClient(create_app(tmp_path))
    with patch(
        "pitchcraft.web.app.assist_canvas_agent",
        return_value={
            "request_id": "assist-1",
            "agent": "A3",
            "beat": "TRACTION",
                "anchor_text": "many customers",
            "message": "Add a clear time reference to the numbers.",
            "ghost_text": " in the last three months",
        },
    ) as assist:
        res = client.post(
            "/api/canvas/assist",
            json={
                "text": "We have many customers",
                "beat": "TRACTION",
                "agent_id": "A3",
                "cursor_offset": 21,
                "accepted_ghost_texts": [],
                "request_id": "assist-1",
            },
        )
    assert res.status_code == 200
    assert res.json()["ghost_text"] == " in the last three months"
    assist.assert_called_once_with(
        "A3",
        "We have many customers",
        beat="TRACTION",
        cursor_offset=21,
        accepted_ghost_texts=[],
        request_id="assist-1",
    )


def test_upload_with_beat_association(tmp_path: Path) -> None:
    client = TestClient(create_app(tmp_path))
    with patch("pitchcraft.web.app._EXECUTOR.submit"):
        res = client.post(
            "/api/upload",
            files={"files": ("demo_clip.mp4", b"fake video content", "video/mp4")},
            data={"beat": "DEMO", "brief": "My pitch"},
        )
    assert res.status_code == 200
    assert res.json()["beat"] == "DEMO"

    # Verify canvas recorded the attached video
    canvas_res = client.get("/api/canvas")
    assert canvas_res.status_code == 200
    assert canvas_res.json()["attached_videos"].get("DEMO") == "demo_clip.mp4"


def test_corrupt_canvas_is_reported_without_overwriting_it(tmp_path: Path) -> None:
    edit_dir = tmp_path / "edit"
    edit_dir.mkdir()
    canvas_path = edit_dir / "canvas.json"
    canvas_path.write_text("{recoverable but invalid", encoding="utf-8")
    client = TestClient(create_app(tmp_path))

    read = client.get("/api/canvas")
    write = client.post("/api/canvas", json={"text": "Do not overwrite"})

    assert read.status_code == 500
    assert write.status_code == 500
    assert canvas_path.read_text(encoding="utf-8") == "{recoverable but invalid"
