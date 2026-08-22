from __future__ import annotations

import json
import threading
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Annotated, Any

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from palantum.export import export_project
from palantum.orchestrator import _schema, analyze, cut, gate
from palantum.state import load

ROLE_NAMES = {
    "A1": "Script Supervisor",
    "A2": "Director",
    "A3": "Stratege",
    "A4": "Cutter",
    "A7": "QC",
}
_EXECUTOR = ThreadPoolExecutor(max_workers=1)
_LOCK = threading.Lock()


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
    sources = state.get("meta", {}).get("sources", [])
    has_sources = bool(sources) or any(
        next(videos_dir.glob(pattern), None) is not None for pattern in ("*.mp4", "*.mov")
    )
    final = edit_dir / "final.mp4"
    if not has_sources:
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
    }


def _process_upload(videos_dir: Path, sources: list[Path], brief: str | None) -> None:
    del brief
    with _LOCK:
        edit_dir = videos_dir / "edit"
        for source in sources:
            analyze(edit_dir, [source])
        state = load(edit_dir, _schema())
        allowed, _ = gate(state, _schema())
        if allowed:
            all_sources = sorted(videos_dir.glob("*.mp4")) + sorted(videos_dir.glob("*.mov"))
            cut(edit_dir, all_sources)


def create_app(videos_dir: Path | str = ".") -> FastAPI:
    root = Path(videos_dir).resolve()
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
        _EXECUTOR.submit(_process_upload, root, sources, brief)
        return JSONResponse({"accepted": [path.name for path in sources], "phase": "working"})

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
