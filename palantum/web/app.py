from __future__ import annotations

import json
import threading
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Annotated, Any

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from palantum.export import export_project
from palantum.orchestrator import (
    _schema,
    analyze,
    build_chunk_variants,
    finalize_chunk_variants,
)
from palantum.state import load
from palantum.web.script import create_script_stream

ROLE_NAMES = {
    "A1": "Script Supervisor",
    "A2": "Director",
    "A3": "Stratege",
    "A4": "Cutter",
    "A7": "QC",
}
_EXECUTOR = ThreadPoolExecutor(max_workers=1)
_LOCK = threading.Lock()


class ScriptRequest(BaseModel):
    prompt: str


class ChunkSelection(BaseModel):
    variant_id: str


def _job_path(edit_dir: Path) -> Path:
    return edit_dir / "job.json"


def _write_job(edit_dir: Path, status: str, error: str | None = None) -> None:
    edit_dir.mkdir(parents=True, exist_ok=True)
    path = _job_path(edit_dir)
    temporary = path.with_suffix(".tmp")
    payload = {"status": status, "error": error}
    temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    temporary.replace(path)


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
    path = _chunks_path(edit_dir)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    temporary.replace(path)


def _chunk_payload(edit_dir: Path) -> list[dict[str, Any]]:
    manifest = _read_chunks(edit_dir)
    result: list[dict[str, Any]] = []
    for chunk in manifest.get("chunks", []):
        if not isinstance(chunk, dict):
            continue
        chunk_id = str(chunk.get("id", ""))
        variants = []
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
                "variants": variants,
            }
        )
    return sorted(result, key=lambda item: item["order"])


def _failure_message(error: Exception) -> str:
    missing = str(error).strip("'") if isinstance(error, KeyError) else ""
    if missing in {"OPENAI_API_KEY", "DEVIN_API_KEY"}:
        return f"{missing} fehlt. Bitte den Schlüssel konfigurieren und erneut versuchen."
    detail = str(error).strip()
    return f"{type(error).__name__}: {detail}" if detail else type(error).__name__


