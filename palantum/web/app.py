from __future__ import annotations

import json
import os
import threading
import uuid
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Annotated, Any, NoReturn

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from palantum.export import export_project
from palantum.orchestrator import (
    _schema,
    analyze,
    build_chunk_variants,
    finalize_chunk_variants,
    recommend_chunk_variants,
)
from palantum.state import load
from palantum.web.script import (
    CanvasAgentResponseError,
    CanvasAgentSuggestion,
    CanvasAgentUnavailableError,
    CanvasOrchestrationResult,
    assist_canvas_agent,
    build_canvas_metadata,
    create_script_stream,
    orchestrate_canvas,
    parse_canvas_beats,
)

ROLE_NAMES = {
    "A1": "Script Supervisor",
    "A2": "Director",
    "A3": "Stratege",
    "A4": "Cutter",
    "A5": "Variant Supervisor",
    "A6": "Graphics Director",
    "A6W": "Graphics Worker",
    "A7": "QC",
}
_EXECUTOR = ThreadPoolExecutor(max_workers=1)
_RECOMMENDATION_EXECUTOR = ThreadPoolExecutor(max_workers=2)
_PIPELINE_LOCK = threading.Lock()
_MANIFEST_LOCK = threading.Lock()
_CANVAS_LOCK = threading.Lock()
_JOB_LOCK = threading.Lock()
_VIDEO_SUFFIXES = {".m4v", ".mov", ".mp4", ".webm"}


class CanvasPersistenceError(RuntimeError):
    """Raised when an existing canvas cannot be read without risking data loss."""


def _load_env() -> None:
    """Load the first local .env without ever overriding the process environment."""
    candidates = (Path.cwd() / ".env", Path(__file__).resolve().parents[2] / ".env")
    seen: set[Path] = set()
    for candidate in candidates:
        resolved = candidate.resolve()
        if resolved in seen or not resolved.is_file():
            continue
        seen.add(resolved)
        try:
            for raw_line in resolved.read_text(encoding="utf-8").splitlines():
                line = raw_line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                key = key.strip()
                value = value.strip().strip("'\"")
                if key and key not in os.environ:
                    os.environ[key] = value
            return
        except OSError:
            continue


_load_env()


class ScriptRequest(BaseModel):
    prompt: str


class CanvasRequest(BaseModel):
    title: str | None = None
    text: str | None = None
    beats: dict[str, str] = Field(default_factory=dict)
    attached_videos: dict[str, str] = Field(default_factory=dict)


class DevinOrchestrateRequest(BaseModel):
    text: str
    cursor_offset: int | None = None
    accepted_ghost_texts: list[str] = Field(default_factory=list)
    request_id: str | None = None


class CanvasAssistRequest(BaseModel):
    text: str
    cursor_offset: int | None = None
    accepted_ghost_texts: list[str] = Field(default_factory=list)
    agent_id: str
    beat: str | None = None
    request_id: str | None = None


class ChunkSelection(BaseModel):
    variant_id: str


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _canvas_path(edit_dir: Path) -> Path:
    return edit_dir / "canvas.json"


def _load_canvas(edit_dir: Path) -> dict[str, Any]:
    path = _canvas_path(edit_dir)
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return build_canvas_metadata(
                    title=str(data.get("title", "Mein Startup Pitch")),
                    text=str(data.get("text", "")),
                    beats=data.get("beats") if isinstance(data.get("beats"), dict) else None,
                    attached_videos=(
                        data.get("attached_videos")
                        if isinstance(data.get("attached_videos"), dict)
                        else None
                    ),
                )
            raise CanvasPersistenceError("canvas.json enthält kein JSON-Objekt.")
        except CanvasPersistenceError:
            raise
        except (OSError, TypeError, ValueError) as error:
            raise CanvasPersistenceError(
                "canvas.json konnte nicht sicher gelesen werden; die Datei blieb unverändert."
            ) from error
    return build_canvas_metadata()


def _save_canvas(edit_dir: Path, canvas: dict[str, Any]) -> None:
    _atomic_json(_canvas_path(edit_dir), canvas)


def _job_path(edit_dir: Path) -> Path:
    return edit_dir / "job.json"


