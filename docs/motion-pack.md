# External motion pack

```powershell
python scripts/setup_engine.py
palantum doctor --template-source "C:\path\to\templates.zip"
palantum ingest take.mp4 --template-source "C:\path\to\templates.zip"
palantum cut --template-source "C:\path\to\templates.zip"
```

The template path can instead be supplied once as `PALANTUM_TEMPLATE_SOURCE`. Production agent
decisions require `DEVIN_API_KEY`; `DEVIN_SNAPSHOT_ID` is optional.

Palantum does not redistribute the supplied Locomotion template sources. At runtime,
`palantum.motion.build_scene_catalog()` reads either the original ZIP or an extracted directory,
hashes the complete source, and writes a cached `edit/scene-catalog.json` containing only the eight
curated scene descriptions and slot defaults.

`palantum.motion.materialize_scene()` copies only the selected component and its metadata into
`edit/animations/slot_<scene-id>/`. The generated directory is an isolated Remotion project with
validated props and exact dependency versions. Install its npm dependencies inside the Devin
snapshot, then execute the list returned by `build_render_command()` to request a ProRes 4444 MOV.

The built-in parser is a fast source-integrity gate, not a replacement for a Remotion build or
probe render. A scene must still be rendered and visually checked before production use. ProRes
4444 supports alpha, but a template that paints an opaque background remains opaque. The supplied
templates are 1920x1080 while Palantum's pitch schema is 1080x1920; placement or adaptation must be
explicit rather than silently cropping the composition. The generated harness renders at the
detected delivery orientation and centers the original scene with a contain transform. Landscape
delivery remains 1920x1080; portrait delivery is 1080x1920 with transparent space around the fitted
scene wherever the selected template itself does not paint a background.

## Known limitation: full-bleed scenes as variant B overlays

A validated run with two real takes selected `hero-stat-callout` for the single motion slot of a
variant B chunk. That scene paints a full-bleed background, so the rendered ProRes 4444 MOV is
opaque across the whole frame and hides the A-roll for its entire five second duration. The slot
props chosen by the agent compounded it: `bgColor` `#0B0B0B` behind the scene's fixed `#171717`
text made the copy unreadable, and a non-numeric `heroValue` (`"?"`) falls through the template's
count-up parser and renders as `0`. A7 still reported `overlay alignment: pass`, because the QC
role cannot see rendered frames and only checks timing.

Curated scene selection therefore needs either a restriction to non-full-bleed scenes, contrast and
value constraints on the slot props, or explicit partial-frame placement. Until then, treat variant
B previews as requiring a human visual check.

The pack README claims MIT licensing while also describing redistribution restrictions, and the
archive has no license file. Keep the original pack outside the repository until provenance and
redistribution rights have been verified.
