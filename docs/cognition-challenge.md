# COGNIFRAME — Cognition Challenge Dossier

## Executive case

The Cognition track asks teams to build an autonomous layer on top of Devin, select a domain whose work can be expressed through code, and execute the workflow through Devin API sessions.

COGNIFRAME satisfies that brief in pitch-video production. A pitch is represented as executable state: timestamped source material, a seven-beat schema, validated editorial decisions, Remotion scene props, FFmpeg render instructions, quality findings, and NLE timeline exports. Devin sessions make the semantic decisions; deterministic code measures, validates, executes, and records them.

The differentiator is not another automatic cut. It is a closed production loop:

> diagnose a missing investor argument → issue a shootable Director Note → ingest the new take → produce alternatives → verify the render.

## 1. Official challenge alignment

| Challenge requirement | COGNIFRAME implementation | Demo evidence |
|---|---|---|
| Autonomous layer on top of Devin | A deterministic orchestrator coordinates nine Devin role contracts and advances only validated state. A4 can revise its own invalid decision once inside the same session. | `sessions.json` changes from `running` to `revising` to `done`; the session URL and local findings remain visible. |
| Domain or industry expressible through code | Startup pitch-video production is encoded as JSON manifests and executed by Python, FFmpeg, Remotion, and NLE exporters. | Show `coverage.json`, one chunk manifest, then the resulting preview/final video. |
| Devin API sessions execute the workflow | Every semantic role uses Devin v3 or a credential-safe legacy-v1 transport, structured output schemas, tags, ACU limits, polling, timeout termination, and session URLs. | Open one real A2 or A4 session from the live status UI. |
| Meaningful autonomy | A1/A0 run in parallel; A2 and A3 form a checks-and-balances stage; A4 produces and, if necessary, repairs a cut plan; A6/A6W create a motion variant; A7 evaluates measured output. | Show the state transition and one rejected/accepted decision, not a static chatbot response. |
| Effective use of Entire | Entire is enabled on `main`; the branch has a substantial checkpoint trail. Historic intent for prompt hardening, motion QC, pacing, and the current documentation was recovered with the repository's Entire skills. | Show `entire checkpoint list`, then explain one implementation decision from its checkpoint. |

The source materials provide no more specific Cognition scoring rubric than the requirements above. Therefore the pitch should make four qualities obvious: autonomy, real Devin API use, a code-expressible domain, and a complete working loop.

## 2. Product thesis

Founders usually discover missing footage after recording has ended. A normal editor can shorten pauses or rearrange clips, but cannot manufacture a credible demonstration, traction claim, team advantage, or call to action from absent evidence.

COGNIFRAME moves intelligence earlier in the production process. It evaluates seven investor-relevant beats—HOOK, PROBLEM, SOLUTION, DEMO, TRACTION, TEAM, and ASK—and returns Director Notes with:

- the exact line to record;
- a framing instruction grounded in the available setup;
- a target duration;
- a delivery direction;
- the beat and expected impact.

Once coverage is sufficient, the same state continues into cutting, motion, review, QC, and export. That continuity is the product advantage: diagnosis and execution share one manifest instead of becoming disconnected chat output.

## 3. What is autonomous—and what is deterministic

### Devin owns semantic judgment

- A1 Script Supervisor finds phrase candidates and speech slips.
- A0 Template Scout classifies safe motion scenes.
- A2 Director assigns coverage and writes Director Notes.
- A3 Strategist challenges content hardness.
- A4 Cutter chooses exact ranges and chunk structure.
- A5 Variant Supervisor recommends Version A or B without blocking review.
- A6 Graphics Director chooses scene, beat, and timing.
- A6W Slot Worker fills template props without inventing claims.
- A7 QC interprets measured output and may veto delivery.

### Local code owns control and evidence

- JSON Schema validation rejects malformed role outputs.
- The orchestrator enforces conflict rules, gate conditions, retry limits, and cache validity.
- ffprobe measures source and output properties.
- FFmpeg performs rendering, audio analysis, alpha sampling, and timeline views.
- Remotion renders isolated motion slots.
- Visual classification is deliberately conservative and local.
- Exporters create SRT, FCPXML, OTIO, and CMX 3600 artifacts.

This separation makes the autonomy credible: the agent is allowed to judge meaning, but is not allowed to redefine physics, thresholds, file identity, or success after the fact.

## 4. The bounded multi-step reasoning layer

The deepest autonomous decision loop is A4. It is intentionally short enough for a reliable live demo:

1. COGNIFRAME sends a beat-local, budgeted context to an A4 Devin session.
2. Devin returns a structured cut decision.
3. Local code verifies known source files, positive intervals, beat order, expected chunk beat, and total duration.
4. If any check fails, the system sends the concrete findings back to the same Devin session.
5. Devin returns the complete corrected structure.
6. The corrected result is revalidated. It advances or fails clearly; there is no third round.

`sessions.json` records `reasoning_steps: 2` and `validation_findings` when the correction path is used. This turns “the model reasoned” into an inspectable state transition.

Two other retry classes remain separate:

- schema-invalid output may start one fresh attempt with the schema error in context;
- a timed-out session is terminated and may be retried once.

The project does not claim an open-ended autonomous planning framework. The bounded design is a feature: it controls ACU cost, stage timing, and failure behavior.

## 5. Engineering evidence

### Structured, observable Devin sessions

The transport serializes the role prompt and input JSON, refuses requests at 30,000 characters or more before any network call, supplies a structured output schema, tags each run and role, applies per-role ACU limits, and saves the resulting session URL. v3 credentials are never sent to a v1 endpoint; a v1 fallback is only allowed through a separately configured legacy-compatible key.

### Prompt-budget incident converted into a product guardrail

A real A4 request previously exceeded Devin's 30,000-character limit because noisy floating-point timestamps inflated otherwise useful context. The current implementation:

- rounds timestamps only in the agent context;
- preserves stored transcripts unchanged;
- includes phrase and word data only around candidate windows with a ±0.5-second margin;
- retains the Director selection and at most three unique alternatives per beat;
- drops alternatives deterministically from the back when necessary;
- plans at 28,000 characters and hard-fails locally below the transport ceiling.

This is valuable judging evidence because it shows the team learned from a real API boundary and converted it into deterministic orchestration.

### Motion graphics that cannot hide the founder

Version B must contain exactly one genuine motion graphic. Safety is enforced after the agent decision:

- curated full-bleed scenes such as `hero-stat-callout` are marked `presentation: inset`;
- the inset card occupies at most 52% of frame width and 48% of frame height with a 4% margin;
- unknown scenes are alpha-sampled at three points; coverage above 35% triggers one inset re-render;
- a still-opaque result is rejected;
- numeric hero scenes require exactly one parseable number and at least 4.5:1 contrast against `#171717`;
- structured scenes receive a minimum 4.5-second viewing window when the beat allows;
- subtitles are split or removed across the overlay window.

Every overlay can carry `visual_qc.presentation`, `max_alpha_coverage`, `contrast_ratio`, and `verdict`. Local failures override a blind A7 `pass`; without visual evidence A7 must say `unmeasurable`.

### Human control at the consequential boundary

Autonomy does not remove editorial ownership. The system produces two variants for every chunk and asks the user to choose. A5 is an asynchronous recommendation, not an automatic irreversible decision. This is the right boundary for a creator tool: agents do the expensive analysis and production work, while the creator owns taste and final approval.

## 6. Five-minute judging plan

### 0:00–0:35 — Problem and hook

“An editor can only cut what you recorded. If your traction proof or product demo is missing, AI clipping tools discover the problem too late. COGNIFRAME is the autonomous Devin director that finds the missing shot while you can still record it.”

Show the incomplete input and start the workflow.

### 0:35–1:15 — Challenge fit

Show the architecture as three layers:

1. Devin sessions make semantic production decisions.
2. The COGNIFRAME state machine validates and coordinates them.
3. FFmpeg and Remotion execute the accepted plan.

Say explicitly: “This is our autonomous layer on top of Devin; video production is the code-expressible domain.”

### 1:15–2:10 — Live autonomy proof

Show live role statuses and one session URL. Focus on A4's state transition:

- first structured proposal;
- local validation finding;
- one feedback message to the same session;
- accepted corrected plan.

Do not show a wall of prompts. Show the before/after manifest and the fact that the system, not a presenter, decided whether work could advance.

### 2:10–3:05 — Product reveal

Show a Director Note for one missing beat. If stage conditions allow, record or reveal the requested take. The note should already be visible before the take so it cannot look staged.

Use the line: “We do not just edit the video. We direct the next piece of evidence.”

### 3:05–4:10 — Safe variants and final output

Show clean Version A beside motion Version B. Point out that motion is inset, stays long enough to read, and suppresses subtitles during its window. Select a variant, finalize, and play the prepared final output if live rendering is still running.

### 4:10–4:40 — Engineering credibility

Show the compact proof list:

- structured Devin sessions with ACU limits and traceable URLs;
- deterministic prompt and motion guardrails;
- 140 collected tests;
- layered NLE export;
- Entire checkpoint trail on `main`.

