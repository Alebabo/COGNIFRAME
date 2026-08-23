from __future__ import annotations

import os
import re
import uuid
from collections.abc import Iterator, Mapping, Sequence
from copy import deepcopy
from typing import Any, TypedDict, cast

BEAT_KEYS = ("HOOK", "PROBLEM", "SOLUTION", "DEMO", "TRACTION", "TEAM", "ASK")

BEAT_DEFAULTS = {
    "HOOK": {
        "title": "Hook",
        "time": "3–6s",
        "description": "Open with a concrete, compelling statement in the first 1.5s. No greeting.",
        "placeholder": "State your thesis or write a compelling opening...",
    },
    "PROBLEM": {
        "title": "Problem",
        "time": "6–10s",
        "description": "Who experiences the pain, and what does it really cost them?",
        "placeholder": "Describe the target audience's concrete problem...",
    },
    "SOLUTION": {
        "title": "Solution",
        "time": "8–14s",
        "description": "Explain the concrete mechanism—how does the solution work?",
        "placeholder": "Explain the unique mechanism behind your solution...",
    },
    "DEMO": {
        "title": "Demo & Visuals",
        "time": "8–15s",
        "description": "Show visible product footage or a UI walkthrough.",
        "placeholder": "Show or describe the product in real use...",
    },
    "TRACTION": {
        "title": "Traction & Metrics",
        "time": "4–8s",
        "description": "Include at least one concrete metric with a clear timeframe.",
        "placeholder": "Add measurable users, revenue, or growth...",
    },
    "TEAM": {
        "title": "Team",
        "time": "3–6s",
        "description": "Name the team and its decisive unfair advantage.",
        "placeholder": "Describe the team's distinctive background and core expertise...",
    },
    "ASK": {
        "title": "Call to Action",
        "time": "3–5s",
        "description": "End with a clear call to action and a concrete goal.",
        "placeholder": "Write a clear call to action for the target audience...",
    },
}

AGENT_CURSORS = (
    {
        "agent": "A2",
        "role": "Director",
        "name": "A2 Director",
        "color": "#8b5cf6",
        "beat": "HOOK",
        "status": "ready",
    },
    {
        "agent": "A3",
        "role": "Strategist",
        "name": "A3 Strategist",
        "color": "#f59e0b",
        "beat": "PROBLEM",
        "status": "ready",
    },
    {
        "agent": "A1",
        "role": "Supervisor",
        "name": "A1 Supervisor",
        "color": "#3b82f6",
        "beat": "ASK",
        "status": "ready",
    },
)

_AGENT_ORDER = ("A2", "A3", "A1")
_AGENT_ROLES = {
    "A2": "Director",
    "A3": "Strategist",
    "A1": "Supervisor",
}
_BEAT_ALIASES = {
    "CTA": "ASK",
}
_BEAT_HEADER = re.compile(
    r"^(?:(?:0?[1-7])(?:[.)]|\s*[-:])?\s+)?"
    r"\[?(?P<beat>HOOK|PROBLEM|SOLUTION|DEMO|TRACTION|TEAM|ASK|CTA)\]?"
    r"(?:\s*/\s*(?:HOOK|PROBLEM|SOLUTION|DEMO|TRACTION|TEAM|ASK|CTA))?"
    r"(?:\s*[:—-]\s*|\s+|$)(?P<body>.*)$",
    re.IGNORECASE,
)


class CanvasAgentError(RuntimeError):
    """Base error for the real-time canvas agent layer."""


class CanvasAgentUnavailableError(CanvasAgentError):
    """Raised when Devin is not configured or cannot be reached."""


class CanvasAgentResponseError(CanvasAgentError):
    """Raised when Devin returns a response that violates the canvas contract."""


class CanvasAgentSuggestion(TypedDict):
    agent: str
    beat: str
    anchor_text: str
    message: str
    ghost_text: str


class CanvasOrchestrationResult(TypedDict):
    request_id: str
    session_id: str
    session_url: str
    agents: list[CanvasAgentSuggestion]


CANVAS_ORCHESTRATION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["agents"],
    "additionalProperties": False,
    "properties": {
        "agents": {
            "type": "array",
            "minItems": 3,
            "maxItems": 3,
            "items": {
                "type": "object",
                "required": ["agent", "beat", "anchor_text", "message", "ghost_text"],
                "additionalProperties": False,
                "properties": {
                    "agent": {"type": "string", "enum": list(_AGENT_ORDER)},
                    "beat": {"type": "string", "enum": list(BEAT_KEYS)},
                    "anchor_text": {"type": "string", "minLength": 1},
                    "message": {"type": "string", "minLength": 1},
                    "ghost_text": {"type": "string"},
                },
            },
        }
    },
}

