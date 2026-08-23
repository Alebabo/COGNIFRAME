from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from pitchcraft.web.app import (
    _finalize_selection,
    _load_env,
    _process_chunk_recommendations,
    _process_upload,
    _sessions,
    _transcribe_with_whisper,
    create_app,
    state_payload,
)


def test_local_env_never_overrides_injected_process_value(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("DEVIN_API_KEY=stale-local-key\n", encoding="utf-8")
    with patch.dict(os.environ, {"DEVIN_API_KEY": "injected-key"}, clear=False):
        with patch("pitchcraft.web.app.Path.cwd", return_value=tmp_path):
            _load_env()
        assert os.environ["DEVIN_API_KEY"] == "injected-key"


def test_safe_launchers_disable_uv_env_file_loading() -> None:
    root = Path(__file__).resolve().parents[1]
    powershell = (root / "scripts/serve.ps1").read_text()
    assert '"--no-env-file"' in powershell
    assert "& uv" in powershell
    assert "uv run --no-env-file pitchcraft" in (root / "scripts/serve.sh").read_text()


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
        "pitchcraft.web.app.create_script_stream",
        return_value=(iter(["HOOK\n", "A clear opening."]), "devin"),
    ):
        response = TestClient(create_app(tmp_path)).post(
            "/api/script", json={"prompt": "Our product should explain the workflow."}
        )

    assert response.status_code == 200
    assert response.text == "HOOK\nA clear opening."
    assert response.headers["x-pitchcraft-generator"] == "devin"


def test_script_endpoint_rejects_empty_prompt(tmp_path: Path) -> None:
    response = TestClient(create_app(tmp_path)).post("/api/script", json={"prompt": "  "})

    assert response.status_code == 422


def test_background_failure_is_exposed_in_state(tmp_path: Path) -> None:
    source = tmp_path / "take.mp4"
    source.write_bytes(b"source")
    with patch("pitchcraft.web.app.analyze", side_effect=KeyError("OPENAI_API_KEY")):
        _process_upload(tmp_path, [source], None)

    result = state_payload(tmp_path)

    assert result["phase"] == "error"
    assert result["job_status"] == "failed"
    assert result["error"] == "OPENAI_API_KEY is missing. Configure the key in .env."


def test_state_payload_exposes_precise_video_job_status(tmp_path: Path) -> None:
    edit = tmp_path / "edit"
    edit.mkdir()
    (edit / "job.json").write_text(json.dumps({"status": "finalizing"}), encoding="utf-8")

    result = state_payload(tmp_path)

    assert result["phase"] == "working"
    assert result["job_status"] == "finalizing"


def test_source_without_active_job_does_not_show_video_progress(tmp_path: Path) -> None:
    (tmp_path / "demoeins.mp4").write_bytes(b"source")

    result = state_payload(tmp_path)

    assert result["phase"] == "empty"
    assert result["job_status"] == "idle"


def test_upload_passes_motion_pack_to_background_job(tmp_path: Path) -> None:
    template = tmp_path / "templates.zip"
    template.write_bytes(b"zip")
    client = TestClient(create_app(tmp_path, template))
    with patch("pitchcraft.web.app._EXECUTOR.submit") as submit:
        response = client.post(
            "/api/upload", files={"files": ("take.mp4", b"video", "video/mp4")}
        )

    assert response.status_code == 200
    worker, worker_root, upload_sources, brief, motion_source = submit.call_args.args
    assert worker.__name__ == "_process_upload"
    assert worker_root == tmp_path.resolve()
    assert upload_sources[0].parent == tmp_path.resolve() / "uploads"
    assert upload_sources[0].suffix == ".mp4"
    assert upload_sources[0].read_bytes() == b"video"
    assert brief is None
    assert motion_source == template.resolve()


def test_upload_cannot_overwrite_project_files(tmp_path: Path) -> None:
    protected = tmp_path / ".env"
    protected.write_text("DEVIN_PAT=keep-me", encoding="utf-8")
    client = TestClient(create_app(tmp_path))

    response = client.post(
        "/api/upload", files={"files": (".env", b"overwrite", "text/plain")}
    )

    assert response.status_code == 415
    assert protected.read_text(encoding="utf-8") == "DEVIN_PAT=keep-me"
    assert not (tmp_path / "uploads").exists()