def _write_job(edit_dir: Path, status: str, error: str | None = None) -> None:
    _atomic_json(_job_path(edit_dir), {"status": status, "error": error})


def _job(edit_dir: Path) -> dict[str, Any]:
    path = _job_path(edit_dir)
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _chunks_path(edit_dir: Path) -> Path:
    return edit_dir / "chunks.json"


def _read_chunks(edit_dir: Path) -> dict[str, Any]:
    path = _chunks_path(edit_dir)
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _write_chunks(edit_dir: Path, payload: dict[str, Any]) -> None:
    _atomic_json(_chunks_path(edit_dir), payload)


def _recommendation_snapshot(
    edit_dir: Path, generation_id: str
) -> dict[str, Any] | None:
    """Read the matching review manifest while respecting endpoint lock order."""
    with _JOB_LOCK:
        if str(_job(edit_dir).get("status", "")) != "review":
            return None
        with _MANIFEST_LOCK:
            manifest = _read_chunks(edit_dir)
            if str(manifest.get("generation_id", "")) != generation_id:
                return None
            return manifest


def _apply_recommendation_batch(
    edit_dir: Path,
    generation_id: str,
    recommendations: dict[str, dict[str, Any]],
    *,
    status: str = "complete",
) -> bool:
    """Atomically add only A5 fields to the current matching review manifest."""
    with _JOB_LOCK:
        if str(_job(edit_dir).get("status", "")) != "review":
            return False
        with _MANIFEST_LOCK:
            manifest = _read_chunks(edit_dir)
            if str(manifest.get("generation_id", "")) != generation_id:
                return False
            chunks = {
                str(chunk.get("id", "")): chunk
                for chunk in manifest.get("chunks", [])
                if isinstance(chunk, dict)
            }
            updated = manifest.get("recommendations_status") != status
            manifest["recommendations_status"] = status
            for chunk_id, recommendation in recommendations.items():
                chunk = chunks.get(chunk_id)
                if chunk is None or not isinstance(recommendation, dict):
                    continue
                chunk["recommendation"] = dict(recommendation)
                updated = True
            if updated:
                _write_chunks(edit_dir, manifest)
            return updated


def _process_chunk_recommendations(videos_dir: Path, generation_id: str) -> bool:
    """Run optional A5 work without changing job phase or blocking finalization."""
    edit_dir = videos_dir / "edit"
    snapshot = _recommendation_snapshot(edit_dir, generation_id)
    if snapshot is None:
        return False
    try:
        recommendations = recommend_chunk_variants(edit_dir, snapshot)
    except Exception:
        terminal_status = "unavailable"
        recommendations = {
            str(chunk.get("id", "")): {
                "status": "unavailable",
                "variant_id": None,
                "reason": "Die KI-Empfehlung ist derzeit nicht verfügbar.",
            }
            for chunk in snapshot.get("chunks", [])
            if isinstance(chunk, dict) and str(chunk.get("id", ""))
        }
    else:
        terminal_status = "complete"
    return _apply_recommendation_batch(
        edit_dir,
        generation_id,
        recommendations,
        status=terminal_status,
    )


def _assert_selection_mutable(edit_dir: Path) -> None:
    status = str(_job(edit_dir).get("status", ""))
    if status in {"queued", "running", "finalizing", "done"}:
        raise HTTPException(
            status_code=409,
            detail="chunk selections cannot change while or after final rendering",
        )


def _recommendation_payload(chunk: dict[str, Any]) -> dict[str, Any] | None:
    recommendation = chunk.get("recommendation")
    if not isinstance(recommendation, dict):
        return None
    status = str(recommendation.get("status", "unavailable"))
    variant_id = recommendation.get("variant_id")
    variants = {
        str(item.get("id"))
        for item in chunk.get("variants", [])
        if isinstance(item, dict)
    }
    if status != "ready" or str(variant_id) not in variants:
        status = "unavailable"
        variant_id = None
    return {
        "status": status,
        "variant_id": variant_id,
        "reason": str(recommendation.get("reason", "")),
    }


