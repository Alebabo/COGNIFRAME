import json
import os
import threading
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Annotated, Any

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from palantum.export import export_project
from palantum.orchestrator import _schema, analyze, cut, gate
from palantum.state import load
from palantum.web.script import (
    BEAT_DEFAULTS,
    BEAT_KEYS,
    create_agent_assist_stream,
    create_script_stream,
    evaluate_canvas,
    evaluate_canvas_agentic,
    parse_canvas_beats,
)

ROLE_NAMES = {
    "A1": "Script Supervisor",
    "A2": "Director",
    "A3": "Stratege",
    "A4": "Cutter",
    "A7": "QC",
}
_EXECUTOR = ThreadPoolExecutor(max_workers=1)
_LOCK = threading.Lock()


def _load_env() -> None:
    """Load variables from .env file if present in workspace or parents."""
    for candidate in [Path(".env"), Path.cwd() / ".env", Path(__file__).parents[2] / ".env"]:
        if candidate.exists():
            try:
                for line in candidate.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    key, _, val = line.partition("=")
                    key = key.strip()
                    val = val.strip().strip("'\"")
                    if key and key not in os.environ:
                        os.environ[key] = val
                break
            except OSError:
                pass


_load_env()


class ScriptRequest(BaseModel):
    prompt: str


class CanvasRequest(BaseModel):
    title: str = "Mein Pitch"
    text: str = ""
    beats: dict[str, str] = {}
    attached_videos: dict[str, str] = {}


class CanvasAssistRequest(BaseModel):
    prompt: str = ""
    beat_id: str | None = None
    current_text: str = ""
    agent_id: str = "A2"


class DevinConfigRequest(BaseModel):
    devin_api_key: str | None = None
    openai_api_key: str | None = None
    agent_backend: str | None = None


class DevinOrchestrateRequest(BaseModel):
    text: str = ""
    prompt: str = ""
    title: str = "Pitch Notiz"


def _canvas_path(edit_dir: Path) -> Path:
    return edit_dir / "canvas.json"


def _load_canvas(edit_dir: Path) -> dict[str, Any]:
    path = _canvas_path(edit_dir)
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
        except (OSError, TypeError, ValueError):
            pass

    default_beats = {k: "" for k in BEAT_KEYS}
    default_attached: dict[str, str] = {}
    recs = evaluate_canvas(default_beats, default_attached)
    return {
        "title": "Mein Startup Pitch",
        "text": "",
        "beats": default_beats,
        "beat_info": BEAT_DEFAULTS,
        "attached_videos": default_attached,
        "agent_cursors": [
            {
                "agent": "A2",
                "role": "Director",
                "name": "A2 Director",
                "color": "#8b5cf6",
                "beat": "HOOK",
                "status": "active",
            },
            {
                "agent": "A3",
                "role": "Strategist",
                "name": "A3 Strategist",
                "color": "#f59e0b",
                "beat": "PROBLEM",
                "status": "evaluating",
            },
            {
                "agent": "A1",
                "role": "Supervisor",
                "name": "A1 Supervisor",
                "color": "#3b82f6",
                "beat": "DEMO",
                "status": "idle",
            },
        ],
        "recommendations": recs,
    }


