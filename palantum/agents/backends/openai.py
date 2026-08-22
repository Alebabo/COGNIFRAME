from __future__ import annotations

import json
import os
from typing import Any


def call(
    role_id: str, prompt: str, context: dict[str, Any], schema: dict[str, Any], attempt: int = 0
) -> dict[str, Any]:
    from openai import OpenAI

    response = OpenAI(api_key=os.environ["OPENAI_API_KEY"]).chat.completions.create(
        model=os.getenv("PALANTUM_OPENAI_MODEL", "gpt-4o-mini"),
        temperature=0,
        messages=[
            {"role": "system", "content": prompt},
            {"role": "user", "content": json.dumps(context, ensure_ascii=False)},
        ],
        response_format={
            "type": "json_schema",
            "json_schema": {
                "name": f"palantum_{role_id.lower()}",
                "strict": True,
                "schema": schema,
            },
        },
    )
    content = response.choices[0].message.content
    if not content:
        raise ValueError(f"{role_id} returned an empty response on attempt {attempt + 1}")
    result = json.loads(content)
    if not isinstance(result, dict):
        raise ValueError(f"{role_id} response must be an object")
    return result
