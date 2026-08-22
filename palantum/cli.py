from __future__ import annotations

import argparse
import json
from pathlib import Path

from palantum.export import export_project
from palantum.orchestrator import _schema, analyze, cut, gate
from palantum.state import load

BEATS = ("HOOK", "PROBLEM", "SOLUTION", "DEMO", "TRACTION", "TEAM", "ASK")
ANSI_BOLD = "\033[1m"
ANSI_RESET = "\033[0m"


def _dots(state: dict[str, object]) -> str:
    raw_beats = state["beats"]
    if not isinstance(raw_beats, list):
        return " ".join("○" for _ in BEATS)
    beats = {str(item["id"]): str(item["status"]) for item in raw_beats if isinstance(item, dict)}
    return " ".join(
        {"covered": "●", "weak": "◐", "missing": "○"}[beats.get(beat, "missing")] for beat in BEATS
    )


def print_state(state: dict[str, object]) -> None:
    print(_dots(state))
    notes = state.get("director_notes", [])
    if not isinstance(notes, list):
        return
    for note in notes:
        if not isinstance(note, dict):
            continue
        shot = note.get("shot")
        if not isinstance(shot, dict):
            continue
        print(f"\n{note.get('beat')} · {note.get('impact')} · {note.get('why')}")
        print(f"{ANSI_BOLD}{shot.get('line')}{ANSI_RESET}")
        print(f"  {shot.get('framing')} · {shot.get('duration_s')}s · {shot.get('delivery')}")


def main() -> None:
    parser = argparse.ArgumentParser(prog="palantum")
    parser.add_argument("--videos-dir", type=Path, default=Path("."))
    commands = parser.add_subparsers(dest="command", required=True)
    ingest = commands.add_parser("ingest")
    ingest.add_argument("files", nargs="+", type=Path)
    commands.add_parser("status")
    commands.add_parser("cut")
    commands.add_parser("export")
    serve = commands.add_parser("serve")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", default=8000, type=int)
    args = parser.parse_args()
    videos_dir = args.videos_dir.resolve()
    edit_dir = videos_dir / "edit"
    schema = _schema()
    if args.command == "status":
        print_state(load(edit_dir, schema))
        return
    if args.command == "ingest":
        sources = [
            path.resolve() if path.is_absolute() else (videos_dir / path).resolve()
            for path in args.files
        ]
        known: list[Path] = []
        for source in sources:
            known.append(source)
            print(f"\nIngesting {source.name}")
            state = analyze(edit_dir, known)
            print_state(state)
        return
    if args.command == "export":
        package = export_project(edit_dir)
        print(package)
        return
    if args.command == "serve":
        import uvicorn

        from palantum.web import create_app

        uvicorn.run(create_app(videos_dir), host=args.host, port=args.port)
        return
    sources = sorted(videos_dir.glob("*.mp4")) + sorted(videos_dir.glob("*.mov"))
    state = load(edit_dir, schema)
    allowed, notes = gate(state, schema)
    if not allowed:
        print_state(state)
        raise SystemExit(2)
    edl, qc = cut(edit_dir, sources)
    print(f"Rendered {edit_dir / 'final.mp4'}")
    print(json.dumps(qc, indent=2, ensure_ascii=False))
