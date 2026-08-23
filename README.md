# COGNIFRAME

**An autonomous pitch-video production layer on top of the Devin API.**

COGNIFRAME turns raw founder footage into an investor-pitch workflow: it identifies missing story beats, writes shootable Director Notes, plans precise cuts, produces clean and motion variants, lets the user choose, and verifies the final render.

The project was built for the Cognition challenge at the Munich European Hackathon League: choose a domain expressible through code and use Devin API sessions to execute an autonomous workflow in that domain. COGNIFRAME's domain is pitch-video production; its executable surface is a versioned JSON state, FFmpeg, Remotion, Whisper, and NLE timeline exports.

> The winning claim is deliberately narrow: COGNIFRAME is not a general video editor and Devin does not render pixels. Devin makes bounded editorial decisions; deterministic local code validates those decisions and executes the media pipeline.

## The team

We are former **TUM.ai Hackathon winners** with hands-on expertise across social media production, professional video cutting, machine learning, and applied AI systems:

- **Video production**: professional editing across social media formats, NLE workflows (Final Cut Pro, DaVinci Resolve), and short-form content at scale
- **Machine learning**: model training, deployment, and evaluation in production environments
- **AI systems**: multi-agent orchestration, structured output pipelines, and real-time inference at the application layer

We built COGNIFRAME because we have experienced both sides of this problem — the founder who cannot edit and the editor who cannot read a pitch deck.

---

## Why it matters

Most editing tools can improve footage that already contains the right story. They cannot recover a missing traction claim, product demonstration, or call to action. COGNIFRAME treats editing as an iterative directing problem:

1. understand what the founder actually recorded;
2. compare it with the seven-beat pitch schema;
3. request concrete replacement footage while the setup is still available;
4. turn approved material into reviewable variants and an editable final package.

The product is the closed loop from diagnosis to a shootable instruction to a verified render—not merely the MP4.

## Real output

The following was produced from an actual founder session on 2026-08-22 after three analysis iterations:

```json
{
  "schema": "yc_pitch_60s",
  "coverage_score": 0.3333,
  "beats": [
    { "id": "HOOK",     "status": "weak",    "reason": "Opening spends the first second on a product name the investor has no reason to care about yet." },
    { "id": "PROBLEM",  "status": "missing", "reason": "No phrase in the take names a sufferer or a pain." },
    { "id": "SOLUTION", "status": "weak",    "reason": "States a delivery category, not a mechanism — remains true of any competitor." },
    { "id": "DEMO",     "status": "missing", "reason": "Single take is talking_head with has_ui false — no visual product material." },
    { "id": "TRACTION", "status": "weak",    "reason": "One number but no time anchor — investor cannot tell if 50 took one month or four years." },
    { "id": "TEAM",     "status": "weak",    "reason": "Advantage cited belongs to unnamed outsiders — no person on the team is tied to anything unfair." },
    { "id": "ASK",      "status": "weak",    "reason": "Slogan with no action and no object — a convinced investor has nothing to click, book or reply to." }
  ]
}
```

Score 0.33 after three iterations is not a failure. It means the gate is blocked and the founder receives six shootable Director Notes — each with a verbatim line, a reusable framing, a delivery note, and an exact timeline position. No existing clipping or repurposing tool produces this from raw footage.

---

## End-to-end workflow

```text
raw takes
   |
   +--> Whisper timestamps + ffprobe + conservative visual classification
   |
   +--> A1 phrase candidates --------+
   +--> A0 motion catalog -----------+  (parallel)
                                      v
                                A2 Director
                                      v
                                A3 Strategist
                                      |
                           coverage.json + Director Notes
                                      |
                         required beats at least weak?
                              no --> record another take
                              yes
                                      v
                         A4 semantic chunk plan
                                      |
                    +-----------------+-----------------+
                    |                                   |
             A4 clean variant                  A4 motion variant
                                                + A6 + A6W
                    |                                   |
                    +---------- human review -----------+
                                      |
                            FFmpeg + Remotion render
                                      |
                         local measurements + A7 QC
                                      |
                   MP4 + JSON + SRT + FCPXML/OTIO/EDL
```

The web flow keeps the consequential choice explicit: each semantic chunk has a clean Version A and a Version B with exactly one motion graphic. A5 may recommend a variant, but review remains usable if A5 is unavailable.

## The autonomous Devin decision layer

COGNIFRAME uses nine specialized role contracts: A0, A1, A2, A3, A4, A5, A6, A6W, and A7. Every role is a Devin API session with a JSON Schema output contract, role/run tags, an ACU limit, and a session URL stored in `sessions.json`.

Autonomy is bounded by local evidence:

- **Schema repair:** malformed structured output is retried once with the validation error in context.
- **A2/A3 checks and balances:** A2 owns story order; A3 may harden content verdicts. Conflict resolution is deterministic and recorded in `debate_log`.
- **A4 self-correction:** a draft cut plan is checked locally for source identity, interval validity, beat order, expected beat, and total duration. If it fails, COGNIFRAME sends the concrete findings back into the same Devin session exactly once. The revised plan must pass or the workflow stops.
- **Hard resource bounds:** A4 context is reduced to relevant candidate windows and planned below 28,000 characters; the transport rejects any prompt at or above 30,000 characters before network access.
- **No hidden infinite loop:** semantic repair is capped at one feedback round. Timeout retry is also capped and timed-out sessions are terminated.

