# COGNIFRAME

**AI-native video production for B2B startup pitch videos — built entirely on Devin.**

COGNIFRAME turns raw founder footage into a structured, QC-verified 60-second investor pitch. It does not clip existing content. It produces new editorial decisions from scratch: beat coverage analysis, take selection, motion graphic integration, A/B variant production, and a final quality-controlled render — all orchestrated through a multi-agent Devin pipeline.

---

## Why this exists

Startup founders record their pitch in one afternoon. The footage is usually good enough. The edit is almost never good enough.

Existing tools solve the wrong problem. Opus Clip and CapCut find strong moments in content that is already well-structured. A founder's raw footage is not well-structured — it is seven beats of information scattered across three takes, with no clear hook, no time-anchored traction number, and no screen recording for the demo beat.

COGNIFRAME solves the production problem, not the distribution problem.

The real-world output below comes from an actual founder session recorded on 2026-08-22:

```json
{
  "schema": "yc_pitch_60s",
  "coverage_score": 0.3333,
  "meta": { "iteration": 3, "format": "9:16" },
  "beats": [
    { "id": "HOOK",     "status": "weak",    "reason": "Opening spends the first second on a product name the investor has no reason to care about yet." },
    { "id": "PROBLEM",  "status": "missing", "reason": "No phrase in the take names a sufferer or a pain." },
    { "id": "SOLUTION", "status": "weak",    "reason": "States a delivery category, not a mechanism — remains true of any competitor." },
    { "id": "DEMO",     "status": "missing", "reason": "Single take is talking_head with has_ui false — no visual product material." },
    { "id": "TRACTION", "status": "weak",    "reason": "One number but no time anchor — investor cannot tell whether 50 took one month or four years." },
    { "id": "TEAM",     "status": "weak",    "reason": "Advantage cited belongs to unnamed outsiders — no person on the team is tied to anything unfair." },
    { "id": "ASK",      "status": "weak",    "reason": "Slogan with no action and no object — convinced investor has nothing to click, book or reply to." }
  ]
}
```

A coverage score of 0.33 after three iterations is not a failure. It is the system correctly diagnosing that the founder needs to shoot four more takes — and telling him exactly what to say, how to frame it, and where it lands in the timeline. No existing tool produces this output.

---

## The team

We are former **TUM.ai Hackathon winners** with hands-on expertise across social media production, professional video cutting, machine learning, and applied AI systems. Our background combines:

- **Video production**: professional editing experience across social media formats, NLE workflows (Final Cut Pro, DaVinci Resolve), and short-form content at scale
- **Machine learning**: model training, deployment, and evaluation in production environments
- **AI systems**: multi-agent orchestration, structured LLM output pipelines, and real-time inference at the application layer

We built COGNIFRAME because we have experienced both sides of this problem — the founder who cannot edit and the editor who cannot read a pitch deck. The system encodes what a good pitch editor actually does, not a simplified proxy for it.

---

## Why Devin — not GPT-4o, not Claude, not Gemini

### 1. Structured output with validated retry semantics

Every agent role receives a JSON schema and must pass `jsonschema.Draft202012Validator`. When validation fails, the error is injected into the next attempt's context:

```python
for attempt in range(2):
    attempt_context = dict(context)
    if last_error:
        attempt_context["_validation_error"] = last_error
    result = call_devin(role_id, prompt, attempt_context, schema, attempt=attempt)
    try:
        _validate(role_id, result.output)
        return result.output
    except ValueError as error:
        last_error = str(error)
```

Devin's `structured_output_required: True` (v3 API) gives stronger output guarantees than JSON mode in token-based APIs, and integrates naturally with this retry loop — the session model means each attempt has full context of what went wrong in the previous one.

### 2. Per-role cost control via ACU limits

Each of the eight agent roles has an independent compute budget:

```python
"max_acu_limit": int(os.getenv(f"PALANTUM_{role_id}_MAX_ACU", "5"))
```

