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
_CACHED_ORG_ID: str | None = None


def get_devin_token() -> str:
    """Return the active Devin API token (from DEVIN_PAT or DEVIN_API_KEY)."""
    token = os.getenv("DEVIN_PAT") or os.getenv("DEVIN_API_KEY")
    if not token:
        raise ValueError("Missing DEVIN_PAT or DEVIN_API_KEY in environment or .env")
    return token.strip()


def get_devin_org_id(token: str) -> str | None:
    """Retrieve organization ID for Devin v3 API."""
    global _CACHED_ORG_ID
    if _CACHED_ORG_ID:
        return _CACHED_ORG_ID
    
    explicit_org = os.getenv("DEVIN_ORG_ID")
    if explicit_org:
        _CACHED_ORG_ID = explicit_org.strip()
        return _CACHED_ORG_ID

    if not token.startswith("cog_"):
        return None

    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    try:
        res = requests.get("https://api.devin.ai/v3/self", headers=headers, timeout=10)
        if res.status_code == 200:
            data = res.json()
            org_id = data.get("org_id")
            if org_id:
                _CACHED_ORG_ID = str(org_id)
                return _CACHED_ORG_ID
    except Exception:
        pass
    return None


def call(
    role_id: str,
    prompt: str,
    context: dict[str, Any],
    schema: dict[str, Any],
    attempt: int = 0,
    on_session_created: SessionCreatedCallback | None = None,
) -> DevinCallResult:
    """Run one structured Devin session (v3 with v1 fallback) and return its output and identity."""
    token = get_devin_token()
    headers = {
        "Authorization": f"Bearer {token}",
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

    org_id = get_devin_org_id(token)
    if org_id:
        create_url = f"https://api.devin.ai/v3/organizations/{org_id}/sessions"
        session_base_url = f"https://api.devin.ai/v3/organizations/{org_id}/sessions"
    else:
        base_url = os.getenv("DEVIN_API_URL", "https://api.devin.ai/v1").rstrip("/")
        create_url = f"{base_url}/sessions"
        session_base_url = f"{base_url}/sessions"

    created = requests.post(create_url, headers=headers, json=payload, timeout=60)
    created.raise_for_status()
    session = created.json()
    session_id = str(session["session_id"])
    session_url = str(session.get("url") or f"https://app.devin.ai/sessions/{session_id}")
    if on_session_created is not None:
        on_session_created(session_id, session_url)

    timeout_s = float(os.getenv("PALANTUM_DEVIN_TIMEOUT_S", "600"))
    poll_interval_s = float(os.getenv("PALANTUM_DEVIN_POLL_INTERVAL_S", "10"))
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        response = requests.get(f"{session_base_url}/{session_id}", headers=headers, timeout=30)
        response.raise_for_status()
        current = response.json()
        status = current.get("status_enum") or current.get("status")
        if status == "finished":
            output = current.get("structured_output")
            if not isinstance(output, dict):
                raise ValueError(f"Devin role {role_id} returned no structured output")
            return DevinCallResult(output=output, session_id=session_id, url=session_url)
        if status in {"blocked", "expired", "stopped"}:
            raise RuntimeError(f"Devin role {role_id} ended {status}: {current}")
        time.sleep(poll_interval_s)

    terminated = requests.delete(f"{session_base_url}/{session_id}", headers=headers, timeout=30)
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