def test_background_upload_builds_chunk_review_from_all_sources(tmp_path: Path) -> None:
    first = tmp_path / "one.mp4"
    second = tmp_path / "two.mp4"
    first.write_bytes(b"one")
    second.write_bytes(b"two")
    state = {"meta": {"sources": [str(first), str(second)]}}
    with (
        patch("pitchcraft.web.app.analyze") as analyze,
        patch("pitchcraft.web.app.load", return_value=state),
        patch(
            "pitchcraft.web.app.build_chunk_variants",
            return_value={"generation_id": "generation-1"},
        ) as build,
        patch("pitchcraft.web.app._RECOMMENDATION_EXECUTOR.submit") as submit,
    ):
        _process_upload(tmp_path, [first, second], "brief")

    analyze.assert_called_once_with(
        tmp_path / "edit", [first, second], None, brief="brief"
    )
    build.assert_called_once_with(tmp_path / "edit", [first.resolve(), second.resolve()], None)
    assert json.loads((tmp_path / "edit" / "job.json").read_text())["status"] == "review"
    assert submit.call_args.args == (
        _process_chunk_recommendations,
        tmp_path,
        "generation-1",
    )


def test_review_and_manual_selection_are_available_before_delayed_a5(
    tmp_path: Path,
) -> None:
    source = tmp_path / "take.mp4"
    source.write_bytes(b"source")
    edit = tmp_path / "edit"
    manifest: dict[str, Any] = {
        "generation_id": "generation-delayed",
        "status": "review",
        "recommendations_status": "pending",
        "chunks": [
            {
                "id": "one",
                "selected": None,
                "variants": [{"id": "a"}, {"id": "b"}],
            }
        ],
    }

    def build(*_args: object, **_kwargs: object) -> dict[str, object]:
        edit.mkdir(exist_ok=True)
        (edit / "chunks.json").write_text(json.dumps(manifest), encoding="utf-8")
        return manifest

    with (
        patch("pitchcraft.web.app.analyze"),
        patch(
            "pitchcraft.web.app.load",
            return_value={"meta": {"sources": [str(source)]}},
        ),
        patch("pitchcraft.web.app.build_chunk_variants", side_effect=build),
        patch("pitchcraft.web.app._RECOMMENDATION_EXECUTOR.submit") as submit,
    ):
        _process_upload(tmp_path, [source], None)

    assert json.loads((edit / "job.json").read_text(encoding="utf-8"))["status"] == "review"
    client = TestClient(create_app(tmp_path))
    initial = client.get("/api/state").json()
    response = client.post(
        "/api/chunks/one/selection", json={"variant_id": "a"}
    )
    assert response.status_code == 200
    assert initial["recommendations_status"] == "pending"
    assert initial["chunks"][0]["recommendation"] is None
    assert submit.call_args.args == (
        _process_chunk_recommendations,
        tmp_path,
        "generation-delayed",
    )
    worker, *args = submit.call_args.args
    with patch(
        "pitchcraft.web.app.recommend_chunk_variants",
        return_value={
            "one": {"status": "ready", "variant_id": "b", "reason": "Motion"}
        },
    ):
        assert worker(*args) is True
    updated = client.get("/api/state").json()
    assert updated["recommendations_status"] == "complete"
    assert updated["chunks"][0]["recommendation"]["variant_id"] == "b"
    assert updated["chunks"][0]["selected"] == "a"


def test_a5_timeout_stays_optional_and_keeps_review_selectable(tmp_path: Path) -> None:
    edit = tmp_path / "edit"
    edit.mkdir()
    manifest: dict[str, Any] = {
        "generation_id": "generation-timeout",
        "chunks": [
            {
                "id": "one",
                "selected": None,
                "variants": [{"id": "a"}, {"id": "b"}],
            }
        ],
    }
    (edit / "chunks.json").write_text(json.dumps(manifest), encoding="utf-8")
    (edit / "job.json").write_text(json.dumps({"status": "review"}), encoding="utf-8")

    with patch(
        "pitchcraft.web.app.recommend_chunk_variants",
        side_effect=TimeoutError("A5 timed out"),
    ):
        updated = _process_chunk_recommendations(tmp_path, "generation-timeout")

    stored = json.loads((edit / "chunks.json").read_text(encoding="utf-8"))
    assert updated is True
    assert stored["recommendations_status"] == "unavailable"
    assert stored["chunks"][0]["recommendation"]["status"] == "unavailable"
    assert json.loads((edit / "job.json").read_text(encoding="utf-8"))["status"] == "review"
    assert (
        TestClient(create_app(tmp_path)).post(
            "/api/chunks/one/selection", json={"variant_id": "b"}
        ).status_code
        == 200
    )


