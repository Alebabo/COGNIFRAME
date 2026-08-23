# External motion pack

```powershell
python scripts/setup_engine.py
pitchcraft doctor --template-source "C:\path\to\templates.zip"
pitchcraft ingest take.mp4 --template-source "C:\path\to\templates.zip"
pitchcraft cut --template-source "C:\path\to\templates.zip"
```

The template path can instead be supplied once as `PITCHCRAFT_TEMPLATE_SOURCE`. Production agent
decisions require `DEVIN_API_KEY`; `DEVIN_SNAPSHOT_ID` is optional.

PitchCraft does not redistribute the supplied Locomotion template sources. At runtime,
`pitchcraft.motion.build_scene_catalog()` reads either the original ZIP or an extracted directory,
hashes the complete source, and writes a cached `edit/scene-catalog.json` containing only the eight
curated scene descriptions and slot defaults.

`pitchcraft.motion.materialize_scene()` copies only the selected component and its metadata into
`edit/animations/slot_<scene-id>/`. The generated directory is an isolated Remotion project with
validated props and exact dependency versions. Install its npm dependencies inside the Devin
snapshot, then execute the list returned by `build_render_command()` to request a ProRes 4444 MOV.

The built-in parser is a fast source-integrity gate, not a replacement for a Remotion build. Every
render is sampled at 25%, 50%, and 75% of its duration and its alpha coverage is measured with
FFmpeg. A composition that covers more than 35% of the delivery frame is rendered once more as a
transparent, top-right inset; it is rejected if it remains too opaque. Known full-bleed scenes are
catalogued as insets from the start. The generated harness always renders at the detected delivery
orientation, so landscape delivery remains 1920x1080 and portrait delivery is 1080x1920.

Scenes containing charts, flows, UI, cards, or other shapes are catalogued as `structured`. PitchCraft
keeps them visible for at least 4.5 seconds when their beat permits, slows their animation when extra
time is available, and preserves roughly one second on the final frame. Burned-in subtitles are
removed only for the active motion intervals and return immediately afterwards.

Scene-specific prop checks run before rendering. In particular, `hero-stat-callout` is available
only for timeline quotes with a numeric claim, its `heroValue` must contain one number understood by
the template, and its background must maintain at least 4.5:1 contrast against the fixed text color.
The resulting alpha, contrast, layout, and prop measurements are stored in each overlay's
`visual_qc` block and are merged into final A7 QC as authoritative local findings.

The pack README claims MIT licensing while also describing redistribution restrictions, and the
archive has no license file. Keep the original pack outside the repository until provenance and
redistribution rights have been verified.
