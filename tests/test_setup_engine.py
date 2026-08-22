from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from scripts import setup_engine


def test_install_uses_active_python_pip(monkeypatch: Any, tmp_path: Path) -> None:
    commands: list[list[str]] = []

    monkeypatch.setattr(sys, "prefix", "virtualenv")
    monkeypatch.setattr(sys, "base_prefix", "base")
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda command, **_kwargs: subprocess.CompletedProcess(command, 0, stdout="pip", stderr=""),
    )
    monkeypatch.setattr(setup_engine, "_run", commands.append)

    setup_engine._install([tmp_path / "video-use", tmp_path])

    assert commands == [
        [
            sys.executable,
            "-m",
            "pip",
            "install",
            "-e",
            str(tmp_path / "video-use"),
            "-e",
            str(tmp_path),
        ]
    ]


def test_install_requires_active_virtualenv(monkeypatch: Any, tmp_path: Path) -> None:
    monkeypatch.setattr(sys, "prefix", "same")
    monkeypatch.setattr(sys, "base_prefix", "same")

    with pytest.raises(RuntimeError, match="activate a virtual environment"):
        setup_engine._install([tmp_path])


def test_setup_fetches_checks_out_and_installs_exact_pin(monkeypatch: Any, tmp_path: Path) -> None:
    checkout = tmp_path / "vendor" / "video-use"
    (checkout / ".git").mkdir(parents=True)
    commands: list[list[str]] = []
    installed: list[list[Path]] = []

    monkeypatch.setattr(setup_engine, "_git", lambda: "git")
    monkeypatch.setattr(setup_engine, "_run", commands.append)
    monkeypatch.setattr(
        setup_engine,
        "_capture",
        lambda command: setup_engine.VIDEO_USE_PIN if command[-2:] == ["rev-parse", "HEAD"] else "",
    )
    monkeypatch.setattr(setup_engine, "_install", installed.append)

    result = setup_engine.setup(tmp_path)

    assert result == checkout
    assert [
        "git",
        "-C",
        str(checkout),
        "fetch",
        "--quiet",
        "origin",
        setup_engine.VIDEO_USE_PIN,
    ] in commands
    assert [
        "git",
        "-C",
        str(checkout),
        "checkout",
        "--detach",
        setup_engine.VIDEO_USE_PIN,
    ] in commands
    assert installed == [[checkout, tmp_path]]


def test_setup_refuses_dirty_vendor_checkout(monkeypatch: Any, tmp_path: Path) -> None:
    checkout = tmp_path / "vendor" / "video-use"
    (checkout / ".git").mkdir(parents=True)
    commands: list[list[str]] = []

    monkeypatch.setattr(setup_engine, "_git", lambda: "git")
    monkeypatch.setattr(setup_engine, "_capture", lambda _command: " M helpers/render.py")
    monkeypatch.setattr(setup_engine, "_run", commands.append)

    with pytest.raises(RuntimeError, match="local changes"):
        setup_engine.setup(tmp_path)

    assert commands == []