def _chunk_payload(
    edit_dir: Path, manifest: dict[str, Any] | None = None
) -> list[dict[str, Any]]:
    if manifest is None:
        manifest = _read_chunks(edit_dir)
    result: list[dict[str, Any]] = []
    for chunk in manifest.get("chunks", []):
        if not isinstance(chunk, dict):
            continue
        chunk_id = str(chunk.get("id", ""))
        variants: list[dict[str, Any]] = []
        for variant in chunk.get("variants", []):
            if not isinstance(variant, dict):
                continue
            variant_id = str(variant.get("id", ""))
            variants.append(
                {
                    "id": variant_id,
                    "label": str(variant.get("label", variant_id)),
                    "name": str(variant.get("name", "")),
                    "strategy": str(variant.get("strategy", "")),
                    "status": str(variant.get("status", "ready")),
                    "duration_s": float(variant.get("duration_s", 0)),
                    "video_url": f"/api/chunks/{chunk_id}/variants/{variant_id}/video",
                }
            )
        result.append(
            {
                "id": chunk_id,
                "order": int(chunk.get("order", len(result))),
                "beat": str(chunk.get("beat", "")),
                "selected": chunk.get("selected"),
                "recommendation": _recommendation_payload(chunk),
                "variants": variants,
            }
        )
    return sorted(result, key=lambda item: int(item["order"]))


def _chunk_selection_is_valid(chunk: Any) -> bool:
    if not isinstance(chunk, dict):
        return False
    selected = chunk.get("selected")
    variants = chunk.get("variants", [])
    return selected is not None and any(
        isinstance(variant, dict) and variant.get("id") == selected for variant in variants
    )


def _selection_complete(chunks: Any) -> bool:
    return isinstance(chunks, list) and bool(chunks) and all(
        _chunk_selection_is_valid(chunk) for chunk in chunks
    )


def _failure_message(error: Exception) -> str:
    missing = str(error).strip("'") if isinstance(error, KeyError) else ""
    if missing in {"OPENAI_API_KEY", "DEVIN_API_KEY", "DEVIN_PAT"}:
        return f"{missing} fehlt. Bitte den Schlüssel in der .env konfigurieren."
    detail = str(error).strip()
    return f"{type(error).__name__}: {detail}" if detail else type(error).__name__


def _sessions(edit_dir: Path) -> list[dict[str, Any]]:
    path = edit_dir / "sessions.json"
    raw: dict[str, Any] = {}
    if path.exists():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                raw = loaded
        except (OSError, TypeError, ValueError):
            pass
    result = []
    for role, name in ROLE_NAMES.items():
        entries = [
            entry
            for key, entry in raw.items()
            if isinstance(entry, dict) and (key == role or entry.get("role") == role)
        ]
        priority = {"idle": 0, "done": 1, "failed": 2, "running": 3}
        entry = max(
            entries,
            key=lambda item: priority.get(str(item.get("status", "idle")), 0),
            default={},
        )
        result.append(
            {
                "role": name,
                "status": str(entry.get("status", "idle")),
                "url": entry.get("url"),
            }
        )
    return result


def _note_payload(note: dict[str, Any], resolved: bool = False) -> dict[str, Any]:
    shot = note.get("shot")
    if not isinstance(shot, dict):
        shot = {"line": "", "framing": "", "duration_s": 0, "delivery": ""}
    return {
        "beat": str(note.get("beat", "")),
        "impact": str(note.get("impact", "medium")),
        "resolved": resolved,
        "why": str(note.get("why", "")),
        "shot": {
            "line": str(shot.get("line", "")),
            "framing": str(shot.get("framing", "")),
            "duration_s": shot.get("duration_s", 0),
            "delivery": str(shot.get("delivery", "")),
        },
    }


