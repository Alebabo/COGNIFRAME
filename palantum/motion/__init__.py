"""Runtime integration for externally supplied Remotion template packs."""

from palantum.motion.catalog import CURATED_SCENES, build_scene_catalog
from palantum.motion.harness import build_render_command, materialize_scene

__all__ = [
    "CURATED_SCENES",
    "build_render_command",
    "build_scene_catalog",
    "materialize_scene",
]