def test_stale_a5_batch_cannot_mutate_a_newer_manifest(tmp_path: Path) -> None:
    edit = tmp_path / "edit"
    edit.mkdir()
    old = {
        "generation_id": "generation-old",
        "chunks": [
            {"id": "one", "selected": None, "variants": [{"id": "a"}, {"id": "b"}]}
        ],
    }
    new = {
        "generation_id": "generation-new",
        "chunks": [
            {"id": "one", "selected": "b", "variants": [{"id": "a"}, {"id": "b"}]}
        ],
    }
    (edit / "chunks.json").write_text(json.dumps(old), encoding="utf-8")
    (edit / "job.json").write_text(json.dumps({"status": "review"}), encoding="utf-8")
    started = threading.Event()
    release = threading.Event()
    outcome: list[bool] = []

    def delayed(*_args: object, **_kwargs: object) -> dict[str, dict[str, object]]:
        started.set()
        assert release.wait(3)
        return {"one": {"status": "ready", "variant_id": "a", "reason": "A"}}

    with patch("pitchcraft.web.app.recommend_chunk_variants", side_effect=delayed):
        worker = threading.Thread(
            target=lambda: outcome.append(
                _process_chunk_recommendations(tmp_path, "generation-old")
            )
        )
        worker.start()
        assert started.wait(3)
        (edit / "chunks.json").write_text(json.dumps(new), encoding="utf-8")
        release.set()
        worker.join(3)

    assert not worker.is_alive()
    assert outcome == [False]
    assert json.loads((edit / "chunks.json").read_text(encoding="utf-8")) == new


def test_a5_merge_preserves_a_concurrent_manual_selection(tmp_path: Path) -> None:
    edit = tmp_path / "edit"
    edit.mkdir()
    manifest: dict[str, Any] = {
        "generation_id": "generation-concurrent",
        "chunks": [
            {"id": "one", "selected": None, "variants": [{"id": "a"}, {"id": "b"}]}
        ],
    }
    (edit / "chunks.json").write_text(json.dumps(manifest), encoding="utf-8")
    (edit / "job.json").write_text(json.dumps({"status": "review"}), encoding="utf-8")
    started = threading.Event()
    release = threading.Event()
    outcome: list[bool] = []

    def delayed(*_args: object, **_kwargs: object) -> dict[str, dict[str, object]]:
        started.set()
        assert release.wait(3)
        return {"one": {"status": "ready", "variant_id": "a", "reason": "A"}}

    with patch("pitchcraft.web.app.recommend_chunk_variants", side_effect=delayed):
        worker = threading.Thread(
            target=lambda: outcome.append(
                _process_chunk_recommendations(tmp_path, "generation-concurrent")
            )
        )
        worker.start()
        assert started.wait(3)
        selected = TestClient(create_app(tmp_path)).post(
            "/api/chunks/one/selection", json={"variant_id": "b"}
        )
        release.set()
        worker.join(3)

    stored = json.loads((edit / "chunks.json").read_text(encoding="utf-8"))
    assert not worker.is_alive()
    assert selected.status_code == 200
    assert outcome == [True]
    assert stored["chunks"][0]["selected"] == "b"
    assert stored["chunks"][0]["recommendation"]["variant_id"] == "a"
    assert stored["recommendations_status"] == "complete"