def state_payload(videos_dir: Path) -> dict[str, Any]:
    edit_dir = videos_dir / "edit"
    state = load(edit_dir, _schema())
    job = _job(edit_dir)
    canvas = _load_canvas(edit_dir)
    final = edit_dir / "final.mp4"
    job_status = str(job.get("status", ""))
    with _MANIFEST_LOCK:
        chunk_manifest = _read_chunks(edit_dir)
    chunks = _chunk_payload(edit_dir, chunk_manifest)
    recommendations_status = str(
        chunk_manifest.get("recommendations_status", "complete")
    )
    if recommendations_status not in {"pending", "complete", "unavailable"}:
        recommendations_status = "complete"
    if job_status == "failed":
        phase = "error"
    elif job_status in {"queued", "running", "finalizing"}:
        phase = "working"
    elif job_status == "review":
        phase = "review"
    elif final.exists():
        phase = "done"
    else:
        # Source files and cached coverage do not mean that a video job is active.
        # Only an explicit job status may put the UI into its progress state.
        phase = "empty"
    open_notes = [
        _note_payload(note)
        for note in state.get("director_notes", [])
        if isinstance(note, dict)
    ]
    resolved_notes = []
    for item in state.get("resolved_notes", []):
        if not isinstance(item, dict):
            continue
        nested = item.get("note")
        note = nested if isinstance(nested, dict) else item
        resolved_notes.append(_note_payload(note, resolved=True))
    beats = [
        {
            "id": str(beat["id"]),
            "status": str(beat["status"]),
            "reason": str(beat.get("reason", "")),
        }
        for beat in state.get("beats", [])
    ]
    selection_complete = _selection_complete(chunks)
    return {
        "phase": phase,
        "job_status": job_status or "idle",
        "coverage": {"score": state.get("coverage_score", 0.0), "beats": beats},
        "canvas": canvas,
        "notes": open_notes + resolved_notes,
        "sessions": [] if phase == "empty" else _sessions(edit_dir),
        "video_url": "/api/video" if final.exists() else None,
        "export_url": "/api/export" if final.exists() else None,
        "error": job.get("error") if phase == "error" else None,
        "chunks": chunks,
        "recommendations_status": recommendations_status,
        "selection_complete": selection_complete,
    }


def _process_upload(
    videos_dir: Path,
    sources: list[Path],
    brief: str | None,
    template_source: Path | None = None,
) -> None:
    with _PIPELINE_LOCK:
        edit_dir = videos_dir / "edit"
        _write_job(edit_dir, "running")
        try:
            analyze(edit_dir, sources, template_source, brief=brief)
            state = load(edit_dir, _schema())
            all_sources = [
                Path(str(value)).resolve() for value in state.get("meta", {}).get("sources", [])
            ]
            with _MANIFEST_LOCK:
                manifest = build_chunk_variants(edit_dir, all_sources, template_source)
            _write_job(edit_dir, "review")
        except Exception as error:
            _write_job(edit_dir, "failed", _failure_message(error))
        else:
            generation_id = str(manifest.get("generation_id", ""))
            if generation_id:
                try:
                    _RECOMMENDATION_EXECUTOR.submit(
                        _process_chunk_recommendations, videos_dir, generation_id
                    )
                except RuntimeError:
                    unavailable = {
                        str(chunk.get("id", "")): {
                            "status": "unavailable",
                            "variant_id": None,
                            "reason": "Die KI-Empfehlung ist derzeit nicht verfügbar.",
                        }
                        for chunk in manifest.get("chunks", [])
                        if isinstance(chunk, dict) and str(chunk.get("id", ""))
                    }
                    _apply_recommendation_batch(
                        edit_dir,
                        generation_id,
                        unavailable,
                        status="unavailable",
                    )


def _finalize_selection(videos_dir: Path) -> None:
    with _PIPELINE_LOCK:
        edit_dir = videos_dir / "edit"
        _write_job(edit_dir, "finalizing")
        try:
            with _MANIFEST_LOCK:
                finalize_chunk_variants(edit_dir)
            _write_job(edit_dir, "done")
        except Exception as error:
            _write_job(edit_dir, "failed", _failure_message(error))


def _raise_canvas_error(error: Exception) -> NoReturn:
    if isinstance(error, CanvasAgentUnavailableError):
        raise HTTPException(status_code=503, detail=str(error)) from error
    if isinstance(error, CanvasAgentResponseError):
        raise HTTPException(
            status_code=502, detail="Devin lieferte keine gültige Empfehlung."
        ) from error
    raise error


def _transcribe_with_whisper(path: Path, api_key: str) -> str:
    """Run the blocking Whisper SDK call outside the async request loop."""
    from openai import OpenAI

    with path.open("rb") as audio_handle:
        transcript = OpenAI(api_key=api_key).audio.transcriptions.create(
            model="whisper-1", file=audio_handle, language="de"
        )
    return str(transcript.text)