This is an autonomous layer rather than a sequence of prompts: Devin proposes and revises state; the orchestrator decides what may advance; executable media tooling realizes the accepted state.

For the full challenge-to-evidence mapping and the five-minute judging plan, see [docs/cognition-challenge.md](docs/cognition-challenge.md). The implementation-level flow is in [docs/architecture.md](docs/architecture.md), and motion safety is documented in [docs/motion-pack.md](docs/motion-pack.md).

## Safety and quality controls

- Required beats gate the final cut; the CLI can only bypass the gate through an explicit rough-cut confirmation or `--force`.
- Motion scenes are curated from an external pack; broken or unclassified scenes are excluded.
- Known full-bleed scenes render as insets. Unknown scenes are alpha-sampled at 25%, 50%, and 75% of their runtime and are re-rendered as insets when mean coverage exceeds 35%.
- `hero-stat-callout` requires a real numeric claim and readable color contrast.
- Structured motion receives at least 4.5 seconds when the beat permits, with slowed playback and a final hold.
- Subtitles are removed or split for every motion-overlay window, so captions never compete with the graphic.
- Local visual measurements are authoritative. A7 must report image-dependent checks without image evidence as `unmeasurable`, never `pass`.
- The human chooses the final variant per chunk; A5 is advisory and non-blocking.

## Outputs

| Artifact | Purpose |
|---|---|
| `final.mp4` | selected, rendered pitch video |
| `coverage.json` | beat state, Director Notes, debate log, project metadata |
| `edl.json` | accepted cut ranges and overlay manifest, including `visual_qc` when available |
| `sessions.json` | live Devin status, URLs, and bounded-revision findings |
| `subtitles.srt` | editable Whisper-derived captions |
| `timeline.fcpxml` | layered Final Cut Pro / DaVinci handoff |
| `timeline.otio` | OpenTimelineIO interchange |
| `timeline.edl` | CMX 3600 fallback |
| `chunks/` | Version A/B previews and per-chunk manifests |

The repository includes [demoeins.mp4](demoeins.mp4) as the checked-in demo input. The licensed external motion pack itself is intentionally not redistributed.

## Stack

| Layer | Implementation |
|---|---|
| Agent runtime | Devin API v3, with a safe legacy-v1 fallback when a separate compatible key is configured |
| State and orchestration | Python state machine + JSON Schema Draft 2020-12 |
| Transcription | OpenAI Whisper `whisper-1`, used only for ASR |
| Video execution | pinned `browser-use/video-use` helpers + FFmpeg/ffprobe |
| Motion | Remotion isolated per scene, ProRes 4444 alpha output |
| Product surface | FastAPI + a build-free single-page review UI |
| Handoff | MP4, SRT, FCPXML, OTIO, CMX 3600 EDL |

## Setup

Prerequisites: Python 3.11+, `uv`, FFmpeg/ffprobe, Node/npm, and credentials for Devin and Whisper. Motion Version B also needs a local `PALANTUM_TEMPLATE_SOURCE`.

```bash
uv sync
uv run palantum doctor --template-source /path/to/motion-pack
```

### Local `.env` mode

Use this only when the credentials should come from the repository-local `.env` file:

```bash
cp .env.example .env
# fill DEVIN_PAT or DEVIN_API_KEY, OPENAI_API_KEY, PALANTUM_TEMPLATE_SOURCE
uv run palantum serve
```

### Injected-environment mode

When a shell, CI system, or secret manager injects credentials, use the canonical launcher. It passes `--no-env-file` to `uv`, so a stale local `.env` cannot replace the process values before Palantum starts.

```powershell
.\scripts\serve.ps1 -VideosDir . -TemplateSource C:\path\to\motion-pack
```

```bash
./scripts/serve.sh --videos-dir . --template-source /path/to/motion-pack
```

Palantum's own loader only fills missing variables; it never overwrites an already injected value.

### CLI

```bash
uv run palantum --videos-dir ./demo ingest demoeins.mp4 --template-source /path/to/pack
uv run palantum --videos-dir ./demo status
uv run palantum --videos-dir ./demo cut --template-source /path/to/pack
uv run palantum --videos-dir ./demo export
```

## Verification

The current suite contains 138 collected tests covering API contracts, deterministic conflict rules, Devin transport and secret redaction, bounded A4 revision, prompt budgets, parallel chunk variants, motion safety, subtitle suppression, export formats, injected-environment precedence, and honest job-progress visibility.

```bash
uv run --no-env-file pytest
uv run --no-env-file ruff check .
uv run --no-env-file mypy palantum
git diff --check
```

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

## Scope boundaries

Implemented and demoable: seven-beat coverage analysis, Director Notes, A/B chunk production, bounded A4 repair, curated motion rendering, manual review, local media measurements, final QC, and layered export.

Not claimed: general-purpose timeline editing, unrestricted multi-step agent planning across every role, frame-understanding by A7 without supplied image data, redistribution of the external motion pack, or a fully unattended production decision after A/B review.