### 4:40–5:00 — Close

“COGNIFRAME turns Devin from a software engineer into a bounded production studio: it reasons about the story, asks for missing evidence, executes the edit, and proves why the result is safe to ship.”

## 7. Judge Q&A

### Why does this require Devin instead of one LLM call?

The workflow persists across specialized decisions, parallel sessions, structured contracts, one same-session repair, ACU budgets, timeouts, and observable session URLs. A one-shot call could draft JSON; it would not provide the same role isolation, lifecycle, correction trace, or operational control.

### Is it really autonomous if the user chooses A or B?

Yes. The autonomous layer performs diagnosis, planning, variant construction, measurement, and recommendation. Human selection is a deliberate approval gate for taste, not manual editing. The user never builds a timeline or fixes timestamps.

### Does A7 see the finished frames?

Not by itself. Image-dependent claims without image data are `unmeasurable`. Local FFmpeg alpha measurements and render facts are authoritative and can force the final verdict to fail. This is an explicit limitation, not hidden confidence theater.

### What happens when Devin returns an invalid edit?

The local validator returns concrete findings to the same A4 session exactly once. A second invalid result fails before rendering. Schema errors and timeout retries are separately bounded.

### What prevents runaway cost or a broken stage demo?

Per-role ACU limits, prompt budgets, bounded feedback, bounded timeout retry, reusable validated role caches, parallel work, a manual review boundary, and a prepared final-video fallback.

### Are the motion templates part of the repository?

No. The external pack is supplied locally and is not redistributed. COGNIFRAME stores a hash-based curated catalog and isolated render harnesses.

## 8. Honest scope and remaining risks

### Implemented

- Devin-only semantic agent backend;
- seven-beat coverage and shootable Director Notes;
- parallel A0/A1 analysis and deterministic A2/A3 conflict resolution;
- budgeted A4 plans and one same-session semantic repair;
- A/B chunk production with exactly one safe motion graphic in Version B;
- subtitle suppression during motion;
- optional non-blocking A5 recommendation;
- local audio, duration, and motion measurements plus A7 final QC;
- web review, final render, and layered export;
- safe local and injected-environment startup modes.

### Not claimed

- open-ended reasoning loops across all roles;
- A7 frame understanding without real image input;
- a general-purpose NLE or timeline UI;
- unrestricted template generation;
- automatic replacement of the human's final taste decision;
- redistribution rights for the external motion source.

### Demo risks and mitigations

| Risk | Mitigation |
|---|---|
| Network or session delay | Start the live run immediately; keep a verified final MP4 ready. |
| API key precedence | Use the canonical `--no-env-file` launcher for injected credentials. |
| Prompt overflow | 28,000-character plan budget and local `<30,000` transport guard. |
| Motion hides footage | Inset curation, alpha measurement, one re-render, then rejection. |
| A7 overclaims vision | `unmeasurable` contract plus authoritative local measurements. |
| Live take is weak | Use a generic, stage-shootable Director Note and allow a second take. |

## 9. Entire development trail

At documentation audit time, `entire checkpoint list` reported 25 checkpoints on `main`. The most relevant canonical checkpoints are:

- `01M0PBFZNVRKHRYEQKEJS2AC9W` — A4 prompt budget, motion overlay QC, and safe environment launchers;
- `01M0PD430R24XK43W30XZT642Y` — bounded Devin reasoning, motion pacing, and subtitle suppression;
- `01M0PA0FGAZQSPSJA1RY0BA6B5` — live agent status notifications;
- `01M0P6CAYGXVVRPX7Z05Q7ZC3Z` — unified Palantum/COGNIFRAME integration.

The checkpoint trail is not decorative. It preserves the decisions behind the current constraints—for example why Version B still requires real motion after a full-bleed overlay bug, and why the A4 repair loop is capped at one round.

## 10. Verification commands

```bash
uv run --no-env-file pytest
uv run --no-env-file ruff check .
uv run --no-env-file mypy palantum
git diff --check
```

The current test inventory contains 140 tests. For judging, report the result of the final executed run rather than implying that collection alone proves runtime success.

## 11. Source basis

This dossier reconciles:

- the supplied Cognition challenge opening document;
- the event requirement to use Entire and present a five-minute live demo plus Q&A;
- the original product PRD and pitch script;
- current repository code and tests;
- the Entire checkpoint history attached to the implementation commits.

Where an older document conflicts with the implementation, the code, tests, and current checkpoint intent win. Roadmap ideas remain labeled as such; they are not presented as demoable functionality.
