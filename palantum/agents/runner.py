from __future__ import annotations

import json
import os
import tempfile
import threading
from pathlib import Path
from typing import Any, cast

from jsonschema import Draft202012Validator

_SESSION_STATE_LOCK = threading.Lock()


def _schema(role_id: str) -> dict[str, Any]:
    path = Path(__file__).resolve().parents[1] / "schemas" / "roles.json"
    roles = json.loads(path.read_text())
    return cast(dict[str, Any], roles[role_id])


def _validate(role_id: str, output: dict[str, Any]) -> None:
    errors = sorted(
        Draft202012Validator(_schema(role_id)).iter_errors(output),
        key=lambda error: list(error.path),
    )
    if errors:
        raise ValueError(f"{role_id} response validation failed: {errors[0].message}")


def _record_session(
    context: dict[str, Any], role_id: str, status: str, url: str | None = None
) -> None:
    path_value = context.get("_session_state_path")
    if not path_value:
        return
    path = Path(str(path_value))
    with _SESSION_STATE_LOCK:
        sessions: dict[str, dict[str, Any]] = {}
        if path.exists():
            try:
                loaded = json.loads(path.read_text())
                if isinstance(loaded, dict):
                    sessions = loaded
            except (OSError, TypeError, ValueError):
                sessions = {}
        session_key = str(context.get("_session_key", role_id))
        entry: dict[str, Any] = {"status": status, "url": url}
        if session_key != role_id:
            entry["role"] = role_id
        sessions[session_key] = entry
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=path.parent,
                prefix=f".{path.name}.",
                suffix=".tmp",
                delete=False,
            ) as handle:
                json.dump(sessions, handle, indent=2, ensure_ascii=False)
                handle.write("\n")
                temporary_path = Path(handle.name)
            os.replace(temporary_path, path)
        finally:
            if temporary_path is not None and temporary_path.exists():
                temporary_path.unlink()


def run_role(
    role_id: str, prompt: str, context: dict[str, Any], schema: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Run one role with one backend, validating and retrying once."""
    selected = schema or _schema(role_id)
    backend = os.getenv("PALANTUM_AGENT_BACKEND", "devin").lower()
    if backend not in {"devin", "openai"}:
        raise ValueError(f"unsupported PALANTUM_AGENT_BACKEND={backend}")

    last_error = ""
    session_url: str | None = None
    _record_session(context, role_id, "running")
    for attempt in range(2):
        attempt_context = dict(context)
        if last_error:
            attempt_context["_validation_error"] = last_error
        try:
            if backend == "devin":
                from palantum.agents.backends.devin import call as call_devin

                def session_created(_session_id: str, url: str) -> None:
                    nonlocal session_url
                    session_url = url
                    _record_session(context, role_id, "running", url)

                result = call_devin(
                    role_id,
                    prompt,
                    attempt_context,
                    selected,
                    attempt=attempt,
                    on_session_created=session_created,
                )
                output = result.output
                session_url = result.url
            else:
                from palantum.agents.backends.openai import call as call_openai

                output = call_openai(role_id, prompt, attempt_context, selected, attempt=attempt)
        except Exception:
            _record_session(context, role_id, "failed", session_url)
            raise
        try:
            _validate(role_id, output)
            _record_session(context, role_id, "done", session_url)
            return output
        except ValueError as error:
            last_error = str(error)
    _record_session(context, role_id, "failed", session_url)
    raise ValueError(last_error)
