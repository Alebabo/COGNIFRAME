from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, cast

from jsonschema import Draft202012Validator


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
    sessions: dict[str, dict[str, Any]] = {}
    if path.exists():
        try:
            loaded = json.loads(path.read_text())
            if isinstance(loaded, dict):
                sessions = loaded
        except (OSError, TypeError, ValueError):
            sessions = {}
    sessions[role_id] = {"status": status, "url": url}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(sessions, indent=2, ensure_ascii=False) + "\n")


def run_role(
    role_id: str, prompt: str, context: dict[str, Any], schema: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Run one role with one backend, validating and retrying once."""
    selected = schema or _schema(role_id)
    backend = os.getenv("PALANTUM_AGENT_BACKEND", "openai").lower()
    if backend == "devin":
        from palantum.agents.backends.devin import call
    elif backend == "openai":
        from palantum.agents.backends.openai import call
    else:
        raise ValueError(f"unsupported PALANTUM_AGENT_BACKEND={backend}")
    last_error = ""
    _record_session(context, role_id, "running")
    for attempt in range(2):
        attempt_context = dict(context)
        if last_error:
            attempt_context["_validation_error"] = last_error
        try:
            output = call(role_id, prompt, attempt_context, selected, attempt=attempt)
        except Exception:
            _record_session(context, role_id, "failed")
            raise
        try:
            _validate(role_id, output)
            url: str | None = None
            if backend == "devin":
                from palantum.agents.backends import devin

                url = devin.LAST_SESSION_URL
            _record_session(context, role_id, "done", url)
            return output
        except ValueError as error:
            last_error = str(error)
    _record_session(context, role_id, "failed")
    raise ValueError(last_error)
