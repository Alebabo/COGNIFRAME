from __future__ import annotations

import argparse
import json
from pathlib import Path

from palantum.engine.videouse import doctor
from palantum.export import export_project
from palantum.orchestrator import _schema, analyze, cut, gate
from palantum.state import load

BEATS = ("HOOK", "PROBLEM", "SOLUTION", "DEMO", "TRACTION", "TEAM", "ASK")
ANSI_BOLD = "\033[1m"
ANSI_RESET = "\033[0m"


def _confirm_rough_cut() -> bool:
    try:
        answer = input("Required beats are missing. Start a rough cut anyway? [y/N] ")
    except (EOFError, OSError):
        return False
    return answer.strip().lower() in {"y", "yes", "j", "ja"}


def _cut_sources(videos_dir: Path, state: dict[str, object]) -> list[Path]:
    meta = state.get("meta")
    if isinstance(meta, dict):
        recorded = meta.get("sources")
        if isinstance(recorded, list) and recorded:
            return [
                Path(value).resolve()
                for value in recorded
                if isinstance(value, str) and Path(value).suffix.lower() in {".mp4", ".mov"}
            ]
    return sorted(videos_dir.glob("*.mp4")) + sorted(videos_dir.glob("*.mov"))


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
    ingest.add_argument("--template-source", type=Path)
    commands.add_parser("status")
    cut_command = commands.add_parser("cut")
    cut_command.add_argument("--template-source", type=Path)
    cut_command.add_argument(
        "--force",
        action="store_true",
        help="start a rough cut even when required beats are missing",
    )
    cut_command.add_argument(
        "--resume",
        action="store_true",
        help="reuse validated Cutter and Motion role outputs from a failed render",
    )
    commands.add_parser("export")
    doctor_command = commands.add_parser("doctor")
    doctor_command.add_argument("--template-source", type=Path)
    doctor_command.add_argument("--json", action="store_true")
    serve = commands.add_parser("serve")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", default=8000, type=int)
    serve.add_argument("--template-source", type=Path)
    args = parser.parse_args()
    videos_dir = args.videos_dir.resolve()
    edit_dir = videos_dir / "edit"
    if args.command == "doctor":
        report = doctor(args.template_source)
        if args.json:
            print(json.dumps(report.as_dict(), indent=2, ensure_ascii=False))
        else:
            for check in report.checks:
                marker = "ok" if check.ok else ("FAIL" if check.required else "optional")
                suffix = f" - {check.detail}" if check.detail else ""
                print(f"[{marker}] {check.name}: {check.value}{suffix}")
        raise SystemExit(0 if report.ok else 1)
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
            state = analyze(edit_dir, known, args.template_source)
            print_state(state)
        return
    if args.command == "export":
        package = export_project(edit_dir)
        print(package)
        return
    if args.command == "serve":
        import uvicorn

        from palantum.web import create_app

        uvicorn.run(
            create_app(videos_dir, args.template_source), host=args.host, port=args.port
        )
        return
    state = load(edit_dir, schema)
    sources = _cut_sources(videos_dir, state)
    allowed, notes = gate(state, schema)
    if not allowed:
        print_state(state)
        if not args.force and not _confirm_rough_cut():
            raise SystemExit(2)
        print("\nStarting a rough cut with the available footage.")
    edl, qc = cut(
        edit_dir,
        sources,
        args.template_source,
        force=not allowed,
        resume=args.resume,
    )
    print(f"Rendered {edit_dir / 'final.mp4'}")
    print(json.dumps(qc, indent=2, ensure_ascii=False))
