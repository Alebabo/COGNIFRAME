from __future__ import annotations

import json
from pathlib import Path

from palantum.web.app import state_payload


def test_state_payload_maps_done_and_resolved_notes(tmp_path: Path) -> None:
    edit = tmp_path / "edit"
    edit.mkdir()
    (tmp_path / "take.mp4").write_bytes(b"source")
    (edit / "final.mp4").write_bytes(b"render")
    (edit / "coverage.json").write_text(
        json.dumps(
            {
                "meta": {"sources": [str(tmp_path / "take.mp4")]},
                "beats": [{"id": "HOOK", "status": "covered", "reason": "clear"}],
                "director_notes": [],
                "resolved_notes": [{"beat": "DEMO", "closed_by": "take_demo"}],
                "coverage_score": 0.5,
            }
        )
    )

    result = state_payload(tmp_path)

    assert result["phase"] == "done"
    assert result["video_url"] == "/api/video"
    assert result["export_url"] == "/api/export"
    assert result["notes"][0]["resolved"] is True