def test_a5_batch_is_skipped_after_job_leaves_review(tmp_path: Path) -> None:
    edit = tmp_path / "edit"
    edit.mkdir()
    manifest: dict[str, Any] = {
        "generation_id": "generation-finalizing",
        "chunks": [
            {"id": "one", "selected": "a", "variants": [{"id": "a"}, {"id": "b"}]}
        ],
    }
    path = edit / "chunks.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    (edit / "job.json").write_text(json.dumps({"status": "review"}), encoding="utf-8")
    started = threading.Event()
    release = threading.Event()
    outcome: list[bool] = []

    def delayed(*_args: object, **_kwargs: object) -> dict[str, dict[str, object]]:
        started.set()
        assert release.wait(3)
        return {"one": {"status": "ready", "variant_id": "b", "reason": "B"}}

    with patch("pitchcraft.web.app.recommend_chunk_variants", side_effect=delayed):
        worker = threading.Thread(
            target=lambda: outcome.append(
                _process_chunk_recommendations(tmp_path, "generation-finalizing")
            )
        )
        worker.start()
        assert started.wait(3)
        (edit / "job.json").write_text(json.dumps({"status": "queued"}), encoding="utf-8")
        release.set()
        worker.join(3)

    assert not worker.is_alive()
    assert outcome == [False]
    assert json.loads(path.read_text(encoding="utf-8")) == manifest


def test_a5_submit_failure_finishes_pending_state_without_blocking_review(
    tmp_path: Path,
) -> None:
    source = tmp_path / "take.mp4"
    source.write_bytes(b"source")
    edit = tmp_path / "edit"
    manifest: dict[str, Any] = {
        "generation_id": "generation-submit-failed",
        "status": "review",
        "recommendations_status": "pending",
        "chunks": [
            {"id": "one", "selected": None, "variants": [{"id": "a"}, {"id": "b"}]}
        ],
    }

    def build(*_args: object, **_kwargs: object) -> dict[str, object]:
        edit.mkdir(exist_ok=True)
        (edit / "chunks.json").write_text(json.dumps(manifest), encoding="utf-8")
        return manifest

    with (
        patch("pitchcraft.web.app.analyze"),
        patch(
            "pitchcraft.web.app.load",
            return_value={"meta": {"sources": [str(source)]}},
        ),
        patch("pitchcraft.web.app.build_chunk_variants", side_effect=build),
        patch(
            "pitchcraft.web.app._RECOMMENDATION_EXECUTOR.submit",
            side_effect=RuntimeError("executor unavailable"),
        ),
    ):
        _process_upload(tmp_path, [source], None)

    state = state_payload(tmp_path)
    assert state["phase"] == "review"
    assert state["recommendations_status"] == "unavailable"
    assert state["chunks"][0]["recommendation"]["status"] == "unavailable"


def test_frontend_exposes_canvas_agent_status_and_actions(tmp_path: Path) -> None:
    response = TestClient(create_app(tmp_path)).get("/")

    assert response.status_code == 200
    assert 'id="prompt"' in response.text
    assert 'id="avatar-stack"' in response.text
    assert 'id="cursor-director"' in response.text
    assert 'id="cursor-strategist"' in response.text
    assert 'id="cursor-supervisor"' in response.text
    assert 'id="toolbar"' in response.text
    assert 'id="mic"' in response.text
    assert 'id="generate"' in response.text
    assert 'class="corner-logo"' in response.text
    assert 'id="chunk-review"' in response.text
    assert 'id="finalize"' in response.text
    assert 'id="apply-recommendations"' in response.text
    assert 'id="job-status-card"' in response.text
    assert 'id="job-status-eta"' in response.text
    assert 'id="job-status-progress"' in response.text
    assert 'id="job-agent-activity"' in response.text
    assert 'id="context-hint-card"' in response.text
    assert "function renderJobStatus(data)" in response.text
    assert "function renderAgentActivity(sessions = [])" in response.text
    assert "function updateContextHint()" in response.text
    assert "fetch('/api/activity')" in response.text
    assert "data.job_status" in response.text
    assert "animation: travel" not in response.text
    assert "new AbortController()" in response.text
    assert "signal: controller.signal" in response.text
    assert "error?.name === 'AbortError'" in response.text
    assert "sequence !== state.orchestrationSequence" in response.text
    assert "revision !== state.textRevision" in response.text
    assert "state.seenRequestIds.has(responseId)" in response.text
    assert "data.recommendations_status === 'pending'" in response.text
    assert "state.uploading || state.pollingRecommendations" in response.text


