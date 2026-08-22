from __future__ import annotations

import os
import re
import time
from collections.abc import Iterator
from typing import Any

SYSTEM_PROMPT = """Du bist der Script-Editor von Palantum. Schreibe ein prägnantes Videoskript
für ungefähr 60 Sekunden. Antworte in der Sprache des Briefings. Gliedere den Text in
HOOK, PROBLEM, SOLUTION, DEMO, TRACTION, TEAM und ASK. Schreibe sprechbare Sätze.
Erfinde keine Zahlen, Kunden oder Funktionen. Markiere fehlende Fakten knapp in
eckigen Klammern. Gib ausschließlich das Skript aus."""

BEAT_KEYS = ("HOOK", "PROBLEM", "SOLUTION", "DEMO", "TRACTION", "TEAM", "ASK")

BEAT_DEFAULTS = {
    "HOOK": {
        "title": "Hook",
        "time": "3–6s",
        "description": "Erste 1.5s mit einer konkreten, packenden Aussage. Keine Begrüßung.",
        "placeholder": "Zwei Stunden Fehlersuche werden mit Palantum zu zwei Minuten...",
    },
    "PROBLEM": {
        "title": "Problem",
        "time": "6–10s",
        "description": "Wer leidet unter dem Schmerz und was kostet es wirklich?",
        "placeholder": "Entwickler verlieren täglich wertvolle Zeit durch fehlerhafte Tests...",
    },
    "SOLUTION": {
        "title": "Lösung",
        "time": "8–14s",
        "description": "Der konkrete Mechanismus – wie funktioniert die Lösung genau?",
        "placeholder": "Unsere Engine analysiert Testläufe und isoliert die genaue Ursache...",
    },
    "DEMO": {
        "title": "Demo & Visuals",
        "time": "8–15s",
        "description": "Sichtbares Produktmaterial oder UI-Walkthrough.",
        "placeholder": "Ein Klick öffnet direkt den passenden Fix im Code...",
    },
    "TRACTION": {
        "title": "Traction & Zahlen",
        "time": "4–8s",
        "description": "Mindestens eine konkrete Zahl mit klarem Zeitbezug.",
        "placeholder": "In den letzten 3 Monaten haben 120 Teams unsere Beta genutzt...",
    },
    "TEAM": {
        "title": "Team",
        "time": "3–6s",
        "description": "Namen und der entscheidende 'Unfair Advantage'.",
        "placeholder": "Wir haben zuvor 6 Jahre die Testinfrastruktur bei Google skaliert...",
    },
    "ASK": {
        "title": "Call to Action",
        "time": "3–5s",
        "description": "Klarer Handlungsaufruf mit konkretem Ziel.",
        "placeholder": "Starte jetzt die kostenlose Developer Preview auf palantum.dev.",
    },
}


def parse_canvas_beats(text: str) -> dict[str, str]:
    """Parse unstructured or tagged canvas text into discrete beat sections."""
    lines = text.splitlines()
    beats: dict[str, list[str]] = {b: [] for b in BEAT_KEYS}
    current_beat: str | None = None

    pattern = re.compile(
        r"^(?:(?:0?[1-7][.\s:]+)?(?:\[)?(HOOK|PROBLEM|SOLUTION|LÖSUNG|DEMO|BEWEIS|TRACTION|ZAHLEN|TEAM|ASK|CTA)(?:\])?[\s:]*)(.*)$",
        re.IGNORECASE,
    )

    alias_map = {
        "LÖSUNG": "SOLUTION",
        "BEWEIS": "DEMO",
        "ZAHLEN": "TRACTION",
        "CTA": "ASK",
    }

    for line in lines:
        match = pattern.match(line.strip())
        if match:
            raw_key = match.group(1).upper()
            canonical_key = alias_map.get(raw_key, raw_key)
            if canonical_key in beats:
                current_beat = canonical_key
                rest = match.group(2).strip()
                if rest:
                    beats[current_beat].append(rest)
                continue

        if current_beat:
            beats[current_beat].append(line)
        else:
            if line.strip():
                # Default first unstructured content to HOOK
                current_beat = "HOOK"
                beats["HOOK"].append(line)

    return {b: "\n".join(lines).strip() for b, lines in beats.items()}


