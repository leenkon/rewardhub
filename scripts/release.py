#!/usr/bin/env python3
from __future__ import annotations

import argparse
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SEMVER = re.compile(r"^\d+\.\d+\.\d+$")
TAG_PATTERN = re.compile(r"^v(\d+)\.(\d+)\.(\d+)$")


def git_output(*args: str) -> str:
    # Commit subjects are UTF-8; without an explicit encoding a Windows console decodes them as GBK.
    return subprocess.check_output(["git", *args], cwd=ROOT, text=True, encoding="utf-8").strip()


def current_version() -> str:
    text = (ROOT / "app.py").read_text(encoding="utf-8")
    match = re.search(r'^APP_VERSION = "([^"]+)"', text, re.MULTILINE)
    if not match or not SEMVER.fullmatch(match.group(1)):
        raise SystemExit("APP_VERSION is missing or invalid")
    return match.group(1)


def latest_tag() -> str:
    tags = git_output("tag", "--sort=-v:refname").splitlines()
    return next((tag for tag in tags if TAG_PATTERN.fullmatch(tag)), "")


def version_tuple(version: str) -> tuple[int, int, int]:
    return tuple(int(part) for part in version.split("."))


def next_version() -> str:
    current = current_version()
    tag = latest_tag()
    if not tag:
        return current
    major, minor, patch = TAG_PATTERN.fullmatch(tag).groups()
    tagged = f"{major}.{minor}.{patch}"
    if version_tuple(current) > version_tuple(tagged):
        return current
    return f"{major}.{minor}.{int(patch) + 1}"


def replace_file(path: str, pattern: str, replacement: str, count: int = 0) -> None:
    target = ROOT / path
    text = target.read_text(encoding="utf-8")
    updated, changed = re.subn(pattern, replacement, text, count=count, flags=re.MULTILINE)
    if changed == 0:
        raise SystemExit(f"Version marker not found in {path}")
    target.write_text(updated, encoding="utf-8", newline="\n")


def bump_version(version: str) -> None:
    if not SEMVER.fullmatch(version):
        raise SystemExit(f"Invalid version: {version}")
    replace_file("app.py", r'^APP_VERSION = "[^"]+"$', f'APP_VERSION = "{version}"', 1)
    replace_file(
        "Dockerfile",
        r'(org\.opencontainers\.image\.version=")[^"]+',
        rf'\g<1>{version}',
        1,
    )
    replace_file("README.md", r"^当前版本：`\d+\.\d+\.\d+`", f"当前版本：`{version}`", 1)
    replace_file("tests/test_app.py", r'(state\["version"\], ")[^"]+', rf"\g<1>{version}", 1)


def changelog(version: str) -> None:
    """Open this release's section; a section already written by hand keeps its own entries."""
    path = ROOT / "CHANGELOG.md"
    today = datetime.now(timezone.utc).date().isoformat()
    if path.exists():
        text = path.read_text(encoding="utf-8")
        dated, written = re.subn(
            rf"^## v{re.escape(version)} - .*$", f"## v{version} - {today}", text, count=1, flags=re.MULTILINE
        )
        if written:
            path.write_text(dated, encoding="utf-8", newline="\n")
            return
    else:
        text = "# Changelog\n"
    tag = latest_tag()
    if tag:
        entries = git_output("log", "--pretty=format:- %s (%h)", f"{tag}..HEAD")
    else:
        entries = git_output("log", "--reverse", "--pretty=format:- %s (%h)")
    entries = entries or "- No user-facing changes recorded."
    previous = text[len("# Changelog") :].lstrip("\n") if text.startswith("# Changelog") else text
    section = f"# Changelog\n\n## v{version} - {today}\n\n{entries}\n\n{previous}"
    path.write_text(section, encoding="utf-8", newline="\n")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("next-version", "bump-version", "changelog"))
    parser.add_argument("--version")
    args = parser.parse_args()

    if args.command == "next-version":
        print(next_version())
        return
    if not args.version:
        raise SystemExit("--version is required")
    if args.command == "bump-version":
        bump_version(args.version)
    else:
        changelog(args.version)


if __name__ == "__main__":
    main()