def test_frontend_defers_job_ui_until_video_processing_is_running(tmp_path: Path) -> None:
    html = TestClient(create_app(tmp_path)).get("/").text

    canvas_activity = html.split("function beginCanvasAgentActivity", 1)[1].split(
        "function endCanvasAgentActivity", 1
    )[0]
    upload_start = html.split("async function uploadVideos", 1)[1].split(
        "const form = new FormData();", 1
    )[0]

    assert "showJobStatus" not in canvas_activity
    assert "$('progress').hidden = false" not in upload_start
    assert "title: 'Video job started'" not in upload_start
    assert "if (!['running', 'finalizing'].includes(reportedStatus))" in html
    assert "const jobStarted = ['running', 'finalizing'].includes" in html


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
                        "recommendation": {
                            "status": "ready",
                            "variant_id": "b",
                            "reason": "The motion version explains the beat.",
                        },
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
    assert result["chunks"][0]["recommendation"]["variant_id"] == "b"
    assert result["selection_complete"] is False


def test_state_uses_one_coherent_chunk_manifest_snapshot(tmp_path: Path) -> None:
    edit = tmp_path / "edit"
    edit.mkdir()
    (edit / "job.json").write_text(json.dumps({"status": "review"}), encoding="utf-8")
    pending: dict[str, Any] = {
        "generation_id": "one",
        "recommendations_status": "pending",
        "chunks": [
            {"id": "one", "selected": None, "variants": [{"id": "a"}, {"id": "b"}]}
        ],
    }
    completed = {
        **pending,
        "recommendations_status": "complete",
        "chunks": [
            {
                **pending["chunks"][0],
                "recommendation": {
                    "status": "ready",
                    "variant_id": "b",
                    "reason": "B",
                },
            }
        ],
    }

    with patch(
        "pitchcraft.web.app._read_chunks", side_effect=[pending, completed]
    ) as read_chunks:
        result = state_payload(tmp_path)

    assert read_chunks.call_count == 1
    assert result["recommendations_status"] == "pending"
    assert result["chunks"][0]["recommendation"] is None