CANVAS_ASSIST_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["agent", "beat", "anchor_text", "message", "ghost_text"],
    "additionalProperties": False,
    "properties": {
        "agent": {"type": "string", "enum": list(_AGENT_ORDER)},
        "beat": {"type": "string", "enum": list(BEAT_KEYS)},
        "anchor_text": {"type": "string", "minLength": 1},
        "message": {"type": "string", "minLength": 1},
        "ghost_text": {"type": "string"},
    },
}

SCRIPT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["script"],
    "additionalProperties": False,
    "properties": {"script": {"type": "string", "minLength": 1}},
}

_ORCHESTRATION_PROMPT = """You are the PitchCraft canvas orchestrator. In one response, provide
one independent contribution from each of these three agents: A2 Director, A3 Strategist, and
A1 Supervisor. A2 improves the hook and visual direction; A3 challenges the problem, solution,
and evidence; A1 protects timing, clarity, and the final call to action.

Return exactly the requested JSON schema. Each anchor_text must be a non-empty, case-sensitive,
verbatim substring of INPUT JSON.text. Do not invent an anchor. Keep message concise and useful.
ghost_text is an optional continuation to insert immediately after anchor_text. Use an empty
string when no wording is useful. Never repeat text already present in INPUT JSON.text or in
accepted_ghost_texts. Continue an unfinished sentence with matching grammar and casing; after
sentence-ending punctuation, begin a new sentence. Always write message and ghost_text in English,
even when the input uses another language. Never invent metrics, customers, or facts."""

_ASSIST_PROMPT = """Act only as the requested PitchCraft canvas agent and return the requested
JSON object. anchor_text must be a non-empty, case-sensitive, verbatim substring of INPUT
JSON.text. Keep message concise. ghost_text, when non-empty, must be wording that can be inserted
immediately after anchor_text without repeating current or accepted text. Preserve grammatical
continuation. Always write message and ghost_text in English, even when the input uses another
language. Never invent metrics, customers, or facts."""

_SCRIPT_PROMPT = """You are PitchCraft's A1 Script Supervisor. Turn the supplied description into
a concise, speakable startup video script of about 60 seconds. Use HOOK, PROBLEM, SOLUTION, DEMO,
TRACTION, TEAM, and ASK sections. Do not invent numbers, customers, results, or product features;
mark genuinely missing facts briefly in square brackets. Write the complete script in English,
even when the description uses another language. Return only the structured JSON."""


def parse_canvas_beats(text: str) -> dict[str, str]:
    """Parse tagged canvas text while preserving untagged prose as the hook."""
    if not isinstance(text, str):
        raise TypeError("canvas text must be a string")

    grouped: dict[str, list[str]] = {beat: [] for beat in BEAT_KEYS}
    current: str | None = None
    for line in text.splitlines():
        match = _BEAT_HEADER.match(line.strip())
        if match is not None:
            raw_beat = match.group("beat").upper()
            current = _BEAT_ALIASES.get(raw_beat, raw_beat)
            body = match.group("body").strip()
            if body:
                grouped[current].append(body)
            continue

        if current is None:
            if not line.strip():
                continue
            current = "HOOK"
        grouped[current].append(line)

    return {beat: "\n".join(grouped[beat]).strip() for beat in BEAT_KEYS}


