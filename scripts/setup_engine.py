from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

VIDEO_USE_REPO = "https://github.com/browser-use/video-use.git"
VIDEO_USE_PIN = "92c2b34e44c205cbc2acae7f6ca7c1c219d5dd66"
ROOT = Path(__file__).resolve().parents[1]


def _run(command: list[str]) -> None:
    subprocess.run(command, check=True)


def _capture(command: list[str]) -> str:
    result = subprocess.run(command, check=True, capture_output=True, text=True)
    return result.stdout.strip()


def _git() -> str:
    executable = shutil.which("git")
    if executable is None:
        raise RuntimeError("git is required but was not found on PATH")
    return executable


def _install(editable: list[Path]) -> None:
    if sys.prefix == sys.base_prefix:
        raise RuntimeError("activate a virtual environment before installing video-use")
    pip_check = subprocess.run(
        [sys.executable, "-m", "pip", "--version"],
        check=False,
        capture_output=True,
        text=True,
    )
    if pip_check.returncode == 0:
        command = [sys.executable, "-m", "pip", "install"]
    else:
        uv = shutil.which("uv")
        if uv is None:
            raise RuntimeError(
                f"{sys.executable} has no pip and uv was not found; activate a seeded virtualenv"
            )
        command = [uv, "pip", "install", "--python", sys.executable]
    for path in editable:
        command.extend(["-e", str(path)])
    _run(command)


def setup(repo_root: Path = ROOT) -> Path:
    root = repo_root.resolve()
    checkout = root / "vendor" / "video-use"
    git = _git()
    checkout.parent.mkdir(parents=True, exist_ok=True)

    if checkout.exists() and not (checkout / ".git").exists():
        raise RuntimeError(f"refusing to replace non-git path: {checkout}")
    if not checkout.exists():
        _run([git, "clone", "--no-checkout", VIDEO_USE_REPO, str(checkout)])
    elif _capture([git, "-C", str(checkout), "status", "--porcelain"]):
        raise RuntimeError(f"refusing to overwrite local changes in {checkout}")

    _run([git, "-C", str(checkout), "fetch", "--quiet", "origin", VIDEO_USE_PIN])
    _run([git, "-C", str(checkout), "checkout", "--detach", VIDEO_USE_PIN])
    head = _capture([git, "-C", str(checkout), "rev-parse", "HEAD"])
    if head != VIDEO_USE_PIN:
        raise RuntimeError(f"video-use checkout mismatch: expected {VIDEO_USE_PIN}, got {head}")

    _install([checkout, root])
    return checkout


def main() -> None:
    parser = argparse.ArgumentParser(description="Install the pinned video-use engine")
    parser.add_argument("--repo-root", type=Path, default=ROOT)
    args = parser.parse_args()
    checkout = setup(args.repo_root)
    print(f"video-use pinned at {VIDEO_USE_PIN} in {checkout}")
    print(f"installed with {sys.executable}")


if __name__ == "__main__":
    main()
