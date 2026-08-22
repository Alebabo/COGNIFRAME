from __future__ import annotations

import json
import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import requests


@dataclass(frozen=True)
class DevinCallResult:
    output: dict[str, Any]
    session_id: str
    url: str


SessionCreatedCallback = Callable[[str, str], None]


def call(
    role_id: str,
    prompt: str,
    context: dict[str, Any],
    schema: dict[str, Any],
    attempt: int = 0,
    on_session_created: SessionCreatedCallback | None = None,
) -> DevinCallResult:
    """Run one structured Devin v1 session and return its output and identity."""
    base_url = os.getenv("DEVIN_API_URL", "https://api.devin.ai/v1").rstrip("/")
    headers = {
        "Authorization": f"Bearer {os.environ['DEVIN_API_KEY']}",
        "Content-Type": "application/json",
    }
    run_number = context.get("run_number", 0)
    payload: dict[str, Any] = {
        "prompt": f"{prompt}\n\nINPUT JSON:\n{json.dumps(context, ensure_ascii=False)}",
        "structured_output_schema": schema,
        "tags": [f"run-{run_number}", role_id],
        "title": f"Palantum {role_id} · run {run_number}",
        "max_acu_limit": int(os.getenv(f"PALANTUM_{role_id}_MAX_ACU", "5")),
    }
    snapshot_id = os.getenv("DEVIN_SNAPSHOT_ID")
    if snapshot_id:
        payload["snapshot_id"] = snapshot_id

    created = requests.post(f"{base_url}/sessions", headers=headers, json=payload, timeout=60)
    created.raise_for_status()
    session = created.json()
    session_id = str(session["session_id"])
    session_url = str(session["url"])
    if on_session_created is not None:
        on_session_created(session_id, session_url)

    timeout_s = float(os.getenv("PALANTUM_DEVIN_TIMEOUT_S", "600"))
    poll_interval_s = float(os.getenv("PALANTUM_DEVIN_POLL_INTERVAL_S", "10"))
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        response = requests.get(f"{base_url}/sessions/{session_id}", headers=headers, timeout=30)
        response.raise_for_status()
        current = response.json()
        status = current.get("status_enum") or current.get("status")
        if status == "finished":
            output = current.get("structured_output")
            if not isinstance(output, dict):
                raise ValueError(f"Devin role {role_id} returned no structured output")
            return DevinCallResult(output=output, session_id=session_id, url=session_url)
        if status in {"blocked", "expired"}:
            raise RuntimeError(f"Devin role {role_id} ended {status}: {current}")
        time.sleep(poll_interval_s)

    terminated = requests.delete(f"{base_url}/sessions/{session_id}", headers=headers, timeout=30)
    terminated.raise_for_status()
    if attempt == 0:
        return call(
            role_id,
            prompt,
            context,
            schema,
            attempt=1,
            on_session_created=on_session_created,
        )
    raise TimeoutError(f"Devin role {role_id} exceeded {timeout_s:.0f}s")