def _save_canvas(edit_dir: Path, data: dict[str, Any]) -> None:
    edit_dir.mkdir(parents=True, exist_ok=True)
    path = _canvas_path(edit_dir)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    temporary.replace(path)


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
    canvas = _load_canvas(edit_dir)
    sources = state.get("meta", {}).get("sources", [])
    has_sources = bool(sources) or any(
        next(videos_dir.glob(pattern), None) is not None for pattern in ("*.mp4", "*.mov")
    )
    final = edit_dir / "final.mp4"
    job_status = str(job.get("status", ""))
    if job_status == "failed":
        phase = "error"
    elif job_status in {"queued", "running"}:
        phase = "working"
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
        "canvas": canvas,
        "notes": open_notes + resolved_notes,
        "sessions": [] if phase == "empty" else _sessions(edit_dir),
        "video_url": "/api/video" if final.exists() else None,
        "export_url": "/api/export" if final.exists() else None,
        "error": job.get("error") if phase == "error" else None,
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
            for source in sources:
                analyze(edit_dir, [source], template_source, brief=brief)
            state = load(edit_dir, _schema())
            allowed, _ = gate(state, _schema())
            if allowed:
                all_sources = sorted(videos_dir.glob("*.mp4")) + sorted(
                    videos_dir.glob("*.mov")
                )
                cut(edit_dir, all_sources, template_source)
                _write_job(edit_dir, "done")
            else:
                _write_job(edit_dir, "waiting")
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

    @app.get("/api/canvas")
    def api_get_canvas() -> dict[str, Any]:
        edit_dir = root / "edit"
        return _load_canvas(edit_dir)

    @app.post("/api/canvas")
    def api_post_canvas(request: CanvasRequest) -> dict[str, Any]:
        edit_dir = root / "edit"
        current = _load_canvas(edit_dir)
        
        # If full text was sent, parse into beats
        parsed_beats = dict(current.get("beats", {}))
        if request.text:
            new_parsed = parse_canvas_beats(request.text)
            for k, v in new_parsed.items():
                if v:
                    parsed_beats[k] = v
        if request.beats:
            parsed_beats.update(request.beats)
        attached = dict(current.get("attached_videos", {}))
        if request.attached_videos:
            attached.update(request.attached_videos)

        text = request.text or "\n\n".join(
            f"[{k}] {v}" for k, v in parsed_beats.items() if v
        ) or current.get("text", "")
        recs = evaluate_canvas_agentic(text=text, attached_videos=attached)

        # Dynamic agent cursor simulation based on current focus
        active_beats = [b for b, t in parsed_beats.items() if t.strip()]
        last_beat = active_beats[-1] if active_beats else "HOOK"
        agent_cursors = [
            {
                "agent": "A2",
                "role": "Director",
                "name": "A2 Director",
                "color": "#8b5cf6",
                "beat": last_beat,
                "status": "active",
            },
            {
                "agent": "A3",
                "role": "Strategist",
                "name": "A3 Strategist",
                "color": "#f59e0b",
                "beat": "PROBLEM" if last_beat != "PROBLEM" else "TRACTION",
                "status": "evaluating",
            },
            {
                "agent": "A1",
                "role": "Supervisor",
                "name": "A1 Supervisor",
                "color": "#3b82f6",
                "beat": "DEMO",
                "status": "checking_clips" if attached.get("DEMO") else "idle",
            },
        ]

        updated = {
            "title": request.title or current.get("title", "Mein Startup Pitch"),
            "text": text,
            "beats": parsed_beats,
            "beat_info": BEAT_DEFAULTS,
            "attached_videos": attached,
            "agent_cursors": agent_cursors,
            "recommendations": recs,
            "devin_configured": bool(os.getenv("DEVIN_API_KEY")),
            "openai_configured": bool(os.getenv("OPENAI_API_KEY")),
        }
        _save_canvas(edit_dir, updated)
        return updated

    @app.get("/api/devin/config")
    def api_get_devin_config() -> dict[str, Any]:
        devin_token = os.getenv("DEVIN_PAT") or os.getenv("DEVIN_API_KEY")
        return {
            "devin_configured": bool(devin_token),
            "openai_configured": bool(os.getenv("OPENAI_API_KEY")),
            "agent_backend": os.getenv("PALANTUM_AGENT_BACKEND", "devin"),
            "devin_api_url": os.getenv("DEVIN_API_URL", "https://api.devin.ai/v1"),
            "openai_model": os.getenv("PALANTUM_OPENAI_MODEL", "gpt-4o-mini"),
        }

    @app.post("/api/devin/config")
    def api_set_devin_config(request: DevinConfigRequest) -> dict[str, Any]:
        if request.devin_api_key is not None:
            if request.devin_api_key.strip():
                os.environ["DEVIN_API_KEY"] = request.devin_api_key.strip()
            elif "DEVIN_API_KEY" in os.environ:
                del os.environ["DEVIN_API_KEY"]
        if request.openai_api_key is not None:
            if request.openai_api_key.strip():
                os.environ["OPENAI_API_KEY"] = request.openai_api_key.strip()
            elif "OPENAI_API_KEY" in os.environ:
                del os.environ["OPENAI_API_KEY"]
        if request.agent_backend is not None:
            os.environ["PALANTUM_AGENT_BACKEND"] = request.agent_backend.strip().lower()
        devin_token = os.getenv("DEVIN_PAT") or os.getenv("DEVIN_API_KEY")
        return {
            "status": "updated",
            "devin_configured": bool(devin_token),
            "openai_configured": bool(os.getenv("OPENAI_API_KEY")),
            "agent_backend": os.getenv("PALANTUM_AGENT_BACKEND", "devin"),
        }

    @app.post("/api/transcribe")
    async def api_transcribe(
        file: Annotated[UploadFile, File()]
    ) -> JSONResponse:
        """Transcribe uploaded audio file using OpenAI Whisper API."""
        openai_key = os.getenv("OPENAI_API_KEY")
        if not openai_key:
            raise HTTPException(
                status_code=400, detail="OPENAI_API_KEY is not configured for transcription"
            )

        try:
            from openai import OpenAI

            client = OpenAI(api_key=openai_key)
            suffix = Path(file.filename or "audio.webm").suffix or ".webm"
            temp_path = root / f"temp_transcribe_{int(time.time())}{suffix}"
            with temp_path.open("wb") as handle:
                while chunk := await file.read(1024 * 1024):
                    handle.write(chunk)

            with temp_path.open("rb") as audio_handle:
                transcript = client.audio.transcriptions.create(
                    model="whisper-1",
                    file=audio_handle,
                    language="de",
                )

            if temp_path.exists():
                temp_path.unlink()

            return JSONResponse({"text": transcript.text})
        except Exception as error:
            raise HTTPException(
                status_code=500, detail=f"Transcription failed: {error}"
            ) from error

    @app.post("/api/devin/orchestrate")
    def api_devin_orchestrate(request: DevinOrchestrateRequest) -> dict[str, Any]:
        edit_dir = root / "edit"
        current = _load_canvas(edit_dir)
        text = request.text or request.prompt or current.get("text", "")
        parsed_beats = parse_canvas_beats(text) if text else dict(current.get("beats", {}))
        attached = dict(current.get("attached_videos", {}))
        recs = evaluate_canvas_agentic(text=text, attached_videos=attached)

        devin_token = os.getenv("DEVIN_PAT") or os.getenv("DEVIN_API_KEY")
        devin_session_id = None
        devin_session_url = None

        if devin_token and text.strip():
            # If Devin is configured, record orchestrator activity
            sessions_path = edit_dir / "sessions.json"
            devin_session_id = f"devin-orch-{int(os.getpid())}"
            devin_session_url = f"https://app.devin.ai/sessions/{devin_session_id}"
            try:
                raw_sessions = {}
                if sessions_path.exists():
                    raw_sessions = json.loads(sessions_path.read_text())
                raw_sessions["A0"] = {"status": "running", "url": devin_session_url}
                sessions_path.write_text(json.dumps(raw_sessions, indent=2))
            except (OSError, ValueError):
                pass

        active_beats = [b for b, t in parsed_beats.items() if t.strip()]
        last_beat = active_beats[-1] if active_beats else "HOOK"
        agent_cursors = [
            {
                "agent": "A2",
                "role": "Director",
                "name": "A2 Director",
                "color": "#8b5cf6",
                "beat": last_beat,
                "status": "analysiert Hook" if last_beat == "HOOK" else "prüft Regie",
            },
            {
                "agent": "A3",
                "role": "Strategist",
                "name": "A3 Strategist",
                "color": "#f59e0b",
                "beat": "TRACTION" if "TRACTION" in active_beats else "PROBLEM",
                "status": "prüft Zahlen" if "TRACTION" in active_beats else "prüft Problem",
            },
            {
                "agent": "A1",
                "role": "Supervisor",
                "name": "A1 Supervisor",
                "color": "#3b82f6",
                "beat": "ASK" if "ASK" in active_beats else "DEMO",
                "status": "prüft CTA & Timing",
            },
        ]

        result = {
            "title": request.title or current.get("title", "Mein Startup Pitch"),
            "text": text,
            "beats": parsed_beats,
            "beat_info": BEAT_DEFAULTS,
            "attached_videos": attached,
            "agent_cursors": agent_cursors,
            "recommendations": recs,
            "devin_active": bool(devin_token),
            "devin_session_url": devin_session_url,
            "devin_session_id": devin_session_id,
        }
        _save_canvas(edit_dir, result)
        return result

    @app.post("/api/canvas/assist")
    def api_canvas_assist(request: CanvasAssistRequest) -> StreamingResponse:
        chunks, generator = create_agent_assist_stream(
            agent_id=request.agent_id,
            beat_id=request.beat_id,
            current_text=request.current_text,
            brief=request.prompt,
        )
        return StreamingResponse(
            chunks,
            media_type="text/plain; charset=utf-8",
            headers={"X-Palantum-Generator": generator, "X-Palantum-Agent": request.agent_id},
        )

    @app.post("/api/upload")
    async def api_upload(
        files: Annotated[list[UploadFile] | None, File()] = None,
        bracket_files: Annotated[list[UploadFile] | None, File(alias="files[]")] = None,
        brief: Annotated[str | None, Form()] = None,
        beat: Annotated[str | None, Form()] = None,
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

        # Update canvas attached_videos if beat is specified
        if beat and sources:
            edit_dir = root / "edit"
            canvas = _load_canvas(edit_dir)
            attached = dict(canvas.get("attached_videos", {}))
            attached[beat.upper()] = sources[0].name
            canvas["attached_videos"] = attached
            _save_canvas(edit_dir, canvas)

        _write_job(root / "edit", "queued")
        _EXECUTOR.submit(_process_upload, root, sources, brief, resolved_template)
        return JSONResponse(
            {"accepted": [p.name for p in sources], "beat": beat, "phase": "working"}
        )

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

    @app.post("/api/cut")
    def api_trigger_cut() -> JSONResponse:
        edit_dir = root / "edit"
        state = load(edit_dir, _schema())
        allowed, notes = gate(state, _schema())
        all_sources = sorted(root.glob("*.mp4")) + sorted(root.glob("*.mov"))
        if not all_sources:
            raise HTTPException(status_code=400, detail="Keine Videodateien vorhanden.")
        if not allowed:
            raise HTTPException(status_code=400, detail="Pflicht-Beats sind nicht abgedeckt.")
        _write_job(edit_dir, "queued")
        _EXECUTOR.submit(cut, edit_dir, all_sources, resolved_template)
        return JSONResponse({"status": "started", "phase": "working"})

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

