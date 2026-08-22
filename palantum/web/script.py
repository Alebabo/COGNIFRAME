from __future__ import annotations

import os
import re
import time
from collections.abc import Iterator

SYSTEM_PROMPT = """Du bist der Script-Editor von Palantum. Schreibe ein prägnantes Videoskript
für ungefähr 60 Sekunden. Antworte in der Sprache des Briefings. Gliedere den Text in HOOK,
PROBLEM, LÖSUNG, BEWEIS und CTA. Schreibe sprechbare Sätze, keine Regieerklärung. Erfinde keine
Zahlen, Kunden, Resultate oder Produktfunktionen. Markiere fehlende harte Fakten knapp in
eckigen Klammern. Gib ausschließlich das Skript aus."""


def _fallback_script(description: str) -> str:
    subject = " ".join(description.strip().split())
    return (
        f"HOOK\n{subject}\n\n"
        "PROBLEM\nZeige in einem konkreten Satz, welches Problem heute ungelöst bleibt.\n\n"
        "LÖSUNG\nErkläre den Ansatz so, dass man ihn ohne Vorwissen versteht.\n\n"
        "BEWEIS\n[Füge hier einen belegbaren Demo-Moment, ein Ergebnis "
        "oder Kundenfeedback ein.]\n\n"
        "CTA\n[Sage klar, was die Zuschauerinnen und Zuschauer als Nächstes tun sollen.]"
    )


def _word_chunks(text: str) -> Iterator[str]:
    for chunk in re.findall(r"\S+\s*", text):
        yield chunk
        time.sleep(0.012)


def create_script_stream(description: str) -> tuple[Iterator[str], str]:
    """Return a genuine model stream when configured, otherwise an honest local scaffold."""
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        return _word_chunks(_fallback_script(description)), "local"

    try:
        from openai import OpenAI

        response = OpenAI(api_key=api_key).chat.completions.create(
            model=os.getenv("PALANTUM_OPENAI_MODEL", "gpt-4o-mini"),
            temperature=0.4,
            stream=True,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": description},
            ],
        )
    except Exception:
        return _word_chunks(_fallback_script(description)), "local"

    def chunks() -> Iterator[str]:
        for event in response:
            if not event.choices:
                continue
            content = event.choices[0].delta.content
            if content:
                yield content

    return chunks(), "openai"
