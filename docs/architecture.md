# Palantum — P0 architecture

Palantum is a director layer on top of [`browser-use/video-use`](https://github.com/browser-use/video-use).
video-use does the cutting; Palantum decides what the video should be, what is missing from the
footage, and hands the project back to the user in separable tracks.

## Layout

```
palantum/
  cli.py               # terminal loop: palantum ingest / status / cut / export
  orchestrator.py      # deterministic state machine; owns conflict resolution, no LLM
  state.py             # coverage.json read/write + validation
  schemas/
    yc_pitch_60s.json  # beat schema (swappable — this is the expansion path)
    roles.json         # structured output schema per role
  agents/
    runner.py          # Devin-only role invocation + schema validation
    backends/          # devin.py (Devin API sessions)
    prompts/*.md       # role prompts (A0-A7, including A5 variant review)
  motion/              # external-pack catalog + isolated Remotion slot harnesses
  engine/
    videouse.py        # wrapper around vendored video-use helpers
    transcribe.py      # Whisper word-level ASR in Scribe JSON shape
  export/
    package.py         # §7 export package: stems + FCPXML + OTIO + CMX3600
  web/                 # FastAPI + single white page (states 3 -> 2 -> 1)
vendor/video-use/      # cloned by scripts/setup_engine.py, pinned, not committed
```

Session outputs live in `<videos_dir>/edit/` only — never inside the repo, never inside
`vendor/video-use/`.

## The loop

```
ingest(file) -> transcribe (cached) -> pack -> A1 -> A2 <-> A3 -> coverage.json
                                  \-> A0 template scout (parallel) -> scene-catalog.json
                                                        |
                             all required beats >= weak? no -> stop, print Director Notes
                                                        yes
                                                         v
                         CLI: A4 -> A6 -> parallel A6.x slots -> render.py -> A7
                         Web: A4 semantic chunks
                                  |
                                  +-> parallel A4.A / A4.B per chunk
                                      (B requires A6 + parallel A6.x; exactly one overlay)
                                  |
                                  v
                         optional A5 recommendation + two previews per chunk
                                  |
                         user selects one variant per chunk
                                  |
                                  v
                         combined edl.json -> render.py -> A7 -> final.mp4
```

Conflict rules, enforced by the orchestrator and not negotiable by any agent:

| Conflict | Winner |
|---|---|
| Structure / beat order | A2 Director |
| Content hardness of a beat (`weak` vs `covered`) | A3 Strategist |
| Take selection inside a beat | A4 Cutter |
| Delivery | A7 QC has veto, no content vote |

A2/A3 results are resolved mechanically with the rule above and the outcome is appended to
`debate_log` in `coverage.json`.

## Agent backends

Every role is one Devin session with a JSON schema from `schemas/roles.json`; the orchestrator
never parses prose.

It creates one session per role (and one per A6.x motion slot) with
`structured_output_schema`, `tags` (`run-<n>`, role), and `max_acu_limit`. Session URLs are written
atomically to `sessions.json` and surfaced in the UI. OpenAI is reserved for Whisper
transcription; it is not an agent backend.

## Web API contract

`palantum/web/static/index.html` is the whole frontend: one white page, four states
(empty → working → review → done), no build step. It renders from one endpoint and polls it every 1.5 s.
`?mock=work|review|done|empty` renders the same page against checked-in fixture files instead of the
server, which is how the page is developed and verified without a backend.

```
GET  /api/state -> {
  "phase": "empty" | "working" | "review" | "done",
  "coverage": { "score": 0.57, "beats": [{"id": "HOOK", "status": "covered", "reason": "..."}] },
  "notes":    [{"beat", "impact", "resolved", "why", "shot": {"line", "framing", "duration_s", "delivery"}}],
  "sessions": [{"role": "Director", "status": "idle"|"running"|"done", "url": null | "<devin session url>"}],
  "video_url":  null | "/api/video",
  "export_url": null | "/api/export",
  "chunks": [{
    "id": "chunk-00-hook", "beat": "HOOK", "selected": null | "a" | "b",
    "recommendation": null | {"status", "variant_id", "reason"},
    "variants": [{"id": "a" | "b", "name", "strategy", "video_url"}]
  }],
  "selection_complete": false
}
POST /api/canvas      {"title", "text", "beats", "attached_videos"} -> atomic local save
GET  /api/devin/config -> configuration status only
POST /api/devin/orchestrate -> anchored A2/A3/A1 contributions
POST /api/canvas/assist -> one anchored agent contribution
POST /api/upload      multipart: files[] (+ optional brief) -> analyze, build A/B chunks
POST /api/chunks/{chunk_id}/selection  {"variant_id":"a"|"b"}
POST /api/chunks/recommendations/apply -> atomically apply every valid A5 recommendation
GET  /api/chunks/{chunk_id}/variants/{variant_id}/video
POST /api/finalize    combine the selected variants and start the final render
GET  /api/video       final.mp4
GET  /api/export      the §7 package as a zip
```

`notes` contains open *and* resolved notes; the page strikes the resolved ones through instead of
removing them, because the visible closing of gaps is the product.

## Deviations from the PRD, and why

- **ASR is OpenAI Whisper with word-level timestamps, not ElevenLabs Scribe** — no ElevenLabs key
  is available. The adapter emits Scribe-shaped JSON (`words[]` with `type`, `speaker_id` and
  synthetic `spacing` entries) so every video-use helper works unmodified. Cost: no diarization
  and no audio-event tags, and fillers are partially normalized, which weakens the Cutter's slip
  signal. Swapping back to Scribe is one module.
- **A8 Delivery is a script, not an agent** (PRD §5.4).
