"""Runtime integration for externally supplied Remotion template packs."""

from pitchcraft.motion.catalog import CURATED_SCENES, build_scene_catalog
from pitchcraft.motion.harness import build_render_command, materialize_scene

__all__ = [
    "CURATED_SCENES",
    "build_render_command",
    "build_scene_catalog",
    "materialize_scene",
]
