from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from palantum.web.app import create_app
from palantum.web.script import evaluate_canvas, parse_canvas_beats


def test_parse_canvas_beats_structured_and_unstructured() -> None:
    structured = (
        "01 HOOK: Zwei Stunden werden zu zwei Minuten.\n"
        "02 PROBLEM: Entwickler verlieren viel Zeit mit flakigen Tests.\n"
        "03 SOLUTION: Automatische Isolation der Ursache.\n"
        "04 DEMO: Ein Klick öffnet den Fix.\n"
        "05 TRACTION: 120 Teams in 3 Monaten.\n"
        "06 TEAM: 6 Jahre Google & Stripe.\n"
        "07 ASK: Teste die Beta auf palantum.dev."
    )
    parsed = parse_canvas_beats(structured)
    assert "Zwei Stunden" in parsed["HOOK"]
    assert "Entwickler verlieren" in parsed["PROBLEM"]
    assert "Automatische Isolation" in parsed["SOLUTION"]
    assert "Ein Klick" in parsed["DEMO"]
    assert "120 Teams" in parsed["TRACTION"]
    assert "Google & Stripe" in parsed["TEAM"]
    assert "palantum.dev" in parsed["ASK"]


def test_evaluate_canvas_generates_director_and_strategist_feedback() -> None:
    # 1. Missing beats
    beats_tuple = ("HOOK", "PROBLEM", "SOLUTION", "DEMO", "TRACTION", "TEAM", "ASK")
    empty_beats = {k: "" for k in beats_tuple}
    recs = evaluate_canvas(empty_beats, {})
    rec_beats = {r["beat"] for r in recs}
    assert "HOOK" in rec_beats
    assert "PROBLEM" in rec_beats
    assert "DEMO" in rec_beats

    # 2. Greeting in hook
    beats_with_greeting = {
        "HOOK": "Hallo, mein Name ist Alex und wir bauen die Zukunft des Testens.",
        "PROBLEM": "Entwickler verlieren Stunden durch kaputte Pipelines.",
        "SOLUTION": "Palantum löst das Problem.",
        "DEMO": "Hier sieht man die Oberfläche.",
        "TRACTION": "Wir wachsen sehr schnell und haben viele zufriedene Kunden.",  # no numbers!
        "TEAM": "Unser Team hat Erfahrung.",
        "ASK": "Besuche palantum.dev.",
    }
    recs = evaluate_canvas(beats_with_greeting, {"DEMO": "take_demo.mp4"})
    a3_recs = [r for r in recs if r["agent"] == "A3"]
    # A3 should warn about greeting in hook and missing numbers in traction
    reasons = [r["message"] for r in a3_recs]
    assert any("Begrüßung" in msg for msg in reasons)
    assert any("Zahlen" in msg for msg in reasons)


def test_canvas_get_and_post_endpoints(tmp_path: Path) -> None:
    client = TestClient(create_app(tmp_path))

    # GET initial state
    res = client.get("/api/canvas")
    assert res.status_code == 200
    data = res.json()
    assert data["title"] == "Mein Startup Pitch"
    assert "HOOK" in data["beats"]
    assert len(data["agent_cursors"]) == 3

    # POST updated canvas
    update_payload = {
        "title": "SuperPitch AI",
        "beats": {
            "HOOK": "Zwei Stunden Test-Triage werden zu zwei Minuten.",
            "PROBLEM": "Entwickler verlieren wertvolle Zeit.",
        },
        "attached_videos": {
            "HOOK": "take_1.mp4",
        },
    }
    res2 = client.post("/api/canvas", json=update_payload)
    assert res2.status_code == 200
    updated_data = res2.json()
    assert updated_data["title"] == "SuperPitch AI"
    assert "Zwei Stunden" in updated_data["beats"]["HOOK"]
    assert updated_data["attached_videos"]["HOOK"] == "take_1.mp4"
    assert len(updated_data["recommendations"]) > 0


def test_canvas_assist_endpoint_streams_response(tmp_path: Path) -> None:
    client = TestClient(create_app(tmp_path))
    with patch(
        "palantum.web.app.create_agent_assist_stream",
        return_value=(iter(["[A3]: ", "Präzisiere die Zahlen mit Zeitbezug."]), "local"),
    ):
        res = client.post(
            "/api/canvas/assist",
            json={
                "prompt": "Test Pitch",
                "beat_id": "TRACTION",
                "current_text": "Wir haben viele Kunden",
                "agent_id": "A3",
            },
        )
    assert res.status_code == 200
    assert "[A3]: Präzisiere die Zahlen" in res.text
    assert res.headers["x-palantum-agent"] == "A3"


def test_upload_with_beat_association(tmp_path: Path) -> None:
    client = TestClient(create_app(tmp_path))
    with patch("palantum.web.app._EXECUTOR.submit"):
        res = client.post(
            "/api/upload",
            files={"files": ("demo_clip.mp4", b"fake video content", "video/mp4")},
            data={"beat": "DEMO", "brief": "Mein Pitch"},
        )
    assert res.status_code == 200
    assert res.json()["beat"] == "DEMO"

    # Verify canvas recorded the attached video
    canvas_res = client.get("/api/canvas")
    assert canvas_res.status_code == 200
    assert canvas_res.json()["attached_videos"].get("DEMO") == "demo_clip.mp4"
