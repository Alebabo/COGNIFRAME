from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from palantum.web.app import _finalize_selection, _process_upload, create_app, state_payload


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


def test_background_upload_builds_chunk_review_from_all_sources(tmp_path: Path) -> None:
    first = tmp_path / "one.mp4"
    second = tmp_path / "two.mp4"
    first.write_bytes(b"one")
    second.write_bytes(b"two")
    state = {"meta": {"sources": [str(first), str(second)]}}
    with (
        patch("palantum.web.app.analyze") as analyze,
        patch("palantum.web.app.load", return_value=state),
        patch("palantum.web.app.build_chunk_variants") as build,
    ):
        _process_upload(tmp_path, [first, second], "brief")

    analyze.assert_called_once_with(
        tmp_path / "edit", [first, second], None, brief="brief"
    )
    build.assert_called_once_with(tmp_path / "edit", [first.resolve(), second.resolve()], None)
    assert json.loads((tmp_path / "edit" / "job.json").read_text())["status"] == "review"


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
    assert 'id="chunk-review"' in response.text
    assert 'id="finalize"' in response.text


def test_state_payload_exposes_two_variants_per_chunk_for_review(tmp_path: Path) -> None:
    edit = tmp_path / "edit"
    edit.mkdir()
    (tmp_path / "take.mp4").write_bytes(b"source")
    (edit / "job.json").write_text(json.dumps({"status": "review"}), encoding="utf-8")
    (edit / "chunks.json").write_text(
        json.dumps(
            {
                "status": "review",
                "chunks": [
                    {
                        "id": "chunk-00-hook",
                        "order": 0,
                        "beat": "HOOK",
                        "selected": None,
                        "variants": [
                            {"id": "a", "label": "Version A", "name": "Clean Cut"},
                            {"id": "b", "label": "Version B", "name": "Motion Cut"},
                        ],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    result = state_payload(tmp_path)

    assert result["phase"] == "review"
    assert [item["id"] for item in result["chunks"][0]["variants"]] == ["a", "b"]
    assert result["selection_complete"] is False


def test_chunk_selection_and_preview_endpoints(tmp_path: Path) -> None:
    edit = tmp_path / "edit"
    preview = edit / "chunks" / "chunk-00-hook" / "a" / "preview.mp4"
    preview.parent.mkdir(parents=True)
    preview.write_bytes(b"preview")
    (edit / "chunks.json").write_text(
        json.dumps(
            {
                "chunks": [
                    {
                        "id": "chunk-00-hook",
                        "selected": None,
                        "variants": [
                            {
                                "id": "a",
                                "preview": "chunks/chunk-00-hook/a/preview.mp4",
                            },
                            {"id": "b", "preview": "missing.mp4"},
                        ],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    client = TestClient(create_app(tmp_path))

    selected = client.post(
        "/api/chunks/chunk-00-hook/selection", json={"variant_id": "a"}
    )
    video = client.get("/api/chunks/chunk-00-hook/variants/a/video")

    assert selected.status_code == 200
    assert selected.json()["selection_complete"] is True
    assert video.status_code == 200
    assert video.content == b"preview"


def test_finalize_requires_complete_selection_and_starts_background_job(
    tmp_path: Path,
) -> None:
    edit = tmp_path / "edit"
    edit.mkdir()
    manifest = {
        "chunks": [
            {"id": "one", "selected": "a", "variants": [{"id": "a"}]},
            {"id": "two", "selected": None, "variants": [{"id": "a"}]},
        ]
    }
    (edit / "chunks.json").write_text(json.dumps(manifest), encoding="utf-8")
    client = TestClient(create_app(tmp_path))

    assert client.post("/api/finalize").status_code == 409
    manifest["chunks"][1]["selected"] = "a"
    (edit / "chunks.json").write_text(json.dumps(manifest), encoding="utf-8")
    with patch("palantum.web.app._EXECUTOR.submit") as submit:
        response = client.post("/api/finalize")

    assert response.status_code == 202
    assert submit.call_args.args == (_finalize_selection, tmp_path.resolve())


def test_frontend_serves_corner_logo(tmp_path: Path) -> None:
    response = TestClient(create_app(tmp_path)).get("/palantum-logo.png")

    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"
