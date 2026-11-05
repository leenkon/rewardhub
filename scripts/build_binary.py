#!/usr/bin/env python3
"""用 PyInstaller 打一个自带运行时的 RewardHub 可执行文件。"""

from __future__ import annotations

import argparse
import os
from pathlib import Path

from PyInstaller.__main__ import run


ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--name", default="RewardHub")
    parser.add_argument("--distpath", default=str(ROOT / "build" / "dist"))
    args = parser.parse_args()

    build_dir = ROOT / "build" / "pyinstaller"
    spec_dir = ROOT / "build" / "spec"
    separator = os.pathsep

    run(
        [
            "--noconfirm",
            "--clean",
            "--onefile",
            f"--name={args.name}",
            f"--distpath={Path(args.distpath).resolve()}",
            f"--workpath={build_dir}",
            f"--specpath={spec_dir}",
            f"--paths={ROOT}",
            f"--add-data={ROOT / 'templates'}{separator}templates",
            f"--add-data={ROOT / 'static'}{separator}static",
            str(ROOT / "app.py"),
        ]
    )


if __name__ == "__main__":
    main()
