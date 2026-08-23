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
_CACHED_ORG: tuple[str, str] | None = None
DEVIN_PROMPT_LIMIT_CHARS = 30_000
DEVIN_PROMPT_BUDGET_CHARS = 28_000


@dataclass(frozen=True)
class _DevinTransport:
    token: str
    version: str
    session_base_url: str


def get_devin_token() -> str:
    """Return the active Devin API token (from DEVIN_PAT or DEVIN_API_KEY)."""
    pat = (os.getenv("DEVIN_PAT") or "").strip()
    api_key = (os.getenv("DEVIN_API_KEY") or "").strip()
    token = pat or api_key
    if not token:
        raise ValueError("Missing DEVIN_PAT or DEVIN_API_KEY in environment or .env")
    return token


def get_devin_org_id(token: str) -> str | None:
    """Retrieve organization ID for Devin v3 API."""
    global _CACHED_ORG
    explicit_org = os.getenv("DEVIN_ORG_ID")
    if explicit_org and explicit_org.strip():
        return explicit_org.strip()

    cached_org = _CACHED_ORG
    if cached_org and token == cached_org[0]:
        return cached_org[1]

    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    try:
        response = requests.get("https://api.devin.ai/v3/self", headers=headers, timeout=10)
        if response.status_code == 200:
            data = response.json()
            org_id = (
                data.get("org_id") or data.get("organization_id")
                if isinstance(data, dict)
                else None
            )
            if org_id:
                discovered_org_id = str(org_id)
                _CACHED_ORG = (token, discovered_org_id)
                return discovered_org_id
    except (requests.RequestException, ValueError):
        pass
    return None


def _redact_token(value: str, token: str) -> str:
    """Keep Devin diagnostics useful without ever reflecting credentials."""
    return value.replace(token, "[redacted]") if token else value


def _redact_configured_tokens(value: str) -> str:
    for token in (
        (os.getenv("DEVIN_PAT") or "").strip(),
        (os.getenv("DEVIN_API_KEY") or "").strip(),
    ):
        value = _redact_token(value, token)
    return value


def _is_legacy_api_key(token: str) -> bool:
    return token.startswith(("apk_", "apk_user_"))


def _v1_transport(token: str) -> _DevinTransport:
    base_url = os.getenv("DEVIN_API_URL", "https://api.devin.ai/v1").rstrip("/")
    return _DevinTransport(
        token=token,
        version="v1",
        session_base_url=f"{base_url}/sessions",
    )


def _resolve_transport() -> _DevinTransport:
    pat = (os.getenv("DEVIN_PAT") or "").strip()
    api_key = (os.getenv("DEVIN_API_KEY") or "").strip()
    v3_token = pat or (api_key if api_key.startswith("cog_") else "")

    if not v3_token:
        if not api_key:
            raise ValueError("Missing DEVIN_PAT or DEVIN_API_KEY in environment or .env")
        return _v1_transport(api_key)

    org_id = get_devin_org_id(v3_token)
    if org_id:
        return _DevinTransport(
            token=v3_token,
            version="v3",
            session_base_url=(
                f"https://api.devin.ai/v3/organizations/{org_id}/sessions"
            ),
        )

    # A PAT/cog_ credential is a v3 credential and must never be sent to v1.
    # A separately configured legacy API key is the only safe v1 fallback.
    if pat and _is_legacy_api_key(api_key):
        return _v1_transport(api_key)

    raise RuntimeError(
        "Could not resolve a Devin v3 organization via DEVIN_ORG_ID or /v3/self. "
        "Set DEVIN_ORG_ID, or configure a separate legacy DEVIN_API_KEY "
        "starting with apk_ or apk_user_ for v1 fallback."
    )


def _headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }


def _finished_result(
    role_id: str,
    current: dict[str, Any],
    session_id: str,
    session_url: str,
) -> DevinCallResult:
    output = current.get("structured_output")
    if not isinstance(output, dict):
        raise ValueError(f"Devin role {role_id} returned no structured output")
    return DevinCallResult(output=output, session_id=session_id, url=session_url)


def serialize_prompt(prompt: str, context: dict[str, Any]) -> str:
    """Build the exact prompt string counted by Devin's request limit."""
    return (
        f"{prompt}\n\nINPUT JSON:\n"
        f"{json.dumps(context, ensure_ascii=False, separators=(',', ':'))}"
    )


