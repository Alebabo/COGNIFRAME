from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

STATUSES = ("missing", "weak", "covered")


def coverage_score(beats: list[dict[str, Any]], schema: dict[str, Any]) -> float:
    """Return weighted coverage: covered=1, weak=.5, missing=0; required count double."""
    definitions = {beat["id"]: beat for beat in schema["beats"]}
    numerator = denominator = 0.0
    for beat in beats:
        weight = 2.0 if definitions[beat["id"]].get("required") else 1.0
        denominator += weight
        numerator += weight * {"missing": 0.0, "weak": 0.5, "covered": 1.0}[beat["status"]]
    return round(numerator / denominator, 4) if denominator else 0.0


def empty_state(schema: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema": schema["schema"],
        "meta": {
            "target_duration_s": schema["target_duration_s"],
            "format": schema["format"],
            "iteration": 0,
        },
        "beats": [
            {
                "id": b["id"],
                "status": "missing",
                "source": None,
                "range": None,
                "owner": "A2",
                "reason": "Noch kein Material analysiert.",
            }
            for b in schema["beats"]
        ],
        "director_notes": [],
        "resolved_notes": [],
        "debate_log": [],
        "brand": {
            "palette": ["#0B0B0B", "#FF5A00", "#6E6E6E"],
            "font": "Inter",
            "grade": "neutral_punch",
        },
        "coverage_score": 0.0,
    }


def load(edit_dir: Path, schema: dict[str, Any]) -> dict[str, Any]:
    path = edit_dir / "coverage.json"
    if not path.exists():
        return empty_state(schema)
    return cast(dict[str, Any], json.loads(path.read_text()))


def save(edit_dir: Path, state: dict[str, Any]) -> Path:
    edit_dir.mkdir(parents=True, exist_ok=True)
    path = edit_dir / "coverage.json"
    path.write_text(
        json.dumps(state, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    return path
