from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path
from typing import TypedDict, cast

from palantum.engine.videouse import probe


class Word(TypedDict):
    type: str
    text: str
    start: float
    end: float
    speaker_id: str


def _cache_meta(source: Path) -> dict[str, int]:
    stat = source.stat()
    return {"mtime_ns": stat.st_mtime_ns, "size": stat.st_size}


def _synthesise_scribe(words: list[dict[str, object]]) -> dict[str, object]:
    kept: list[Word] = []
    previous_end: float | None = None
    for item in words:
        text = str(item.get("word", item.get("text", "")))
        if not text.strip() or item.get("start") is None or item.get("end") is None:
            continue
        start = float(cast(float, item["start"]))
        end = float(cast(float, item["end"]))
        if previous_end is not None and start >= previous_end:
            kept.append(
                {
                    "type": "spacing",
                    "text": " ",
                    "start": previous_end,
                    "end": start,
                    "speaker_id": "speaker_0",
                }
            )
        kept.append(
            {
                "type": "word",
                "text": text,
                "start": start,
                "end": end,
                "speaker_id": "speaker_0",
            }
        )
        previous_end = end
    return {
        "words": kept,
        "language_code": "en",
        "source": "openai-whisper-1",
        "duration_seconds": previous_end or 0.0,
    }


def whisper_to_scribe(payload: dict[str, object]) -> dict[str, object]:
    """Adapt verbose Whisper word timestamps to video-use's Scribe shape."""
    raw_words = payload.get("words", [])
    if not isinstance(raw_words, list):
        raise ValueError("Whisper verbose_json did not contain a word list")
    return _synthesise_scribe([item for item in raw_words if isinstance(item, dict)])


def _extract_audio(
    source: Path, destination: Path, start: float | None = None, duration: float | None = None
) -> None:
    command = ["ffmpeg", "-y"]
    if start is not None:
        command.extend(["-ss", f"{start:.3f}"])
    command.extend(["-i", str(source)])
    if duration is not None:
        command.extend(["-t", f"{duration:.3f}"])
    command.extend(
        ["-vn", "-ac", "1", "-ar", "16000", "-c:a", "mp3", "-b:a", "64k", str(destination)]
    )
    subprocess.run(command, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)


def _whisper(audio: Path) -> dict[str, object]:
    from openai import OpenAI

    with audio.open("rb") as handle:
        response = OpenAI(api_key=os.environ["OPENAI_API_KEY"]).audio.transcriptions.create(
            model="whisper-1",
            file=handle,
            response_format="verbose_json",
            timestamp_granularities=["word"],
        )
    dumped = response.model_dump() if hasattr(response, "model_dump") else dict(response)
    return {str(k): v for k, v in dumped.items()}


def transcribe(source: Path, edit_dir: Path) -> Path:
    """Transcribe once per source mtime/size and write transcript + word index."""
    transcript_dir = edit_dir / "transcripts"
    transcript_dir.mkdir(parents=True, exist_ok=True)
    transcript_path = transcript_dir / f"{source.stem}.json"
    meta_path = transcript_path.with_suffix(".meta")
    metadata = _cache_meta(source)
    if (
        transcript_path.exists()
        and meta_path.exists()
        and json.loads(meta_path.read_text()) == metadata
    ):
        _write_word_index(edit_dir, source.stem, json.loads(transcript_path.read_text()))
        return transcript_path
    source_probe = probe(source)
    with tempfile.TemporaryDirectory(prefix="palantum-audio-") as directory:
        audio = Path(directory) / "audio.mp3"
        if source_probe.duration_s * 16000 * 2 / 8 > 24_000_000:
            chunks: list[dict[str, object]] = []
            chunk_duration = 300.0
            offset = 0.0
            while offset < source_probe.duration_s:
                length = min(chunk_duration, source_probe.duration_s - offset)
                chunk = Path(directory) / f"chunk-{len(chunks):03d}.mp3"
                _extract_audio(source, chunk, offset, length)
                chunks.append({"payload": _whisper(chunk), "offset": offset})
                offset += length
            words: list[dict[str, object]] = []
            for chunk_info in chunks:
                payload = chunk_info["payload"]
                if isinstance(payload, dict):
                    for item in payload.get("words", []):
                        if isinstance(item, dict):
                            adjusted = dict(item)
                            adjusted["start"] = float(cast(float, item["start"])) + float(
                                cast(float, chunk_info["offset"])
                            )
                            adjusted["end"] = float(cast(float, item["end"])) + float(
                                cast(float, chunk_info["offset"])
                            )
                            words.append(adjusted)
            scribe = whisper_to_scribe({"words": words})
        else:
            _extract_audio(source, audio)
            scribe = whisper_to_scribe(_whisper(audio))
    transcript_path.write_text(json.dumps(scribe, indent=2), encoding="utf-8")
    meta_path.write_text(json.dumps(metadata), encoding="utf-8")
    _write_word_index(edit_dir, source.stem, scribe)
    return transcript_path


def _write_word_index(edit_dir: Path, source_id: str, scribe: dict[str, object]) -> None:
    index_path = edit_dir / "word_index.json"
    all_indices: dict[str, list[dict[str, object]]] = {}
    if index_path.exists():
        existing = json.loads(index_path.read_text())
        if isinstance(existing, dict):
            all_indices = existing
    words = cast(list[dict[str, object]], scribe["words"])
    all_indices[source_id] = [
        {"text": w["text"], "start": w["start"], "end": w["end"]}
        for w in words
        if w.get("type") == "word"
    ]
    index_path.write_text(json.dumps(all_indices, indent=2), encoding="utf-8")