def evaluate_canvas(
    beats: dict[str, str], attached_videos: dict[str, str] | None = None
) -> list[dict[str, Any]]:
    """Analyze canvas beats and generate actionable recommendations from A2 and A3."""
    recs: list[dict[str, Any]] = []
    attached = attached_videos or {}

    # 1. Check HOOK
    hook = beats.get("HOOK", "").strip()
    if not hook:
        recs.append({
            "id": "rec-hook-missing",
            "agent": "A2",
            "role": "Director",
            "beat": "HOOK",
            "type": "missing",
            "missing_item": "Hook & Nutzenversprechen",
            "message": "Der Hook fehlt noch. Starte mit einer starken These.",
            "ghost_text": "Zwei Stunden Fehlersuche werden zu zwei Minuten.",
            "target_pattern": "",
        })
    elif any(g in hook.lower() for g in ["hallo", "hi ", "herzlich willkommen", "mein name ist"]):
        clean_hook = re.sub(
            r"^(hallo|hi|herzlich willkommen[,\s]*|mein name ist[^\.]*\.)\s*",
            "",
            hook,
            flags=re.IGNORECASE,
        ).strip()
        first_cap = clean_hook.capitalize() if clean_hook else "Wir automatisieren den Ablauf."
        recs.append({
            "id": "rec-hook-greeting",
            "agent": "A3",
            "role": "Strategist",
            "beat": "HOOK",
            "type": "missing",
            "missing_item": "Direkter Nutzenstart ohne Floskeln",
            "message": "Begrüßungen in den ersten 2s kosten Zuschauer. Starte mit dem Nutzen.",
            "ghost_text": first_cap,
            "target_pattern": r"^(hallo|hi|herzlich willkommen[,\s]*|mein name ist[^\.]*\.)\s*",
        })

    # 2. Check PROBLEM
    problem = beats.get("PROBLEM", "").strip()
    if not problem:
        recs.append({
            "id": "rec-problem-missing",
            "agent": "A2",
            "role": "Director",
            "beat": "PROBLEM",
            "type": "missing",
            "missing_item": "Konkreter Schmerzpunkt",
            "message": "Definiere das konkrete Problem: Wer verliert Zeit oder Geld?",
            "ghost_text": "Entwickler verlieren täglich wertvolle Zeit durch manuelle Schritte.",
            "target_pattern": "",
        })
    elif len(problem.split()) < 4:
        recs.append({
            "id": "rec-problem-short",
            "agent": "A3",
            "role": "Strategist",
            "beat": "PROBLEM",
            "type": "missing",
            "missing_item": "Präzisierung des Schmerzpunkts",
            "message": "Das Problem ist noch abstrakt. Benenne den Schmerzpunkt präziser.",
            "ghost_text": f"{problem} – wodurch Teams jede Woche Stunden verlieren.",
            "target_pattern": problem,
        })

    # 3. Check SOLUTION
    solution = beats.get("SOLUTION", "").strip()
    if not solution:
        recs.append({
            "id": "rec-sol-missing",
            "agent": "A2",
            "role": "Director",
            "beat": "SOLUTION",
            "type": "missing",
            "missing_item": "Lösungsmechanismus",
            "message": "Erkläre den Mechanismus deiner Lösung (nicht nur Adjektive).",
            "ghost_text": "Unsere Technologie isoliert die Fehlerursache automatisch in Echtzeit.",
            "target_pattern": "",
        })

    # 4. Check DEMO
    has_demo_video = bool(attached.get("DEMO"))
    if not has_demo_video:
        recs.append({
            "id": "rec-demo-video",
            "agent": "A2",
            "role": "Director",
            "beat": "DEMO",
            "type": "missing",
            "missing_item": "Visuelles Demo-Material",
            "message": "Für DEMO wird visuelles Produktmaterial benötigt (Screen-Recording).",
            "ghost_text": "[Hier Screen-Recording oder UI-Demo anhängen]",
            "target_pattern": "",
        })

    # 5. Check TRACTION
    traction = beats.get("TRACTION", "").strip()
    if not traction or not re.search(r"\d+", traction):
        recs.append({
            "id": "rec-trac-numbers",
            "agent": "A3",
            "role": "Strategist",
            "beat": "TRACTION",
            "type": "missing",
            "missing_item": "Harte Kennzahlen & Traction",
            "message": "Traction ohne Zahlen wirkt schwach. Nenne messbare Nutzerzahlen.",
            "ghost_text": "Über 100 Teams nutzen die Lösung bereits produktiv im Alltag.",
            "target_pattern": "",
        })

    # 6. Check ASK
    ask = beats.get("ASK", "").strip()
    if not ask:
        recs.append({
            "id": "rec-ask-missing",
            "agent": "A1",
            "role": "Supervisor",
            "beat": "ASK",
            "type": "missing",
            "missing_item": "Call to Action (CTA)",
            "message": "Schließe mit einem klaren Call to Action ab.",
            "ghost_text": "Teste die Beta jetzt kostenlos auf unserer Website.",
            "target_pattern": "",
        })

    return recs