def build_canvas_metadata(
    title: str = "My Startup Pitch",
    text: str = "",
    beats: Mapping[str, str] | None = None,
    attached_videos: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Build an isolated, purely local canvas payload with no agent call."""
    normalized_beats = parse_canvas_beats(text) if text else {beat: "" for beat in BEAT_KEYS}
    if beats is not None:
        for key, value in beats.items():
            canonical = _canonical_beat(key)
            if canonical is not None:
                normalized_beats[canonical] = value.strip()

    normalized_attachments = {
        str(key): str(value)
        for key, value in (attached_videos or {}).items()
        if str(key).strip() and str(value).strip()
    }
    return {
        "title": title.strip() or "My Startup Pitch",
        "text": text,
        "beats": normalized_beats,
        "beat_info": deepcopy(BEAT_DEFAULTS),
        "attached_videos": normalized_attachments,
        "agent_cursors": [dict(cursor) for cursor in AGENT_CURSORS],
    }


def orchestrate_canvas(
    text: str,
    cursor_offset: int | None = None,
    accepted_ghost_texts: Sequence[str] | None = None,
    request_id: str | None = None,
) -> CanvasOrchestrationResult:
    """Run the A2/A3/A1 canvas trio in one structured Devin session."""
    _require_canvas_text(text)
    bounded_cursor = _bounded_cursor(text, cursor_offset)
    normalized_accepted = _normalize_accepted_ghosts(accepted_ghost_texts)
    resolved_request_id = _resolve_request_id(request_id)
    context: dict[str, Any] = {
        "request_id": resolved_request_id,
        "_session_key": f"canvas-orchestrate-{_safe_session_part(resolved_request_id)}",
        "run_number": 0,
        "text": text,
        "cursor_offset": bounded_cursor,
        "cursor_context": text[max(0, bounded_cursor - 120) : bounded_cursor + 120],
        "accepted_ghost_texts": normalized_accepted,
        "beats": parse_canvas_beats(text),
        "agents": [{"agent": agent, "role": _AGENT_ROLES[agent]} for agent in _AGENT_ORDER],
    }
    output, session_id, session_url = _run_structured_devin(
        "CANVAS", _ORCHESTRATION_PROMPT, context, CANVAS_ORCHESTRATION_SCHEMA
    )
    agents = _normalize_orchestration_agents(output, text, bounded_cursor, normalized_accepted)
    return {
        "request_id": resolved_request_id,
        "session_id": session_id,
        "session_url": session_url,
        "agents": agents,
    }


def assist_canvas_agent(
    agent_id: str,
    text: str,
    beat: str | None = None,
    cursor_offset: int | None = None,
    accepted_ghost_texts: Sequence[str] | None = None,
    request_id: str | None = None,
) -> CanvasAgentSuggestion:
    """Ask one canvas agent for one anchored, structured suggestion."""
    _require_canvas_text(text)
    normalized_agent = agent_id.strip().upper()
    if normalized_agent not in _AGENT_ROLES:
        raise ValueError(f"unsupported canvas agent: {agent_id}")
    requested_beat = _canonical_beat(beat) if beat is not None else None
    if beat is not None and requested_beat is None:
        raise ValueError(f"unsupported canvas beat: {beat}")

    bounded_cursor = _bounded_cursor(text, cursor_offset)
    normalized_accepted = _normalize_accepted_ghosts(accepted_ghost_texts)
    resolved_request_id = _resolve_request_id(request_id)
    context: dict[str, Any] = {
        "request_id": resolved_request_id,
        "_session_key": (
            f"canvas-assist-{normalized_agent.lower()}-{_safe_session_part(resolved_request_id)}"
        ),
        "run_number": 0,
        "agent": normalized_agent,
        "role": _AGENT_ROLES[normalized_agent],
        "requested_beat": requested_beat,
        "text": text,
        "cursor_offset": bounded_cursor,
        "cursor_context": text[max(0, bounded_cursor - 120) : bounded_cursor + 120],
        "accepted_ghost_texts": normalized_accepted,
        "beats": parse_canvas_beats(text),
    }
    output, _session_id, _session_url = _run_structured_devin(
        normalized_agent, _ASSIST_PROMPT, context, CANVAS_ASSIST_SCHEMA
    )
    suggestion = _normalize_suggestion(
        output, text, bounded_cursor, set(_comparison_key(value) for value in normalized_accepted)
    )
    if suggestion["agent"] != normalized_agent:
        raise CanvasAgentResponseError(
            f"Devin returned {suggestion['agent']} for requested agent {normalized_agent}"
        )
    if requested_beat is not None and suggestion["beat"] != requested_beat:
        raise CanvasAgentResponseError(
            f"Devin returned beat {suggestion['beat']} for requested beat {requested_beat}"
        )
    return suggestion


def evaluate_canvas_agentic(
    text: str, brief: str = "", attached_videos: Mapping[str, str] | None = None
) -> list[CanvasAgentSuggestion]:
    """Compatibility adapter; all evaluation is delegated to Devin."""
    del brief, attached_videos
    return orchestrate_canvas(text)["agents"]


def evaluate_canvas(
    beats: Mapping[str, str], attached_videos: Mapping[str, str] | None = None
) -> list[CanvasAgentSuggestion]:
    """Compatibility adapter for callers that still hold a beat mapping."""
    del attached_videos
    text = "\n\n".join(
        f"{beat}\n{beats.get(beat, '').strip()}"
        for beat in BEAT_KEYS
        if beats.get(beat, "").strip()
    )
    return orchestrate_canvas(text)["agents"]


def create_agent_assist_stream(
    agent_id: str, beat_id: str | None, current_text: str, brief: str
) -> tuple[Iterator[str], str]:
    """Compatibility stream adapter over the structured Devin assist call."""
    text = current_text if current_text.strip() else brief
    suggestion = assist_canvas_agent(agent_id, text, beat=beat_id)
    response = suggestion["ghost_text"] or suggestion["message"]
    return iter((response,)), "devin"


def create_script_stream(description: str) -> tuple[Iterator[str], str]:
    """Generate the legacy script endpoint response through Devin only."""
    if not description.strip():
        raise ValueError("script description must not be empty")
    request_id = uuid.uuid4().hex
    context: dict[str, Any] = {
        "request_id": request_id,
        "_session_key": f"canvas-script-{request_id}",
        "run_number": 0,
        "description": description,
    }
    output, _session_id, _session_url = _run_structured_devin(
        "A1", _SCRIPT_PROMPT, context, SCRIPT_SCHEMA
    )
    script = output.get("script")
    if not isinstance(script, str) or not script.strip():
        raise CanvasAgentResponseError("Devin returned an empty script")
    return iter((script.strip(),)), "devin"


def _call_devin(
    role_id: str,
    prompt: str,
    context: dict[str, Any],
    schema: dict[str, Any],
) -> tuple[dict[str, Any], str, str]:
    from pitchcraft.agents.backends.devin import call

    result = call(role_id, prompt, context, schema)
    return result.output, result.session_id, result.url


def _run_structured_devin(
    role_id: str,
    prompt: str,
    context: dict[str, Any],
    schema: dict[str, Any],
) -> tuple[dict[str, Any], str, str]:
    if not any(
        isinstance(value, str) and value.strip()
        for value in (os.getenv("DEVIN_PAT"), os.getenv("DEVIN_API_KEY"))
    ):
        raise CanvasAgentUnavailableError(
            "Devin is not configured; set DEVIN_PAT or DEVIN_API_KEY in the environment"
        )
    try:
        output, session_id, session_url = _call_devin(role_id, prompt, context, schema)
    except CanvasAgentError:
        raise
    except ValueError as error:
        raise CanvasAgentResponseError(
            f"Devin returned invalid structured output: {error}"
        ) from error
    except Exception as error:
        raise CanvasAgentUnavailableError(f"Devin request failed: {error}") from error
    if not isinstance(output, dict):
        raise CanvasAgentResponseError("Devin structured output must be an object")
    return output, str(session_id), str(session_url)


def _normalize_orchestration_agents(
    output: Mapping[str, Any],
    text: str,
    cursor_offset: int,
    accepted_ghost_texts: Sequence[str],
) -> list[CanvasAgentSuggestion]:
    raw_agents = output.get("agents")
    if not isinstance(raw_agents, list):
        raise CanvasAgentResponseError("Devin canvas response must contain an agents list")

    seen_ghosts = {_comparison_key(value) for value in accepted_ghost_texts}
    by_agent: dict[str, CanvasAgentSuggestion] = {}
    for raw_agent in raw_agents:
        if not isinstance(raw_agent, dict):
            raise CanvasAgentResponseError("Every Devin canvas contribution must be an object")
        suggestion = _normalize_suggestion(
            cast(Mapping[str, object], raw_agent), text, cursor_offset, seen_ghosts
        )
        agent = suggestion["agent"]
        if agent in by_agent:
            raise CanvasAgentResponseError(f"Devin returned duplicate contribution for {agent}")
        by_agent[agent] = suggestion

    missing = [agent for agent in _AGENT_ORDER if agent not in by_agent]
    extra = [agent for agent in by_agent if agent not in _AGENT_ORDER]
    if missing or extra:
        detail = ", ".join(
            [
                *(f"missing {agent}" for agent in missing),
                *(f"extra {agent}" for agent in extra),
            ]
        )
        raise CanvasAgentResponseError(
            f"Devin must return one contribution per canvas agent: {detail}"
        )
    return [by_agent[agent] for agent in _AGENT_ORDER]


def _normalize_suggestion(
    raw: Mapping[str, object],
    text: str,
    cursor_offset: int,
    seen_ghosts: set[str],
) -> CanvasAgentSuggestion:
    agent = _required_string(raw, "agent").upper()
    if agent not in _AGENT_ROLES:
        raise CanvasAgentResponseError(f"unsupported agent in Devin response: {agent}")
    raw_beat = _required_string(raw, "beat")
    beat = _canonical_beat(raw_beat)
    if beat is None:
        raise CanvasAgentResponseError(f"unsupported beat in Devin response: {raw_beat}")
    anchor = _required_string(raw, "anchor_text")
    anchor_end = _anchor_end(text, anchor, cursor_offset)
    message = _normalize_spacing(_required_string(raw, "message"))

    raw_ghost_value = raw.get("ghost_text", "")
    if not isinstance(raw_ghost_value, str):
        raise CanvasAgentResponseError("ghost_text in Devin response must be a string")
    raw_ghost = _normalize_spacing(raw_ghost_value)
    raw_key = _comparison_key(raw_ghost)
    text_key = _comparison_key(text)
    if raw_key and (raw_key in text_key or raw_key in seen_ghosts):
        ghost = ""
    else:
        prefix = text[:anchor_end]
        ghost = _remove_boundary_overlap(raw_ghost, prefix)
        ghost = _normalize_continuation_case(ghost, prefix)
        ghost_key = _comparison_key(ghost)
        if ghost_key and (ghost_key in text_key or ghost_key in seen_ghosts):
            ghost = ""
        elif ghost_key:
            seen_ghosts.add(ghost_key)

    return {
        "agent": agent,
        "beat": beat,
        "anchor_text": anchor,
        "message": message,
        "ghost_text": ghost,
    }


def _required_string(raw: Mapping[str, object], field: str) -> str:
    value = raw.get(field)
    if not isinstance(value, str) or not value.strip():
        raise CanvasAgentResponseError(f"{field} in Devin response must be a non-empty string")
    return value.strip()


def _canonical_beat(value: str | None) -> str | None:
    if value is None:
        return None
    raw = value.strip().upper()
    canonical = _BEAT_ALIASES.get(raw, raw)
    return canonical if canonical in BEAT_KEYS else None


def _anchor_end(text: str, anchor: str, cursor_offset: int) -> int:
    starts: list[int] = []
    search_from = 0
    while True:
        found = text.find(anchor, search_from)
        if found < 0:
            break
        starts.append(found)
        search_from = found + max(1, len(anchor))
    if not starts:
        raise CanvasAgentResponseError(
            f"anchor_text is not a verbatim substring of the canvas: {anchor!r}"
        )
    start = min(starts, key=lambda value: abs(value + len(anchor) - cursor_offset))
    return start + len(anchor)


def _normalize_accepted_ghosts(values: Sequence[str] | None) -> list[str]:
    normalized: list[str] = []
    seen: set[str] = set()
    for value in values or ():
        if not isinstance(value, str):
            continue
        clean = _normalize_spacing(value)
        key = _comparison_key(clean)
        if clean and key not in seen:
            normalized.append(clean)
            seen.add(key)
    return normalized


def _normalize_spacing(value: str) -> str:
    collapsed = re.sub(r"\s+", " ", value).strip()
    return re.sub(r"\s+([,.;:!?])", r"\1", collapsed)


def _comparison_key(value: str) -> str:
    return _normalize_spacing(value).casefold()


def _remove_boundary_overlap(ghost: str, prefix: str) -> str:
    if not ghost:
        return ""
    prefix_words = prefix.split()
    ghost_words = ghost.split()
    limit = min(6, len(prefix_words), len(ghost_words))
    for size in range(limit, 0, -1):
        before = [_word_key(word) for word in prefix_words[-size:]]
        after = [_word_key(word) for word in ghost_words[:size]]
        if before == after and all(before):
            return _normalize_spacing(" ".join(ghost_words[size:]))
    return ghost


def _word_key(value: str) -> str:
    return value.strip(".,;:!?()[]{}\"'“”„…—-").casefold()


def _normalize_continuation_case(ghost: str, prefix: str) -> str:
    if not ghost:
        return ""
    index = next((position for position, char in enumerate(ghost) if char.isalpha()), None)
    if index is None:
        return ghost
    terminal = prefix.rstrip().endswith((".", "!", "?", "…"))
    replacement = ghost[index].upper() if terminal else ghost[index].lower()
    return f"{ghost[:index]}{replacement}{ghost[index + 1 :]}"


def _bounded_cursor(text: str, cursor_offset: int | None) -> int:
    if cursor_offset is None:
        return len(text)
    return max(0, min(int(cursor_offset), len(text)))


def _resolve_request_id(request_id: str | None) -> str:
    if request_id is None:
        return uuid.uuid4().hex
    resolved = request_id.strip()
    if not resolved:
        raise ValueError("request_id must not be empty")
    if len(resolved) > 128:
        raise ValueError("request_id must be at most 128 characters")
    return resolved


def _safe_session_part(value: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "-", value).strip("-.")
    return safe[:80] or uuid.uuid4().hex


def _require_canvas_text(text: str) -> None:
    if not isinstance(text, str) or not text.strip():
        raise ValueError("canvas text must not be empty")
