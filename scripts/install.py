#!/usr/bin/env python3
"""Copy the two skills to a discovery directory without overwriting existing entries."""
import argparse
import os
from pathlib import Path
import shutil

ROOT = Path(__file__).resolve().parents[1]
NAMES = ("xfyun-highlight-clips", "xfyun-dual-voice-podcast")


def main():
    default = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))) / "skills"
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dest", type=Path, default=default)
    args = parser.parse_args()
    target = args.dest.expanduser().resolve()
    for name in NAMES:
        path = target / name
        if path.exists() or path.is_symlink():
            parser.error(f"Existing skill preserved: {path}. Choose a different destination or manage it manually.")
    target.mkdir(parents=True, exist_ok=True)
    for name in NAMES:
        shutil.copytree(ROOT / "skills" / name, target / name,
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".DS_Store"))
        print(target / name / "SKILL.md")


if __name__ == "__main__":
    main()
