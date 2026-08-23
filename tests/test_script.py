from __future__ import annotations

from unittest.mock import patch

import pytest

from pitchcraft.web.script import CanvasAgentUnavailableError, create_script_stream


def test_script_requires_devin_without_a_local_or_openai_fallback() -> None:
    with (
        patch.dict("os.environ", {}, clear=True),
        pytest.raises(CanvasAgentUnavailableError, match="Devin is not configured"),
    ):
        create_script_stream("Our product explains video production.")