def _fallback_script(description: str) -> str:
    subject = " ".join(description.strip().split())
    return (
        f"01 HOOK\n{subject}\n\n"
        "02 PROBLEM\n"
        "Entwickler und Teams verlieren täglich wertvolle Zeit.\n\n"
        "03 SOLUTION\n"
        "Unsere Software automatisiert den gesamten Ablauf mit intelligenten Mechanismen.\n\n"
        "04 DEMO / BEWEIS\n"
        "[Füge hier einen belegbaren Demo-Moment, ein Ergebnis oder Kundenfeedback ein.]\n\n"
        "05 TRACTION\n"
        "In den letzten 3 Monaten haben bereits über 100 Teams die Lösung im Einsatz.\n\n"
        "06 TEAM\n"
        "Wir verfügen über langjährige Erfahrung im Aufbau skalierbarer Plattformen.\n\n"
        "07 ASK / CTA\n"
        "[Sage klar, was die Zuschauerinnen und Zuschauer als Nächstes tun sollen.]"
    )


def _word_chunks(text: str) -> Iterator[str]:
    for chunk in re.findall(r"\S+\s*", text):
        yield chunk
        time.sleep(0.015)


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


def create_agent_assist_stream(
    agent_id: str, beat_id: str | None, current_text: str, brief: str
) -> tuple[Iterator[str], str]:
    """Generate live interactive assistance or autocomplete from an Entire/Palantum agent."""
    api_key = os.getenv("OPENAI_API_KEY")
    agent_roles = {
        "A2": "Du bist A2 (Director). Gib präzise Regieanweisungen für den Schnitt.",
        "A3": "Du bist A3 (Pitch Strategist). Schärfe die Argumentation und fordere Fakten.",
        "A1": "Du bist A1 (Script Supervisor). Achte auf Timing, Klarheit und Wortgrenzen.",
    }
    role_instruction = agent_roles.get(agent_id, agent_roles["A2"])

    prompt = (
        f"{role_instruction}\n"
        f"Projekt-Kontext: {brief}\n"
        f"Aktueller Beat: {beat_id or 'Allgemein'}\n"
        f"Aktueller Text:\n{current_text}\n\n"
        f"Schreibe eine direkte Formulierung oder Vervollständigung für diesen Beat."
    )

    if not api_key:
        # Dynamic contextual fallback without generic placeholder
        if beat_id == "HOOK" or "hallo" in current_text.lower():
            clean = re.sub(
                r"^(hallo|hi|herzlich willkommen[,\s]*|mein name ist[^\.]*\.)\s*",
                "",
                current_text,
                flags=re.IGNORECASE,
            ).strip()
            capitalized = clean.capitalize() or "Zwei Stunden werden zu zwei Minuten."
            dynamic = f"Starte direkt: '{capitalized}'"
        elif beat_id == "TRACTION" or not re.search(r"\d+", current_text):
            dynamic = "Ergänze konkrete Kennzahlen: 'Über 100 Teams nutzen die Lösung produktiv.'"
        elif beat_id == "ASK":
            dynamic = "Schließe mit klarem Call-to-Action: 'Teste die Beta jetzt kostenlos.'"
        else:
            dynamic = "Präzisiere den konkreten Mehrwert und nenne die Zielgruppe direkt."
        return _word_chunks(dynamic), "local"

    try:
        from openai import OpenAI

        response = OpenAI(api_key=api_key).chat.completions.create(
            model=os.getenv("PALANTUM_OPENAI_MODEL", "gpt-4o-mini"),
            temperature=0.5,
            stream=True,
            messages=[
                {"role": "system", "content": role_instruction},
                {"role": "user", "content": prompt},
            ],
        )

        def chunks() -> Iterator[str]:
            for event in response:
                if not event.choices:
                    continue
                content = event.choices[0].delta.content
                if content:
                    yield content

        return chunks(), "openai"
    except Exception:
        dynamic = "Formuliere den Nutzen präzise in einem aktiven Satz."
        return _word_chunks(dynamic), "local"

