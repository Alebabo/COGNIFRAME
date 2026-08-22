from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from palantum.web.app import _process_upload, create_app, state_payload


def test_state_payload_maps_done_and_resolved_notes(tmp_path: Path) -> None:
    edit = tmp_path / "edit"
    edit.mkdir()
    (tmp_path / "take.mp4").write_bytes(b"source")
    (edit / "final.mp4").write_bytes(b"render")
    (edit / "coverage.json").write_text(
        json.dumps(
            {
                "meta": {"sources": [str(tmp_path / "take.mp4")]},
                "beats": [{"id": "HOOK", "status": "covered", "reason": "clear"}],
                "director_notes": [],
                "resolved_notes": [{"beat": "DEMO", "closed_by": "take_demo"}],
                "coverage_score": 0.5,
            }
        )
    )

    result = state_payload(tmp_path)

    assert result["phase"] == "done"
    assert result["video_url"] == "/api/video"
    assert result["export_url"] == "/api/export"
    assert result["notes"][0]["resolved"] is True


def test_script_endpoint_streams_text_and_identifies_generator(tmp_path: Path) -> None:
    with patch(
        "palantum.web.app.create_script_stream",
        return_value=(iter(["HOOK\n", "Ein klarer Einstieg."]), "openai"),
    ):
        response = TestClient(create_app(tmp_path)).post(
            "/api/script", json={"prompt": "Unser Produkt soll den Ablauf erklären."}
        )

    assert response.status_code == 200
    assert response.text == "HOOK\nEin klarer Einstieg."
    assert response.headers["x-palantum-generator"] == "openai"


def test_script_endpoint_rejects_empty_prompt(tmp_path: Path) -> None:
    response = TestClient(create_app(tmp_path)).post("/api/script", json={"prompt": "  "})

    assert response.status_code == 422


def test_background_failure_is_exposed_in_state(tmp_path: Path) -> None:
    source = tmp_path / "take.mp4"
    source.write_bytes(b"source")
    with patch("palantum.web.app.analyze", side_effect=KeyError("OPENAI_API_KEY")):
        _process_upload(tmp_path, [source], None)

    result = state_payload(tmp_path)

    assert result["phase"] == "error"
    assert result["error"] == (
        "OPENAI_API_KEY fehlt. Bitte den Schlüssel konfigurieren und erneut versuchen."
    )


def test_upload_passes_motion_pack_to_background_job(tmp_path: Path) -> None:
    template = tmp_path / "templates.zip"
    template.write_bytes(b"zip")
    client = TestClient(create_app(tmp_path, template))
    with patch("palantum.web.app._EXECUTOR.submit") as submit:
        response = client.post(
            "/api/upload", files={"files": ("take.mp4", b"video", "video/mp4")}
        )

    assert response.status_code == 200
    assert submit.call_args.args[1:] == (
        tmp_path.resolve(),
        [tmp_path.resolve() / "take.mp4"],
        None,
        template.resolve(),
    )


def test_frontend_exposes_two_modes_dictation_and_four_output_actions(tmp_path: Path) -> None:
    response = TestClient(create_app(tmp_path)).get("/")

    assert response.status_code == 200
    assert 'id="mode-script"' in response.text
    assert 'id="mode-video"' in response.text
    assert 'aria-label="Sachverhalt diktieren"' in response.text
    assert response.text.count('class="action"') == 4
    assert 'class="corner-logo"' in response.text
    assert "data.phase==='error'" in response.text
    assert response.text.index('id="compose-error"') < response.text.index('id="video-mode"')


def test_frontend_serves_corner_logo(tmp_path: Path) -> None:
    response = TestClient(create_app(tmp_path)).get("/palantum-logo.png")

    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"
