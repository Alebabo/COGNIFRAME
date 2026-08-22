from __future__ import annotations

from unittest.mock import patch

from palantum.web.script import create_script_stream


def test_script_falls_back_to_an_honest_local_scaffold_without_api_key() -> None:
    with patch.dict("os.environ", {}, clear=True):
        chunks, source = create_script_stream("Unser Produkt erklärt die Videoproduktion.")
        script = "".join(chunks)

    assert source == "local"
    assert "HOOK" in script
    assert "BEWEIS" in script
    assert "[" in script
