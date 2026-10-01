#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["ziglang==0.14.1"]
# ///
"""Reproducibly cross-build the no-CRT Windows launcher from its adjacent C source."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "plugins" / "codex-trajectory" / "scripts" / "codex_trajectory_launcher.c"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Compare without replacing the binary")
    args = parser.parse_args()
    spec = importlib.util.find_spec("ziglang")
    if spec is None or spec.origin is None:
        raise RuntimeError(
            "Run with uv run --no-project --script scripts/build_windows_launcher.py"
        )
    include = Path(spec.origin).parent / "lib" / "libc" / "include" / "any-windows-any"
    destination = SOURCE.with_suffix(".exe")
    with tempfile.TemporaryDirectory(prefix="codex-trajectory-launcher-") as directory:
        output = Path(directory) / destination.name
        subprocess.run(
            [
                sys.executable,
                "-m",
                "ziglang",
                "cc",
                "-target",
                "x86_64-windows-gnu",
                "-Os",
                "-ffreestanding",
                "-fno-stack-protector",
                "-nostdlib",
                "-isystem",
                str(include),
                "-Wl,--entry,launch",
                "-Wl,--subsystem,windows",
                "-s",
                str(SOURCE),
                "-lkernel32",
                "-o",
                str(output),
            ],
            check=True,
            timeout=120,
        )
        generated = output.read_bytes()
        if args.check:
            if destination.read_bytes() != generated:
                raise RuntimeError(
                    "Packaged Windows launcher differs from the pinned source build."
                )
        else:
            shutil.copyfile(output, destination)
        print(f"Windows launcher SHA-256: {hashlib.sha256(generated).hexdigest()}")


if __name__ == "__main__":
    main()
