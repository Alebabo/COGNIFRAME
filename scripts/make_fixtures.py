#!/usr/bin/env python3
"""Create deliberate pitch takes and optional visual/traction reshoots."""

from __future__ import annotations

import argparse
import shutil
import subprocess
import tempfile
from pathlib import Path

TAKES = {
    "take_1": (
        "Two hours of browser-test triage become two minutes with PitchCraft. "
        "Developers lose time when flaky browser tests delay releases and frustrate customers."
    ),
    "take_2": (
        "Our solution watches each browser test and groups failures by root cause "
        "and opens the exact fix. Visit our website and start the free developer preview today."
    ),
    "take_3": (
        "Our team is Maya and Jonas. I spent six years building the browser automation "
        "platform at Google, and Jonas ran QA infrastructure at Stripe."
    ),
}
TRACTION = "In the last three months, 120 teams joined our beta and cut triage time in half."
DEMO = "PitchCraft groups every failing run by root cause, so one click opens the exact fix."


def tts(text: str, destination: Path) -> None:
    """Generate deterministic fixture speech with a local eSpeak binary."""
    executable = shutil.which("espeak-ng") or shutil.which("espeak")
    if executable is None:
        raise RuntimeError(
            "Fixture generation requires the local espeak-ng (or espeak) executable."
        )
    subprocess.run(
        [executable, "-v", "en-us", "-s", "155", "-w", str(destination), text],
        check=True,
        capture_output=True,
    )


def duration(path: Path) -> float:
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return float(result.stdout.strip())


def make_video(text: str, output: Path, color: str, audio_speed: float = 1.0) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="pitchcraft-fixture-") as directory:
        audio = Path(directory) / "voice.wav"
        tts(text, audio)
        audio_filter = f"atempo={audio_speed}" if audio_speed != 1.0 else None
        command = [
            "ffmpeg",
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"color=c={color}:s=1080x1920:r=24",
            "-i",
            str(audio),
        ]
        if audio_filter:
            command.extend(
                ["-filter:a", audio_filter, "-t", f"{duration(audio) / audio_speed:.3f}"]
            )
        command.extend(
            [
                "-shortest",
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                "-c:a",
                "aac",
                "-ar",
                "48000",
                "-movflags",
                "+faststart",
                str(output),
            ]
        )
        subprocess.run(command, check=True)


def make_demo_video(output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="pitchcraft-demo-") as directory:
        audio = Path(directory) / "voice.wav"
        tts(DEMO, audio)
        font = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
        ui = "".join(
            [
                "drawbox=x=55:y=70:w=970:h=1780:color=white:t=fill,",
                "drawbox=x=55:y=70:w=970:h=150:color=0x111827:t=fill,",
                f"drawtext=fontfile={font}:text='PITCHCRAFT':fontcolor=white:fontsize=52:x=100:y=115,",
                (
                    f"drawtext=fontfile={font}:text='RUNS / FAILURES':"
                    "fontcolor=0x111827:fontsize=34:x=105:y=290,"
                ),
                "drawbox=x=95:y=355:w=890:h=175:color=0xfff1f2f4:t=fill,",
                "drawbox=x=95:y=565:w=890:h=175:color=0xfff1f2f4:t=fill,",
                "drawbox=x=95:y=775:w=890:h=175:color=0xfff1f2f4:t=fill,",
                "drawbox=x=95:y=985:w=890:h=175:color=0xfff1f2f4:t=fill,",
                (
                    f"drawtext=fontfile={font}:text='checkout.spec  /  timeout':"
                    "fontcolor=0x111827:fontsize=30:x=130:y=405,"
                ),
                (
                    f"drawtext=fontfile={font}:text='auth-flow.spec  /  selector':"
                    "fontcolor=0x111827:fontsize=30:x=130:y=615,"
                ),
                (
                    f"drawtext=fontfile={font}:text='billing.spec  /  network':"
                    "fontcolor=0x111827:fontsize=30:x=130:y=825,"
                ),
                (
                    f"drawtext=fontfile={font}:text='profile.spec  /  assertion':"
                    "fontcolor=0x111827:fontsize=30:x=130:y=1035,"
                ),
                "drawbox=x=130:y='500+mod(t*90,650)':w=820:h=12:color=0xf97316:t=fill,",
                (
                    f"drawtext=fontfile={font}:text='OPEN ROOT-CAUSE GROUP':"
                    "fontcolor=0x111827:fontsize=28:x=130:y=1320"
                ),
            ]
        )
        command = [
            "ffmpeg",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=0xf7f7f5:s=1080x1920:r=24",
            "-i",
            str(audio),
            "-vf",
            ui,
            "-shortest",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-ar",
            "48000",
            "-movflags",
            "+faststart",
            str(output),
        ]
        subprocess.run(command, check=True)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, default=Path("fixtures"))
    parser.add_argument("--include-traction", action="store_true")
    parser.add_argument("--include-demo", action="store_true")
    args = parser.parse_args()
    colors = ["0x16213e", "0x0f3460", "0x533483", "0xe94560"]
    for index, (name, text) in enumerate(TAKES.items()):
        speed = 1.7 if name == "take_3" else 1.0
        make_video(text, args.output_dir / f"{name}.mp4", colors[index], speed)
    if args.include_traction:
        make_video(TRACTION, args.output_dir / "take_traction.mp4", colors[3])
    if args.include_demo:
        make_demo_video(args.output_dir / "take_demo.mp4")


if __name__ == "__main__":
    main()