def _sessions(edit_dir: Path) -> list[dict[str, Any]]:
    path = edit_dir / "sessions.json"
    raw: dict[str, Any] = {}
    if path.exists():
        try:
            loaded = json.loads(path.read_text())
            if isinstance(loaded, dict):
                raw = loaded
        except (OSError, TypeError, ValueError):
            pass
    return [
        {
            "role": ROLE_NAMES[role],
            "status": str(raw.get(role, {}).get("status", "idle")),
            "url": raw.get(role, {}).get("url"),
        }
        for role in ROLE_NAMES
    ]


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
    sources = state.get("meta", {}).get("sources", [])
    has_sources = bool(sources) or any(
        next(videos_dir.glob(pattern), None) is not None for pattern in ("*.mp4", "*.mov")
    )
    final = edit_dir / "final.mp4"
    job_status = str(job.get("status", ""))
    chunks = _chunk_payload(edit_dir)
    if job_status == "failed":
        phase = "error"
    elif job_status in {"queued", "running", "finalizing"}:
        phase = "working"
    elif job_status == "review":
        phase = "review"
    elif not has_sources:
        phase = "empty"
    elif final.exists():
        phase = "done"
    else:
        phase = "working"
    open_notes = [
        _note_payload(note) for note in state.get("director_notes", []) if isinstance(note, dict)
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
    return {
        "phase": phase,
        "coverage": {"score": state.get("coverage_score", 0.0), "beats": beats},
        "notes": open_notes + resolved_notes,
        "sessions": [] if phase == "empty" else _sessions(edit_dir),
        "video_url": "/api/video" if final.exists() else None,
        "export_url": "/api/export" if final.exists() else None,
        "error": job.get("error") if phase == "error" else None,
        "chunks": chunks,
        "selection_complete": bool(chunks)
        and all(chunk.get("selected") for chunk in chunks),
    }


def _process_upload(
    videos_dir: Path,
    sources: list[Path],
    brief: str | None,
    template_source: Path | None = None,
) -> None:
    with _LOCK:
        edit_dir = videos_dir / "edit"
        _write_job(edit_dir, "running")
        try:
            analyze(edit_dir, sources, template_source, brief=brief)
            state = load(edit_dir, _schema())
            all_sources = [
                Path(str(value)).resolve()
                for value in state.get("meta", {}).get("sources", [])
            ]
            build_chunk_variants(edit_dir, all_sources, template_source)
            _write_job(edit_dir, "review")
        except Exception as error:
            _write_job(edit_dir, "failed", _failure_message(error))


def _finalize_selection(videos_dir: Path) -> None:
    with _LOCK:
        edit_dir = videos_dir / "edit"
        _write_job(edit_dir, "finalizing")
        try:
            finalize_chunk_variants(edit_dir)
            _write_job(edit_dir, "done")
        except Exception as error:
            _write_job(edit_dir, "failed", _failure_message(error))


def create_app(
    videos_dir: Path | str = ".", template_source: Path | str | None = None
) -> FastAPI:
    root = Path(videos_dir).resolve()
    resolved_template = Path(template_source).resolve() if template_source else None
    root.mkdir(parents=True, exist_ok=True)
    app = FastAPI(title="Palantum")

    @app.get("/api/state")
    def api_state() -> dict[str, Any]:
        return state_payload(root)

    @app.post("/api/upload")
    async def api_upload(
        files: Annotated[list[UploadFile] | None, File()] = None,
        bracket_files: Annotated[list[UploadFile] | None, File(alias="files[]")] = None,
        brief: Annotated[str | None, Form()] = None,
    ) -> JSONResponse:
        files = (files or []) + (bracket_files or [])
        if not files:
            return JSONResponse({"error": "at least one file is required"}, status_code=400)
        sources: list[Path] = []
        for upload in files:
            name = Path(upload.filename or "upload.mp4").name
            destination = root / name
            with destination.open("wb") as handle:
                while chunk := await upload.read(1024 * 1024):
                    handle.write(chunk)
            sources.append(destination)
            await upload.close()
        _write_job(root / "edit", "queued")
        _EXECUTOR.submit(_process_upload, root, sources, brief, resolved_template)
        return JSONResponse({"accepted": [path.name for path in sources], "phase": "working"})

    @app.post("/api/script")
    def api_script(request: ScriptRequest) -> StreamingResponse:
        prompt = request.prompt.strip()
        if not prompt:
            raise HTTPException(status_code=422, detail="prompt must not be empty")
        chunks, generator = create_script_stream(prompt)
        return StreamingResponse(
            chunks,
            media_type="text/plain; charset=utf-8",
            headers={"X-Palantum-Generator": generator},
        )

    @app.post("/api/chunks/{chunk_id}/selection")
    def api_select_chunk(chunk_id: str, request: ChunkSelection) -> dict[str, Any]:
        edit_dir = root / "edit"
        with _LOCK:
            manifest = _read_chunks(edit_dir)
            chunk = next(
                (item for item in manifest.get("chunks", []) if item.get("id") == chunk_id),
                None,
            )
            if chunk is None:
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
        return {
            "chunk_id": chunk_id,
            "selected": request.variant_id,
            "selection_complete": all(
                item.get("selected") for item in manifest.get("chunks", [])
            ),
        }

    @app.get("/api/chunks/{chunk_id}/variants/{variant_id}/video")
    def api_chunk_video(chunk_id: str, variant_id: str) -> FileResponse:
        edit_dir = root / "edit"
        manifest = _read_chunks(edit_dir)
        chunk = next(
            (item for item in manifest.get("chunks", []) if item.get("id") == chunk_id),
            None,
        )
        variant = next(
            (
                item
                for item in (chunk or {}).get("variants", [])
                if item.get("id") == variant_id
            ),
            None,
        )
        if variant is None:
            raise HTTPException(status_code=404, detail="variant not found")
        preview = edit_dir / str(variant.get("preview", ""))
        if not preview.is_file():
            raise HTTPException(status_code=404, detail="variant preview is not ready")
        return FileResponse(preview, media_type="video/mp4")

    @app.post("/api/finalize")
    def api_finalize() -> JSONResponse:
        edit_dir = root / "edit"
        manifest = _read_chunks(edit_dir)
        chunks = manifest.get("chunks", [])
        if not chunks or not all(item.get("selected") for item in chunks):
            raise HTTPException(
                status_code=409, detail="select one variant for every chunk first"
            )
        with _LOCK:
            if str(_job(edit_dir).get("status")) in {"queued", "finalizing"}:
                raise HTTPException(status_code=409, detail="final render already running")
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