def create_app(
    videos_dir: Path | str = ".", template_source: Path | str | None = None
) -> FastAPI:
    root = Path(videos_dir).resolve()
    resolved_template = Path(template_source).resolve() if template_source else None
    root.mkdir(parents=True, exist_ok=True)
    app = FastAPI(title="Palantum")

    @app.exception_handler(CanvasPersistenceError)
    async def canvas_persistence_error(
        _request: Request, error: CanvasPersistenceError
    ) -> JSONResponse:
        return JSONResponse({"detail": str(error)}, status_code=500)

    @app.get("/api/state")
    def api_state() -> dict[str, Any]:
        return state_payload(root)

    @app.get("/api/activity")
    def api_activity() -> dict[str, Any]:
        """Return live agent activity without waiting for the render manifest lock."""
        edit_dir = root / "edit"
        job = _job(edit_dir)
        return {
            "job_status": str(job.get("status", "idle")),
            "sessions": _sessions(edit_dir),
        }

    @app.get("/api/canvas")
    def api_get_canvas() -> dict[str, Any]:
        return _load_canvas(root / "edit")

    @app.post("/api/canvas")
    def api_post_canvas(request: CanvasRequest) -> dict[str, Any]:
        edit_dir = root / "edit"
        with _CANVAS_LOCK:
            current = _load_canvas(edit_dir)
            title = request.title if request.title is not None else str(current["title"])
            text = request.text if request.text is not None else str(current["text"])
            if request.text is not None:
                beats = parse_canvas_beats(text)
                if not text.strip():
                    beats.update(request.beats)
            else:
                beats = dict(current.get("beats", {}))
                beats.update(request.beats)
            attached = dict(current.get("attached_videos", {}))
            attached.update(request.attached_videos)
            updated = build_canvas_metadata(
                title=title,
                text=text,
                beats=beats,
                attached_videos=attached,
            )
            _save_canvas(edit_dir, updated)
        return updated

    @app.get("/api/devin/config")
    def api_get_devin_config() -> dict[str, Any]:
        _load_env()
        return {
            "devin_configured": any(
                bool(os.getenv(name, "").strip()) for name in ("DEVIN_PAT", "DEVIN_API_KEY")
            ),
            "openai_configured": bool(os.getenv("OPENAI_API_KEY", "").strip()),
            "agent_backend": "devin",
        }

    @app.post("/api/devin/orchestrate")
    def api_devin_orchestrate(request: DevinOrchestrateRequest) -> CanvasOrchestrationResult:
        if not request.text.strip():
            raise HTTPException(status_code=422, detail="text must not be empty")
        try:
            return orchestrate_canvas(
                request.text,
                cursor_offset=request.cursor_offset,
                accepted_ghost_texts=request.accepted_ghost_texts,
                request_id=request.request_id,
            )
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        except (CanvasAgentUnavailableError, CanvasAgentResponseError) as error:
            _raise_canvas_error(error)

    @app.post("/api/canvas/assist")
    def api_canvas_assist(request: CanvasAssistRequest) -> CanvasAgentSuggestion:
        if not request.text.strip():
            raise HTTPException(status_code=422, detail="text must not be empty")
        try:
            return assist_canvas_agent(
                request.agent_id,
                request.text,
                beat=request.beat,
                cursor_offset=request.cursor_offset,
                accepted_ghost_texts=request.accepted_ghost_texts,
                request_id=request.request_id,
            )
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        except (CanvasAgentUnavailableError, CanvasAgentResponseError) as error:
            _raise_canvas_error(error)

    @app.post("/api/transcribe")
    async def api_transcribe(file: Annotated[UploadFile, File()]) -> JSONResponse:
        openai_key = os.getenv("OPENAI_API_KEY", "").strip()
        if not openai_key:
            raise HTTPException(
                status_code=503, detail="OPENAI_API_KEY ist für Whisper nicht konfiguriert."
            )
        suffix = Path(file.filename or "").suffix
        temporary = root / f".palantum-transcribe-{uuid.uuid4().hex}{suffix}"
        try:
            with temporary.open("wb") as handle:
                while chunk := await file.read(1024 * 1024):
                    handle.write(chunk)
            transcript = await run_in_threadpool(
                _transcribe_with_whisper, temporary, openai_key
            )
            return JSONResponse({"text": transcript})
        except HTTPException:
            raise
        except Exception as error:
            raise HTTPException(
                status_code=502, detail="Whisper-Transkription fehlgeschlagen."
            ) from error
        finally:
            await file.close()
            if temporary.exists():
                temporary.unlink()

    @app.post("/api/upload")
    async def api_upload(
        files: Annotated[list[UploadFile] | None, File()] = None,
        bracket_files: Annotated[list[UploadFile] | None, File(alias="files[]")] = None,
        brief: Annotated[str | None, Form()] = None,
        beat: Annotated[str | None, Form()] = None,
    ) -> JSONResponse:
        uploads = (files or []) + (bracket_files or [])
        if not uploads:
            return JSONResponse({"error": "at least one file is required"}, status_code=400)
        upload_names = [Path(upload.filename or "").name for upload in uploads]
        if any(Path(name).suffix.lower() not in _VIDEO_SUFFIXES for name in upload_names):
            for upload in uploads:
                await upload.close()
            raise HTTPException(
                status_code=415,
                detail="Nur MP4-, MOV-, M4V- und WebM-Videos können verarbeitet werden.",
            )
        sources: list[Path] = []
        uploads_dir = root / "uploads"
        uploads_dir.mkdir(parents=True, exist_ok=True)
        try:
            for upload, name in zip(uploads, upload_names, strict=True):
                suffix = Path(name).suffix.lower()
                destination = uploads_dir / f"{uuid.uuid4().hex}{suffix}"
                sources.append(destination)
                with destination.open("xb") as handle:
                    while chunk := await upload.read(1024 * 1024):
                        handle.write(chunk)
                await upload.close()
        except Exception:
            for upload in uploads:
                await upload.close()
            for source in sources:
                source.unlink(missing_ok=True)
            raise
        try:
            with _JOB_LOCK:
                if str(_job(root / "edit").get("status")) in {
                    "queued",
                    "running",
                    "finalizing",
                }:
                    raise HTTPException(
                        status_code=409, detail="video processing is already running"
                    )
                if beat and sources:
                    edit_dir = root / "edit"
                    with _CANVAS_LOCK:
                        canvas = _load_canvas(edit_dir)
                        attached = dict(canvas.get("attached_videos", {}))
                        attached[beat.upper()] = upload_names[0]
                        canvas = build_canvas_metadata(
                            title=str(canvas["title"]),
                            text=str(canvas["text"]),
                            beats=dict(canvas.get("beats", {})),
                            attached_videos=attached,
                        )
                        _save_canvas(edit_dir, canvas)
                _write_job(root / "edit", "queued")
        except Exception:
            for source in sources:
                source.unlink(missing_ok=True)
            raise
        _EXECUTOR.submit(_process_upload, root, sources, brief, resolved_template)
        return JSONResponse(
            {"accepted": upload_names, "beat": beat, "phase": "working"}
        )

    @app.post("/api/script")
    def api_script(request: ScriptRequest) -> StreamingResponse:
        prompt = request.prompt.strip()
        if not prompt:
            raise HTTPException(status_code=422, detail="prompt must not be empty")
        try:
            chunks, generator = create_script_stream(prompt)
        except (CanvasAgentUnavailableError, CanvasAgentResponseError) as error:
            _raise_canvas_error(error)
        return StreamingResponse(
            chunks,
            media_type="text/plain; charset=utf-8",
            headers={"X-Palantum-Generator": generator},
        )

    @app.post("/api/chunks/{chunk_id}/selection")
    def api_select_chunk(chunk_id: str, request: ChunkSelection) -> dict[str, Any]:
        edit_dir = root / "edit"
        with _JOB_LOCK:
            _assert_selection_mutable(edit_dir)
            with _MANIFEST_LOCK:
                manifest = _read_chunks(edit_dir)
                chunk = next(
                    (item for item in manifest.get("chunks", []) if item.get("id") == chunk_id),
                    None,
                )
                if not isinstance(chunk, dict):
                    raise HTTPException(status_code=404, detail="chunk not found")
                variant = next(
                    (
                        item
                        for item in chunk.get("variants", [])
                        if item.get("id") == request.variant_id
                    ),
                    None,
                )
                if variant is None:
                    raise HTTPException(status_code=404, detail="variant not found")
                chunk["selected"] = request.variant_id
                _write_chunks(edit_dir, manifest)
                complete = _selection_complete(manifest.get("chunks", []))
        return {
            "chunk_id": chunk_id,
            "selected": request.variant_id,
            "selection_complete": complete,
        }

    @app.post("/api/chunks/recommendations/apply")
    def api_apply_recommendations() -> dict[str, Any]:
        edit_dir = root / "edit"
        with _JOB_LOCK:
            _assert_selection_mutable(edit_dir)
            with _MANIFEST_LOCK:
                manifest = _read_chunks(edit_dir)
                chunks = manifest.get("chunks", [])
                if not chunks:
                    raise HTTPException(
                        status_code=409, detail="no chunk recommendations available"
                    )
                selections: list[tuple[dict[str, Any], str]] = []
                for chunk in chunks:
                    if not isinstance(chunk, dict):
                        raise HTTPException(status_code=409, detail="chunk manifest is invalid")
                    recommendation = _recommendation_payload(chunk)
                    if recommendation and recommendation["status"] == "ready":
                        selections.append((chunk, str(recommendation["variant_id"])))
                if not selections:
                    raise HTTPException(status_code=409, detail="no AI recommendations are ready")
                for chunk, variant_id in selections:
                    chunk["selected"] = variant_id
                _write_chunks(edit_dir, manifest)
                complete = _selection_complete(chunks)
        return {"chunks": _chunk_payload(edit_dir), "selection_complete": complete}

    @app.get("/api/chunks/{chunk_id}/variants/{variant_id}/video")
    def api_chunk_video(chunk_id: str, variant_id: str) -> FileResponse:
        edit_dir = root / "edit"
        manifest = _read_chunks(edit_dir)
        chunk = next(
            (item for item in manifest.get("chunks", []) if item.get("id") == chunk_id), None
        )
        variant = next(
            (
                item
                for item in (chunk or {}).get("variants", [])
                if item.get("id") == variant_id
            ),
            None,
        )
        if not isinstance(variant, dict):
            raise HTTPException(status_code=404, detail="variant not found")
        preview = (edit_dir / str(variant.get("preview", ""))).resolve()
        if not preview.is_relative_to(edit_dir.resolve()) or not preview.is_file():
            raise HTTPException(status_code=404, detail="variant preview is not ready")
        return FileResponse(preview, media_type="video/mp4")

    @app.post("/api/finalize")
    def api_finalize() -> JSONResponse:
        edit_dir = root / "edit"
        with _JOB_LOCK:
            if str(_job(edit_dir).get("status")) in {"queued", "running", "finalizing"}:
                raise HTTPException(status_code=409, detail="video processing is already running")
            with _MANIFEST_LOCK:
                manifest = _read_chunks(edit_dir)
                chunks = manifest.get("chunks", [])
                if not _selection_complete(chunks):
                    raise HTTPException(
                        status_code=409, detail="select one variant for every chunk first"
                    )
            _write_job(edit_dir, "queued")
        _EXECUTOR.submit(_finalize_selection, root)
        return JSONResponse({"phase": "working"}, status_code=202)

    @app.get("/api/video")
    def api_video() -> FileResponse:
        path = root / "edit" / "final.mp4"
        if not path.exists():
            raise HTTPException(status_code=404, detail="final video is not ready")
        return FileResponse(path, media_type="video/mp4")

    @app.get("/api/export")
    def api_export() -> FileResponse:
        package = export_project(root / "edit")
        archive = root / "edit" / f"{package.name}.zip"
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as handle:
            for path in package.rglob("*"):
                if path.is_file():
                    handle.write(path, path.relative_to(package.parent))
        return FileResponse(archive, media_type="application/zip", filename=archive.name)

    static = Path(__file__).parent / "static"
    app.mount("/", StaticFiles(directory=static, html=True), name="static")
    return app
