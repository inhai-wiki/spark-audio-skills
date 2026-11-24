#!/usr/bin/env python3
"""Run a bundled skill with optional local .env credentials, never evaluating shell."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
COMMANDS = {
    "highlight": ROOT / "skills/xfyun-highlight-clips/scripts/highlight_clips.py",
    "podcast": ROOT / "skills/xfyun-dual-voice-podcast/scripts/podcast.py",
}
ENV_KEYS = {
    "XFYUN_APP_ID", "XFYUN_API_KEY", "XFYUN_API_SECRET",
    "XFYUN_TTS_APP_ID", "XFYUN_TTS_API_PASSWORD",
    "XFYUN_TTS_API_KEY", "XFYUN_TTS_API_SECRET",
}


def local_environment(path: Path, base: dict[str, str]) -> dict[str, str]:
    env = dict(base)
    if not path.exists():
        return env
    for lineno, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, sep, value = line.partition("=")
        key = key.strip()
        if not sep or key not in ENV_KEYS:
            raise ValueError(f"Unsupported .env entry on line {lineno}; use the supplied template")
        value = value.strip()
        if value.startswith(("'", '"')):
            if len(value) < 2 or value[-1] != value[0]:
                raise ValueError(f"Unclosed quote in .env on line {lineno}")
            value = value[1:-1]
        if value and not env.get(key):
            env[key] = value
    return env


def main() -> int:
    if len(sys.argv) < 2 or sys.argv[1] not in COMMANDS:
        print("Usage: python scripts/run.py {highlight|podcast} [command and options]")
        return 0 if len(sys.argv) > 1 and sys.argv[1] in {"-h", "--help"} else 2
    try:
        env = local_environment(ROOT / ".env", dict(os.environ))
    except (ValueError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    return subprocess.call([sys.executable, str(COMMANDS[sys.argv[1]]), *sys.argv[2:]], env=env)


if __name__ == "__main__":
    raise SystemExit(main())
