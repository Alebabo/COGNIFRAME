import os
from pathlib import Path

from fastapi.testclient import TestClient

from palantum.web.app import create_app
from palantum.web.script import evaluate_canvas


def test_devin_config_endpoint_get_and_post(tmp_path: Path) -> None:
    client = TestClient(create_app(tmp_path))

    # Initial config check
    res = client.get("/api/devin/config")
    assert res.status_code == 200
    data = res.json()
    assert "devin_configured" in data
    assert "agent_backend" in data

    # Update config via POST
    res2 = client.post(
        "/api/devin/config",
        json={"devin_api_key": "test_devin_key_123", "agent_backend": "devin"},
    )
    assert res2.status_code == 200
    updated = res2.json()
    assert updated["devin_configured"] is True
    assert updated["agent_backend"] == "devin"
    assert os.environ.get("DEVIN_API_KEY") == "test_devin_key_123"

    # Cleanup env
    os.environ.pop("DEVIN_API_KEY", None)


def test_devin_orchestrate_endpoint_evaluates_beats_and_ghost_text(tmp_path: Path) -> None:
    client = TestClient(create_app(tmp_path))

    res = client.post(
        "/api/devin/orchestrate",
        json={
            "text": "Hallo, wir bauen automatisierten Videoschnitt für Entwickler.",
            "title": "Startup Pitch",
        },
    )
    assert res.status_code == 200
    data = res.json()
    assert "beats" in data
    assert "recommendations" in data
    assert "agent_cursors" in data
    assert len(data["agent_cursors"]) == 3

    # Check that recommendations contain missing items and concrete ghost text
    recs = data["recommendations"]
    assert any(r.get("missing_item") for r in recs)
    assert any(r.get("ghost_text") for r in recs)


def test_evaluate_canvas_missing_items_strict() -> None:
    beats = {
        "HOOK": "Zwei Stunden Fehlersuche werden zu zwei Minuten.",
        "PROBLEM": "Entwickler verlieren wertvolle Zeit durch fehlerhafte Builds.",
        "SOLUTION": "Unsere Software isoliert Fehler automatisch.",
        "DEMO": "Hier sieht man die Oberfläche.",
        "TRACTION": "Wir wachsen stark.",  # missing numbers
        "TEAM": "Erfahrenes Team.",
        "ASK": "",  # missing CTA
    }
    recs = evaluate_canvas(beats, {"DEMO": "clip.mp4"})
    missing_items = {r["missing_item"] for r in recs}
    assert "Harte Kennzahlen & Traction" in missing_items
    assert "Call to Action (CTA)" in missing_items
