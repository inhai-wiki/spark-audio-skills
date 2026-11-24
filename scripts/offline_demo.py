#!/usr/bin/env python3
"""Exercise both skills with generated test signals and manual fixture transcripts. No API calls."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
HIGHLIGHT = ROOT / "skills/xfyun-highlight-clips/scripts/highlight_clips.py"
PODCAST = ROOT / "skills/xfyun-dual-voice-podcast/scripts/podcast.py"


def run(args):
    result = subprocess.run([str(x) for x in args], text=True, capture_output=True)
    if result.returncode:
        raise RuntimeError(result.stderr or result.stdout or f"Command failed: {args[0]}")
    return result.stdout


def save(path, obj):
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def duration(path):
    return float(run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", path]).strip())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True, help="A new or empty local directory")
    args = parser.parse_args()
    out = args.out.resolve()
    if out.exists() and any(out.iterdir()):
        parser.error("Choose a new or empty directory; existing demo results are preserved")
    out.mkdir(parents=True, exist_ok=True)
    video = out / "synthetic-fixture.mp4"
    run(["ffmpeg", "-v", "error", "-nostdin", "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=25:duration=12",
         "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=16000:duration=12",
         "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", video])
    original_hash = hashlib.sha256(video.read_bytes()).hexdigest()
    prepared = run([sys.executable, HIGHLIGHT, "prepare", video, "--out", out / "highlight", "--chunk-seconds", "4"])
    job = Path(json.loads(prepared.strip().splitlines()[-1])["job"])
    transcript = out / "manual-fixture-transcript.json"
    save(transcript, {"timing_precision": "manual-test-fixture", "segments": [
        {"id": "s1", "start": 0, "end": 4, "text": "离线流程样例第一段，音轨为测试音。"},
        {"id": "s2", "start": 4, "end": 8, "text": "离线流程样例第二段，此文本未由语音模型识别。"},
        {"id": "s3", "start": 8, "end": 12, "text": "离线流程样例第三段，仅检查时间与文件输出。"},
    ]})
    run([sys.executable, HIGHLIGHT, "import-transcript", "--job", job, "--input", transcript])
    manifest = out / "manual-highlights.json"
    save(manifest, {"source_sha256": original_hash, "clips": [
        {"id": "first", "title": "测试区间一", "reason": "手工测试区间，非模型高光判断。", "start": 0, "end": 4, "segment_ids": ["s1"]},
        {"id": "second", "title": "测试区间二", "reason": "手工测试区间，非模型高光判断。", "start": 4, "end": 8, "segment_ids": ["s2"]},
    ]})
    video_result = json.loads(run([sys.executable, HIGHLIGHT, "clip", "--job", job, "--manifest", manifest]))
    audio_result = json.loads(run([sys.executable, HIGHLIGHT, "clip", "--job", job, "--manifest", manifest, "--audio-only"]))
    turns = []
    for i, (role, frequency, seconds) in enumerate([("host", 440, 1.1), ("guest", 880, 1.4), ("host", 440, .9)]):
        tone = out / f"tone-{i}.wav"
        run(["ffmpeg", "-v", "error", "-nostdin", "-f", "lavfi", "-i",
             f"sine=frequency={frequency}:sample_rate=24000:duration={seconds}", "-c:a", "pcm_s16le", tone])
        turns.append({"id": f"turn_{i}", "speaker": role, "text": f"第{i+1}段离线测试文字。", "audio_path": str(tone)})
    script = out / "podcast-fixture.json"
    save(script, {"schema_version": 1, "title": "离线测试音拼接", "speakers": {
        "host": {"name": "主持人", "voice": "x6_bokenansheng_pro"},
        "guest": {"name": "讲解者", "voice": "x6_lingyuyan_pro"}}, "settings": {"pause_ms": 250}, "turns": turns})
    podcast_result = json.loads(run([sys.executable, PODCAST, "assemble", script, "--out", out / "podcast"]))
    expected = 1.1 + 1.4 + .9 + .5
    actual = duration(out / "podcast/podcast.wav")
    if abs(actual - expected) > .02:
        raise RuntimeError("Podcast duration does not match fixture timing")
    if hashlib.sha256(video.read_bytes()).hexdigest() != original_hash:
        raise RuntimeError("Source media was unexpectedly modified")
    for item in video_result["clips"] + audio_result["clips"]:
        if abs(duration(item["file"]) - 4) > .25:
            raise RuntimeError("Highlight duration does not match fixture")
    report = {"mode": "offline-fixtures-only", "api_requests": 0,
              "notice": "Synthetic test signals and manually supplied text. This is not an ASR or TTS quality demonstration.",
              "source_unchanged": True, "highlight_job": str(job), "video_clips": video_result["clips"],
              "audio_clips": audio_result["clips"], "podcast": podcast_result,
              "podcast_expected_seconds": expected, "podcast_actual_seconds": actual}
    save(out / "verification.json", report)
    print(json.dumps({"ok": True, "api_requests": 0, "report": str(out / "verification.json")}, ensure_ascii=False))


if __name__ == "__main__":
    main()