A6W (slot worker, fills text into a Remotion template) needs 1–2 ACU. A2 (Director, reasoning over full transcripts and previous coverage state) may need 4–5. This granular cost control per role is not possible with token-based APIs where the only lever is model selection.

### 3. Session URLs as production observability

Every agent role produces a Devin session URL stored in `sessions.json` and surfaced live in the web UI:

```python
session_url = str(session.get("url") or f"https://app.devin.ai/sessions/{session_id}")
_record_session(context, role_id, "running", url)
```

When a 10-minute A4 Cutter session produces a bad edit, the operator opens the session URL and sees exactly what the agent reasoned through. This is not a debugging convenience — it is the difference between a diagnosable system and an opaque one. No other API surface provides this natively.

### 4. The session model maps directly to the role model

COGNIFRAME treats each agent role as a discrete unit of work with a clear input contract, a JSON schema output contract, and a session identity. Devin's session primitive maps exactly to this:

```json
{
  "A1": { "status": "done",    "url": "https://app.devin.ai/sessions/..." },
  "A2": { "status": "running", "url": "https://app.devin.ai/sessions/..." },
  "A4": { "status": "idle",    "url": null }
}
```

With a pure API backend, this live status layer requires custom infrastructure. With Devin it is a first-class feature of the platform.

### 5. v3/v1 transport fallback for production stability

```python
def _resolve_transport() -> _Transport:
    # v3 with automatic v1 fallback — survives API version transitions
```

The system detects which Devin API version is available and degrades gracefully. A founder's video processing cannot be blocked by an API version transition.

---

## Agent architecture

Eight specialized roles. Strict input/output contracts. No agent touches what belongs to another.

```
+-----------------------------------------------------------------+
|  ANALYSIS PHASE                              [parallel: A1, A0] |
|                                                                 |
|  A1 Script Supervisor --> phrase candidates per beat            |
|  A0 Template Scout    --> Remotion template classification      |
|                    |                                            |
|                    v                                            |
|  A2 Director      --> coverage verdicts + director notes        |
|                    |                                            |
|                    v                                            |
|  A3 Strategist    --> content hardness rulings (overrules A2)   |
+-----------------------------------------------------------------+
                    |
                    v
+-----------------------------------------------------------------+
|  PRODUCTION PHASE                    [parallel: chunks x 2]    |
|                                                                 |
|  A4 Cutter        --> exact timecode ranges per beat per chunk  |
|  A6 Graphics Dir  --> overlay plan (template x beat x timing)  |
|  A6W Slot Worker  --> prop fill per template slot [parallel]   |
|                    |                                            |
|                    v                                            |
|  FFmpeg + Remotion --> preview.mp4 per variant                 |
+-----------------------------------------------------------------+
                    |
          [human review: select variant per chunk]
                    |
                    v
+-----------------------------------------------------------------+
|  QC PHASE                                                       |
|                                                                 |
|  A5 Variant Supervisor --> async recommendation per chunk      |
|  A7 QC Agent           --> audio + duration + overlay audit    |
|                    |                                            |
|                    v                                            |
|  final.mp4 + edl.json + timeline.fcpxml + timeline.otio        |
+-----------------------------------------------------------------+
```

### The A2/A3 debate — adversarial quality by design

A2 (Director) judges coverage filmically: did the speaker say something usable for this beat?
A3 (Strategist) judges coverage editorially: is what was said concrete enough to convince an investor?

A3 can overrule A2's `weak` and `covered` verdicts. Every disagreement is logged:

```python
state["debate_log"].append({
    "round": 1,
    "beat": beat_id,
    "a2": director_beat["status"],
    "a3": strategist_status,
    "resolved": strategist_status,
    "rule": "A3 gewinnt bei Inhalt",
})
```

Without A3, the TRACTION beat above would be marked `covered` because the founder said a number. With A3, it stays `weak` because the number has no time anchor. That distinction is the difference between an investor who trusts the pitch and one who does not.

---

## What the system produces

From raw founder footage, COGNIFRAME exports:

| File | Description |
|------|-------------|
| `final.mp4` | Rendered 60s pitch, color-graded, motion graphics composited, subtitles burned |
| `timeline.fcpxml` | Native Final Cut Pro X timeline — V1 A-Roll, V2 B-Roll, V3 Graphics, V4 Titles |
| `timeline.otio` | OpenTimelineIO for DaVinci Resolve, Premiere, and any OTIO-compatible NLE |
| `timeline.edl` | CMX 3600 EDL fallback |
| `subtitles.srt` | Whisper-generated, word-accurate, editable |
| `coverage.json` | Beat coverage state, director notes, debate log, brand config |
| `chunks/` | Per-chunk A/B variant previews with EDL, transcript copies, and Remotion renders |

The NLE export means a professional editor can open the project, see every editorial decision as an editable layer, and refine from there. COGNIFRAME is a structured first pass that a human can own — not a locked black box.

---

## Coverage as a gate

The system will not cut a final video unless every required beat is at least `weak`:

```python
def gate(state, schema):
    blocked = [
        beat for beat in schema["beats"]
        if beat.get("required") and statuses.get(beat["id"]) == "missing"
    ]
    return not blocked, state["director_notes"]
```

A coverage score of 0.33 means the gate is blocked. The founder sees which beats are missing, receives a shootable Director Note for each one, and re-ingests new takes. The system is iterative because the footage might be incomplete — not because the AI might be wrong.

---

## Comparison to existing tools

| Capability | Opus Clip | Descript | Runway | COGNIFRAME |
|-----------|-----------|----------|--------|------------|
| Works from raw unedited takes | No | Partial | No | Yes |
| Semantic beat coverage analysis | No | No | No | Yes |
| Adversarial agent quality check | No | No | No | Yes |
| Director notes for missing footage | No | No | No | Yes |
| A/B variant production per beat | No | No | No | Yes |
| Motion graphic integration | No | No | Partial | Yes |
| NLE-ready export (FCPXML / OTIO) | No | Yes | No | Yes |
| QC agent with audio measurement | No | No | No | Yes |
| Human stays in control | No | Yes | Partial | Yes |

Opus Clip optimizes for virality signals. COGNIFRAME optimizes for investor conviction. These are different problems with different output requirements.

---

## Technical stack

| Layer | Technology |
|-------|------------|
| Agent runtime | Devin API v3 (v1 fallback) |
| Transcription | OpenAI Whisper (`whisper-1`) |
| Motion graphics | Remotion (React-based, server-side render) |
| Video processing | FFmpeg (transcode, composite, volumedetect, timeline views) |
| Backend | FastAPI + Pydantic |
| Frontend | Single-page HTML — real-time canvas with agent cursors, ghost text, A/B review UI |
| Output validation | `jsonschema` Draft 2020-12 |
| Export | FCPXML 1.11, OpenTimelineIO, CMX 3600 EDL |

---

## Setup

```bash
uv sync
cp .env.example .env
# Add DEVIN_API_KEY or DEVIN_PAT, OPENAI_API_KEY, PALANTUM_TEMPLATE_SOURCE
uv run palantum serve
```

### Environment variables

| Variable | Required | Description |
|----------|----------|-------------|
| `DEVIN_API_KEY` or `DEVIN_PAT` | Yes | Devin API authentication |
| `OPENAI_API_KEY` | Yes | Whisper transcription |
| `PALANTUM_TEMPLATE_SOURCE` | Yes for Motion Cut | Path to Remotion template ZIP |
| `PALANTUM_DEVIN_TIMEOUT_S` | No (default 600) | Per-role session timeout |
| `PALANTUM_{ROLE}_MAX_ACU` | No (default 5) | ACU limit per agent role |

### CLI

```bash
palantum ingest footage/*.mp4   # transcribe and analyze
palantum status                 # print coverage dots and director notes
palantum cut                    # produce A/B variants and final render
palantum export                 # package for NLE
```

---

## Tests

```bash
uv run pytest tests/ -v
```

`tests/test_deterministic.py` validates that identical inputs produce identical outputs across runs using fixture transcripts and mock Devin responses.

---

## License

MIT
