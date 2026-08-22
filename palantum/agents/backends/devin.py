from __future__ import annotations

import json
import os
import time
from typing import Any

import requests

LAST_SESSION_URL: str | None = None


def call(
    role_id: str, prompt: str, context: dict[str, Any], schema: dict[str, Any], attempt: int = 0
) -> dict[str, Any]:
    """Run a structured Devin session; the endpoint is intentionally isolated."""
    global LAST_SESSION_URL
    base_url = os.getenv("DEVIN_API_URL", "https://api.devin.ai/v1").rstrip("/")
    headers = {
        "Authorization": f"Bearer {os.environ['DEVIN_API_KEY']}",
        "Content-Type": "application/json",
    }
    run_number = context.get("run_number", 0)
    payload = {
        "prompt": f"{prompt}\n\nINPUT JSON:\n{json.dumps(context, ensure_ascii=False)}",
        "structured_output_schema": schema,
        "tags": [f"run-{run_number}", role_id],
        "max_acu_limit": int(os.getenv(f"PALANTUM_{role_id}_MAX_ACU", "5")),
    }
    created = requests.post(f"{base_url}/sessions", headers=headers, json=payload, timeout=60)
    created.raise_for_status()
    session = created.json()
    session_id = str(session["session_id"])
    LAST_SESSION_URL = f"{base_url}/sessions/{session_id}"
    timeout_s = float(os.getenv("PALANTUM_DEVIN_TIMEOUT_S", "600"))
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        response = requests.get(f"{base_url}/sessions/{session_id}", headers=headers, timeout=30)
        response.raise_for_status()
        current = response.json()
        status = current.get("status")
        if status in {"completed", "failed", "blocked", "stopped"}:
            if status != "completed":
                raise RuntimeError(f"Devin role {role_id} ended {status}: {current}")
            output = current.get("structured_output", current.get("output"))
            if not isinstance(output, dict):
                raise ValueError(f"Devin role {role_id} returned no structured output")
            return output
        time.sleep(2)
    requests.post(f"{base_url}/sessions/{session_id}/terminate", headers=headers, timeout=30)
    if attempt == 0:
        return call(role_id, prompt, context, schema, attempt=1)
    raise TimeoutError(f"Devin role {role_id} exceeded {timeout_s:.0f}s")
