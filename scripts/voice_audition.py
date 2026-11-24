#!/usr/bin/env python3
"""Render the same text with several XFYun voices, preserving per-voice caches.

Usage: python scripts/voice_audition.py audition.json --out outputs/audition
Input: {"text": "Spoken text", "voices": [{"id": "host", "name": "Host", "voice": "authorized_voice"}]}
Uses the existing podcast renderer. It never automatically retries uncertain requests.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


podcast = load_module("audition_podcast", ROOT / "skills/xfyun-dual-voice-podcast/scripts/podcast.py")
runner = load_module("audition_runner", ROOT / "scripts/run.py")


def load_audition(path):
    try:
        source = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise podcast.PodcastError(f"Cannot read audition JSON ({type(exc).__name__})") from None
    if not isinstance(source, dict):
        raise podcast.PodcastError("Audition must be a JSON object")
    text = podcast.plain_text(source.get("text"), "audition text")
    voices = source.get("voices")
    if not isinstance(voices, list) or not 1 <= len(voices) <= 20:
        raise podcast.PodcastError("Provide between 1 and 20 voices")
    normalized, seen = [], set()
    for voice in voices:
        if not isinstance(voice, dict):
            raise podcast.PodcastError("Each voice must be an object")
        ident, code = voice.get("id"), voice.get("voice")
        if not isinstance(ident, str) or not podcast.ID_PATTERN.fullmatch(ident) or ident in seen:
            raise podcast.PodcastError("Voice ids must be unique, path-safe identifiers")
        if not isinstance(code, str) or not podcast.ID_PATTERN.fullmatch(code):
            raise podcast.PodcastError(f"{ident}: provide an authorized voice code")
        name = voice.get("name", ident)
        if not isinstance(name, str) or not name.strip() or any(ord(c) < 32 for c in name):
            raise podcast.PodcastError(f"{ident}: provide a printable voice name")
        clean = {"id": ident, "name": name, "voice": code}
        for key, high, default in (("speed", 100, 50), ("volume", 100, 50), ("pitch", 100, 50),
                                   ("reg", 2, 0), ("rdn", 3, 0)):
            clean[key] = podcast.bounded_int(voice.get(key, default), 0, high, key)
        normalized.append(clean)
        seen.add(ident)
    return {"text": text, "voices": normalized}


def voice_document(text, voice):
    """An audition has one voice. The dual-role script validator is unchanged."""
    speaker = {key: value for key, value in voice.items() if key != "id"}
    return {"schema_version": 1, "title": f"{voice['name']}同文试听", "speakers": {"voice": speaker},
            "settings": {"pause_ms": 0}, "turns": [
                {"id": "audition", "speaker": "voice", "text": text, "pause_after_ms": 0}]}


def audition_manifest(audition):
    plans = [podcast.make_plan(voice_document(audition["text"], voice)) for voice in audition["voices"]]
    return {"schema_version": 1, "mode": "xfyun_voice_audition", "status": "incomplete",
            "notice": "AI-generated voice samples. Same input text; not a voice-quality benchmark or a price quote.",
            "text": audition["text"], "planned_characters": sum(plan["characters"] for plan in plans),
            "planned_request_count": sum(plan["request_count"] for plan in plans),
            "billing_note": "Actual usage and charges depend on account authorization and provider billing.",
            "voices": [{**voice, "status": "pending", "plan_hash": plan["plan_hash"]}
                       for voice, plan in zip(audition["voices"], plans)]}


def render_auditions(audition, output_dir, resume=False, timeout=120):
    output_dir = Path(output_dir).resolve()
    manifest_path = output_dir / "audition-manifest.json"
    with podcast.render_lock(output_dir):
        if manifest_path.exists() and not resume:
            raise podcast.PodcastError("Existing audition preserved; use --resume to reuse successful voice segments")
        manifest = audition_manifest(audition)
        podcast.atomic_json(manifest_path, manifest)
        for voice, entry in zip(audition["voices"], manifest["voices"]):
            sample_dir = output_dir / voice["id"]
            entry["status"] = "running"
            podcast.atomic_json(manifest_path, manifest)
            try:
                result = podcast.render(voice_document(audition["text"], voice), sample_dir,
                                        resume=resume, retry_uncertain=False, timeout=timeout)
            except Exception as exc:
                entry.update({"status": "failed", "error_type": type(exc).__name__})
                if isinstance(exc, podcast.PodcastError):
                    entry["error"] = str(exc)
                manifest["status"] = "failed"
                podcast.atomic_json(manifest_path, manifest)
                raise
            entry.update({"status": "complete", "duration_seconds": result["duration_seconds"],
                          "mp3": str(sample_dir / "podcast.mp3"), "wav": str(sample_dir / "podcast.wav"),
                          "state": str(sample_dir / "state.json")})
            podcast.atomic_json(manifest_path, manifest)
            print(json.dumps({"voice_id": voice["id"], "voice_name": voice["name"], "status": "complete",
                              "duration_seconds": result["duration_seconds"]}, ensure_ascii=False), flush=True)
        manifest["status"] = "complete"
        podcast.atomic_json(manifest_path, manifest)
    return {"status": "complete", "manifest": str(manifest_path), "voice_count": len(audition["voices"]),
            "planned_characters": manifest["planned_characters"]}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("script", type=Path, help="JSON with text and voices")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--resume", action="store_true", help="Reuse completed segments; never retry uncertain requests")
    parser.add_argument("--timeout", type=float, default=120)
    args = parser.parse_args(argv)
    try:
        audition = load_audition(args.script)
        # Loading happens only during CLI execution, never when imported for tests.
        env = runner.local_environment(ROOT / ".env", dict(os.environ))
        for key in runner.ENV_KEYS:
            if key in env:
                os.environ[key] = env[key]
        result = render_auditions(audition, args.out, args.resume, args.timeout)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (podcast.PodcastError, ValueError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
    except Exception as exc:
        print(json.dumps({"error": f"Audition stopped ({type(exc).__name__}); inspect the per-voice state before resuming"}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