def call(
    role_id: str,
    prompt: str,
    context: dict[str, Any],
    schema: dict[str, Any],
    attempt: int = 0,
    on_session_created: SessionCreatedCallback | None = None,
) -> DevinCallResult:
    """Run one structured Devin session (v3 with v1 fallback) and return its output and identity."""
    serialized_prompt = serialize_prompt(prompt, context)
    if len(serialized_prompt) >= DEVIN_PROMPT_LIMIT_CHARS:
        raise ValueError(
            f"Devin role {role_id} prompt has {len(serialized_prompt)} characters; "
            f"the limit is <{DEVIN_PROMPT_LIMIT_CHARS}"
        )
    transport = _resolve_transport()
    headers = _headers(transport.token)
    run_number = context.get("run_number", 0)
    payload: dict[str, Any] = {
        "prompt": serialized_prompt,
        "structured_output_schema": schema,
        "tags": [f"run-{run_number}", role_id],
        "title": f"Palantum {role_id} · run {run_number}",
        "max_acu_limit": int(os.getenv(f"PALANTUM_{role_id}_MAX_ACU", "5")),
    }
    snapshot_id = os.getenv("DEVIN_SNAPSHOT_ID")
    if snapshot_id and transport.version == "v1":
        payload["snapshot_id"] = snapshot_id
    if transport.version == "v3":
        payload["structured_output_required"] = True

    created = requests.post(
        transport.session_base_url,
        headers=headers,
        json=payload,
        timeout=60,
    )
    try:
        created.raise_for_status()
    except requests.HTTPError as exc:
        detail = _redact_configured_tokens(created.text.strip())[:2000]
        raise RuntimeError(
            f"Devin role {role_id} session creation failed "
            f"({created.status_code}): {detail or 'no response body'}"
        ) from exc
    session = created.json()
    session_id = str(session["session_id"])
    session_url = str(session.get("url") or f"https://app.devin.ai/sessions/{session_id}")
    if on_session_created is not None:
        on_session_created(session_id, session_url)

    timeout_s = float(os.getenv("PALANTUM_DEVIN_TIMEOUT_S", "600"))
    poll_interval_s = float(os.getenv("PALANTUM_DEVIN_POLL_INTERVAL_S", "10"))
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        response = requests.get(
            f"{transport.session_base_url}/{session_id}",
            headers=headers,
            timeout=30,
        )
        response.raise_for_status()
        current = response.json()
        if not isinstance(current, dict):
            raise ValueError(f"Devin role {role_id} returned an invalid session payload")

        if transport.version == "v3":
            v3_status = str(current.get("status") or "")
            status_detail = str(current.get("status_detail") or "")
            output = current.get("structured_output")
            if v3_status == "running" and status_detail == "finished":
                return _finished_result(role_id, current, session_id, session_url)
            if v3_status == "exit" and isinstance(output, dict):
                return DevinCallResult(
                    output=output,
                    session_id=session_id,
                    url=session_url,
                )
            if v3_status in {"exit", "error", "suspended"}:
                redacted_status_detail = _redact_configured_tokens(status_detail)
                detail = _redact_configured_tokens(str(current))
                raise RuntimeError(
                    f"Devin role {role_id} ended {v3_status} "
                    f"({redacted_status_detail or 'no status detail'}): {detail}"
                )
        else:
            v1_status = current.get("status_enum") or current.get("status")
            output = current.get("structured_output")
            if v1_status in {"finished", "blocked"} and isinstance(output, dict):
                return DevinCallResult(
                    output=output,
                    session_id=session_id,
                    url=session_url,
                )
            if v1_status == "finished":
                return _finished_result(role_id, current, session_id, session_url)
            if v1_status in {"blocked", "expired", "stopped"}:
                detail = _redact_configured_tokens(str(current))
                raise RuntimeError(
                    f"Devin role {role_id} ended {v1_status}: {detail}"
                )
        time.sleep(poll_interval_s)

    terminated = requests.delete(
        f"{transport.session_base_url}/{session_id}",
        headers=headers,
        timeout=30,
    )
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