def test_legacy_chunk_manifest_without_recommendation_stays_manual(tmp_path: Path) -> None:
    edit = tmp_path / "edit"
    edit.mkdir()
    (edit / "job.json").write_text(json.dumps({"status": "review"}), encoding="utf-8")
    (edit / "chunks.json").write_text(
        json.dumps(
            {
                "chunks": [
                    {
                        "id": "legacy",
                        "order": 0,
                        "beat": "HOOK",
                        "selected": "removed-variant",
                        "variants": [{"id": "a"}, {"id": "b"}],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    client = TestClient(create_app(tmp_path))

    state = client.get("/api/state").json()

    assert state["chunks"][0]["recommendation"] is None
    assert state["recommendations_status"] == "complete"
    assert state["selection_complete"] is False
    assert client.post("/api/finalize").status_code == 409


def test_variant_supervisor_sessions_are_aggregated_by_role(tmp_path: Path) -> None:
    edit = tmp_path / "edit"
    edit.mkdir()
    (edit / "sessions.json").write_text(
        json.dumps(
            {
                "chunk-00-hook.recommendation": {
                    "role": "A5",
                    "status": "done",
                    "url": "https://app.devin.ai/sessions/a5-done",
                },
                "chunk-01-demo.recommendation": {
                    "role": "A5",
                    "status": "running",
                    "url": "https://app.devin.ai/sessions/a5-running",
                },
            }
        ),
        encoding="utf-8",
    )

    supervisor = next(item for item in _sessions(edit) if item["role"] == "Variant Supervisor")

    assert supervisor["status"] == "running"
    assert supervisor["url"] == "https://app.devin.ai/sessions/a5-running"


def test_activity_endpoint_reports_agents_without_reading_render_manifest(
    tmp_path: Path,
) -> None:
    edit = tmp_path / "edit"
    edit.mkdir()
    (edit / "job.json").write_text(
        json.dumps({"status": "running"}), encoding="utf-8"
    )
    (edit / "sessions.json").write_text(
        json.dumps(
            {
                "A6": {
                    "role": "A6",
                    "status": "running",
                    "url": "https://app.devin.ai/sessions/a6",
                }
            }
        ),
        encoding="utf-8",
    )
    client = TestClient(create_app(tmp_path))

    with patch("pitchcraft.web.app._read_chunks", side_effect=AssertionError):
        response = client.get("/api/activity")

    assert response.status_code == 200
    assert response.json()["job_status"] == "running"
    graphics = next(
        item for item in response.json()["sessions"] if item["role"] == "Graphics Director"
    )
    assert graphics["status"] == "running"


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
    manifest: dict[str, Any] = {
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
    with patch("pitchcraft.web.app._EXECUTOR.submit") as submit:
        response = client.post("/api/finalize")

    assert response.status_code == 202
    assert submit.call_args.args == (_finalize_selection, tmp_path.resolve())


def test_apply_recommendations_is_atomic_and_selects_every_chunk(tmp_path: Path) -> None:
    edit = tmp_path / "edit"
    edit.mkdir()
    manifest = {
        "chunks": [
            {
                "id": "one",
                "selected": "a",
                "recommendation": {"status": "ready", "variant_id": "b", "reason": "B"},
                "variants": [{"id": "a"}, {"id": "b"}],
            },
            {
                "id": "two",
                "selected": None,
                "recommendation": {"status": "ready", "variant_id": "a", "reason": "A"},
                "variants": [{"id": "a"}, {"id": "b"}],
            },
        ]
    }
    (edit / "chunks.json").write_text(json.dumps(manifest), encoding="utf-8")

    response = TestClient(create_app(tmp_path)).post("/api/chunks/recommendations/apply")

    assert response.status_code == 200
    assert response.json()["selection_complete"] is True
    stored = json.loads((edit / "chunks.json").read_text(encoding="utf-8"))
    assert [chunk["selected"] for chunk in stored["chunks"]] == ["b", "a"]


def test_apply_recommendations_applies_ready_subset_atomically(tmp_path: Path) -> None:
    edit = tmp_path / "edit"
    edit.mkdir()
    manifest = {
        "chunks": [
            {
                "id": "one",
                "selected": None,
                "recommendation": {"status": "ready", "variant_id": "b", "reason": "B"},
                "variants": [{"id": "a"}, {"id": "b"}],
            },
            {
                "id": "two",
                "selected": None,
                "recommendation": {"status": "unavailable", "variant_id": None, "reason": ""},
                "variants": [{"id": "a"}, {"id": "b"}],
            },
        ]
    }
    path = edit / "chunks.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")

    response = TestClient(create_app(tmp_path)).post("/api/chunks/recommendations/apply")

    assert response.status_code == 200
    assert response.json()["selection_complete"] is False
    stored = json.loads(path.read_text(encoding="utf-8"))
    assert [chunk["selected"] for chunk in stored["chunks"]] == ["b", None]


def test_selection_cannot_make_a_finished_master_stale(tmp_path: Path) -> None:
    edit = tmp_path / "edit"
    edit.mkdir()
    manifest = {
        "chunks": [
            {
                "id": "one",
                "selected": "a",
                "recommendation": {"status": "ready", "variant_id": "b", "reason": "B"},
                "variants": [{"id": "a"}, {"id": "b"}],
            }
        ]
    }
    path = edit / "chunks.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    (edit / "job.json").write_text(json.dumps({"status": "done"}), encoding="utf-8")
    client = TestClient(create_app(tmp_path))

    selected = client.post("/api/chunks/one/selection", json={"variant_id": "b"})
    applied = client.post("/api/chunks/recommendations/apply")

    assert selected.status_code == 409
    assert applied.status_code == 409
    assert json.loads(path.read_text(encoding="utf-8"))["chunks"][0]["selected"] == "a"


def test_frontend_serves_corner_logo(tmp_path: Path) -> None:
    response = TestClient(create_app(tmp_path)).get("/pitchcraft-logo.png")

    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"


def test_whisper_call_is_offloaded_from_async_request_loop(tmp_path: Path) -> None:
    worker = AsyncMock(return_value="Transkribierter Text")
    client = TestClient(create_app(tmp_path))

    with (
        patch.dict("os.environ", {"OPENAI_API_KEY": "whisper-test-key"}),
        patch("pitchcraft.web.app.run_in_threadpool", worker),
    ):
        response = client.post(
            "/api/transcribe",
            files={"file": ("voice.wav", b"audio", "audio/wav")},
        )

    assert response.status_code == 200
    assert response.json() == {"text": "Transkribierter Text"}
    assert worker.await_args is not None
    function, _temporary, api_key = worker.await_args.args
    assert function is _transcribe_with_whisper
    assert api_key == "whisper-test-key"
