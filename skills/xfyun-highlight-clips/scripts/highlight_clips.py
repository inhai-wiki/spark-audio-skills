#!/usr/bin/env python3
"""Prepare media, transcribe with recording-file ASR LLM or ASR V2, and cut Agent-selected highlights."""
from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import http.client
import importlib.util
import json
import math
import os
from pathlib import Path
import re
import secrets
import string
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from email.utils import format_datetime
from urllib.parse import urlencode, quote_plus

API_HOST = "iat.cn-huabei-1.xf-yun.com"
API_PATH = "/v1"
RATE = 16000
BYTES_PER_SECOND = RATE * 2
FRAME_BYTES = 1024
SCHEMA_VERSION = 1


class SkillError(Exception):
    """An actionable error safe to show without credentials."""


def emit(value):
    print(json.dumps(value, ensure_ascii=False), flush=True)


def now():
    return datetime.now(timezone.utc).isoformat()


def read_json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SkillError(f"Cannot read JSON file {path}: {type(exc).__name__}") from None


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temp.replace(path)


def write_text(path, text):
    path = Path(path)
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(text, encoding="utf-8")
    temp.replace(path)


def sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def slug(value):
    clean = re.sub(r"[^\w.-]+", "-", value, flags=re.UNICODE).strip(".-_")
    return clean[:64] or "media"


def require_media_tools():
    missing = [tool for tool in ("ffmpeg", "ffprobe") if not shutil.which(tool)]
    if missing:
        raise SkillError("Install required media tools: " + ", ".join(missing))


def run_media(command):
    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode:
        # These commands contain only local media paths and no credentials.
        raise SkillError(f"{Path(command[0]).name} failed: {result.stderr[-2000:]}")
    return result.stdout


def probe_media(path):
    path = Path(path).expanduser().resolve()
    if not path.is_file():
        raise SkillError(f"Local media file does not exist: {path}")
    raw = run_media(["ffprobe", "-v", "error", "-show_format", "-show_streams", "-of", "json", str(path)])
    try:
        obj = json.loads(raw)
        duration = float(obj["format"]["duration"])
    except (KeyError, ValueError, TypeError):
        raise SkillError(f"Cannot determine finite media duration: {path}") from None
    if not math.isfinite(duration) or duration <= 0:
        raise SkillError(f"Invalid media duration: {path}")
    streams = obj.get("streams", [])
    audio_stream = next((s for s in streams if s.get("codec_type") == "audio"), None)
    if audio_stream is None:
        raise SkillError(f"Media has no audio track: {path}")
    try:
        format_start = float(obj["format"].get("start_time", 0))
        audio_start = float(audio_stream.get("start_time", format_start))
    except (TypeError, ValueError):
        raise SkillError("Cannot establish source audio timeline; normalize the media before preparing") from None
    if not math.isfinite(format_start) or not math.isfinite(audio_start):
        raise SkillError("Invalid source audio start time")
    audio_offset = audio_start - format_start
    # Decoding removes track start offsets. Reject material offsets until a verified
    # timeline-normalization implementation is available; never shift cuts silently.
    if abs(audio_offset) > 0.05:
        raise SkillError(f"Audio track starts {audio_offset:.3f}s relative to the media timeline. This version requires aligned track starts; normalize the source timeline first to avoid shifted highlight cuts.")
    return {"audio_offset": audio_offset, "duration": duration, "has_video": any(s.get("codec_type") == "video" and not s.get("disposition", {}).get("attached_pic") for s in streams)}


def prepare_one(source, out, chunk_seconds=20.0):
    require_media_tools()
    if not math.isfinite(chunk_seconds) or not 1 <= chunk_seconds <= 55:
        raise SkillError("--chunk-seconds must be between 1 and 55 seconds")
    source = Path(source).expanduser().resolve()
    info = probe_media(source)
    checksum = sha256_file(source)
    identity = hashlib.sha256((str(source) + "\0" + checksum + "\0" + str(chunk_seconds)).encode()).hexdigest()[:12]
    job = Path(out).expanduser().resolve() / f"{slug(source.stem)}-{identity}"
    manifest_path = job / "media.json"
    if manifest_path.exists():
        manifest = read_json(manifest_path)
        required = [job / manifest["mp3"], job / manifest["pcm"]] + [job / c["pcm"] for c in manifest["chunks"]]
        if manifest.get("source_sha256") == checksum and all(p.is_file() and p.stat().st_size > 0 for p in required):
            return job
    job.mkdir(parents=True, exist_ok=True)
    (job / "chunks").mkdir(exist_ok=True)
    mp3 = job / "analysis.mp3"
    pcm = job / "analysis.pcm"
    # Build both from the original to avoid a second lossy compression before ASR.
    run_media(["ffmpeg", "-v", "error", "-nostdin", "-y", "-i", str(source), "-map", "0:a:0", "-vn", "-ac", "1", "-ar", str(RATE), "-c:a", "libmp3lame", "-b:a", "128k", "-map_metadata", "-1", str(mp3)])
    run_media(["ffmpeg", "-v", "error", "-nostdin", "-y", "-i", str(source), "-map", "0:a:0", "-vn", "-ac", "1", "-ar", str(RATE), "-c:a", "pcm_s16le", "-f", "s16le", str(pcm)])
    # Compressed sources can decode encoder padding beyond the media timeline.
    max_source_bytes = int(info["duration"] * RATE) * 2
    if pcm.stat().st_size > max_source_bytes:
        with pcm.open("r+b") as handle:
            handle.truncate(max_source_bytes)
    chunk_size = int(chunk_seconds * RATE) * 2
    chunks, offset = [], 0
    with pcm.open("rb") as handle:
        while True:
            data = handle.read(chunk_size)
            if not data:
                break
            index = len(chunks)
            relative = f"chunks/{index:05d}.pcm"
            (job / relative).write_bytes(data)
            chunks.append({"index": index, "start": offset / BYTES_PER_SECOND, "end": (offset + len(data)) / BYTES_PER_SECOND, "pcm": relative, "bytes": len(data)})
            offset += len(data)
    if not chunks:
        raise SkillError("Decoded audio is empty")
    manifest = {"schema_version": SCHEMA_VERSION, "source": str(source), "source_sha256": checksum, "duration": info["duration"], "audio_duration": offset / BYTES_PER_SECOND, "has_video": info["has_video"], "mp3": "analysis.mp3", "pcm": "analysis.pcm", "sample_rate": RATE, "chunk_seconds": chunk_seconds, "chunks": chunks, "prepared_at": now()}
    write_json(manifest_path, manifest)
    return job


def build_auth_url(api_key, api_secret, date=None):
    date = date or format_datetime(datetime.now(timezone.utc), usegmt=True)
    origin = f"host: {API_HOST}\ndate: {date}\nGET {API_PATH} HTTP/1.1"
    signature = base64.b64encode(hmac.new(api_secret.encode(), origin.encode(), hashlib.sha256).digest()).decode()
    auth = f'api_key="{api_key}",algorithm="hmac-sha256",headers="host date request-line",signature="{signature}"'
    query = urlencode({"host": API_HOST, "date": date, "authorization": base64.b64encode(auth.encode()).decode()})
    return f"wss://{API_HOST}{API_PATH}?{query}"


def build_frame(app_id, data, seq, status, context=None, res_id=None):
    header = {"app_id": app_id, "status": status}
    if res_id:
        header["res_id"] = res_id
    frame = {"header": header, "payload": {"audio": {"encoding": "raw", "sample_rate": RATE, "channels": 1, "bit_depth": 16, "seq": seq, "status": status, "audio": base64.b64encode(data).decode()}}}
    if status == 0:
        iat = {"domain": "spark_asr_v2", "language": "zh_cn", "accent": "mandarin", "dwa": "wpgs", "vinfo": 1, "svad": 0, "result": {"encoding": "utf8", "compress": "raw", "format": "json"}}
        if context:
            encoded = json.dumps(context, ensure_ascii=False, separators=(",", ":"))
            if len(encoded.encode()) > 2048:
                raise SkillError("Context exceeds the API 2 KB limit")
            iat["context"] = encoded
        frame["parameter"] = {"iat": iat}
    return frame


class ResultAccumulator:
    """Keep corrected ASR pieces. Reserved timestamp fields are never treated as word alignment."""
    def __init__(self):
        self.pieces = {}
        self.server_done = False
        self.sid = None

    def add_message(self, message):
        if not isinstance(message, dict):
            raise SkillError("ASR response must be a JSON object")
        header = message.get("header")
        if not isinstance(header, dict) or "code" not in header:
            raise SkillError("ASR response is missing the required header.code")
        if isinstance(header["code"], bool) or not isinstance(header["code"], int):
            raise SkillError("ASR response header.code must be an integer")
        if header["code"] != 0:
            raise SkillError(f"ASR returned error code {header.get('code')}; inspect the saved response locally")
        self.sid = header.get("sid", self.sid)
        result = message.get("payload", {}).get("result", {})
        if result.get("text"):
            if result.get("compress", "raw") != "raw":
                raise SkillError("Unexpected compressed ASR result")
            try:
                decoded = json.loads(base64.b64decode(result["text"], validate=True).decode("utf-8"))
            except (ValueError, UnicodeError, TypeError):
                raise SkillError("Invalid base64/JSON ASR payload") from None
            if isinstance(decoded, dict):
                for field in ("pgs", "rg", "rst"):
                    if field in result and field not in decoded:
                        decoded[field] = result[field]
            self.add_piece(decoded)
        if header.get("status") == 2:
            self.server_done = True

    def add_piece(self, piece):
        if not isinstance(piece, dict):
            raise SkillError("ASR text payload must be an object")
        if piece.get("ret", 0) != 0:
            raise SkillError(f"ASR payload returned error code {piece.get('ret')}")
        sn = piece.get("sn")
        if not isinstance(sn, int) or isinstance(sn, bool):
            raise SkillError("ASR result missing integer sequence number")
        mode = piece.get("pgs", "apd")
        if mode not in ("apd", "rpl"):
            raise SkillError(f"Unknown ASR replacement mode {mode!r}")
        if mode == "rpl":
            rg = piece.get("rg")
            if not isinstance(rg, list) or len(rg) != 2 or any(not isinstance(x, int) or isinstance(x, bool) for x in rg) or rg[0] > rg[1]:
                raise SkillError("Invalid ASR replacement range")
            for key in list(self.pieces):
                if rg[0] <= key <= rg[1]:
                    del self.pieces[key]
        self.pieces[sn] = piece

    def text(self):
        parts = []
        for _, piece in sorted(self.pieces.items()):
            for word in piece.get("ws", []):
                candidates = word.get("cw", [])
                if candidates:
                    text = candidates[0].get("w", "")
                    if not isinstance(text, str):
                        raise SkillError("Invalid ASR word value")
                    parts.append(text)
        return "".join(parts)


def stream_chunk(pcm_path, credentials, response_path, context=None, res_id=None, timeout=120):
    try:
        import websocket
    except ImportError:
        raise SkillError("Install websocket-client before transcribe") from None
    app_id, key, secret = credentials
    try:
        ws = websocket.create_connection(build_auth_url(key, secret), timeout=timeout, enable_multithread=True)
    except Exception as exc:
        raise SkillError(f"ASR connection failed ({type(exc).__name__}); no automatic retry") from None
    accumulator = ResultAccumulator()
    errors, done = [], threading.Event()
    response_path = Path(response_path)
    # Never log the signed request URL or request headers.
    def receive():
        try:
            with response_path.open("w", encoding="utf-8") as log:
                while not done.is_set():
                    raw = ws.recv()
                    if not raw:
                        raise SkillError("ASR connection closed before final response")
                    message = json.loads(raw)
                    log.write(json.dumps(message, ensure_ascii=False) + "\n")
                    log.flush()
                    accumulator.add_message(message)
                    if accumulator.server_done:
                        break
        except Exception as exc:
            errors.append(exc if isinstance(exc, SkillError) else SkillError(f"ASR receive failed ({type(exc).__name__}); inspect saved responses"))
        finally:
            done.set()
    worker = threading.Thread(target=receive, daemon=True)
    worker.start()
    try:
        seq = 0
        with Path(pcm_path).open("rb") as handle:
            while True:
                data = handle.read(FRAME_BYTES)
                if not data:
                    break
                if done.is_set():
                    if errors:
                        raise errors[0]
                    raise SkillError("ASR finalized before the full chunk was sent; no automatic retry")
                frame = build_frame(app_id, data, seq, 0 if seq == 0 else 1, context, res_id)
                ws.send(json.dumps(frame, ensure_ascii=False))
                seq += 1
                time.sleep(0.04)
        if seq == 0:
            raise SkillError("Cannot transcribe an empty PCM file")
        ws.send(json.dumps(build_frame(app_id, b"", seq, 2, context, res_id)))
        if not done.wait(timeout):
            raise SkillError("ASR timed out after upload; billing outcome may be unknown; no automatic retry")
        if errors:
            raise errors[0]
        if not accumulator.server_done:
            raise SkillError("ASR final response is missing")
        return {"text": accumulator.text(), "sid": accumulator.sid, "raw_pieces": [piece for _, piece in sorted(accumulator.pieces.items())]}
    except SkillError:
        raise
    except Exception as exc:
        raise SkillError(f"ASR transport failed ({type(exc).__name__}); billing outcome may be unknown; no automatic retry") from None
    finally:
        done.set()
        ws.close()
        worker.join(timeout=3)


def timecode(seconds, srt=False):
    millis = max(0, round(seconds * 1000))
    hours, millis = divmod(millis, 3600000)
    minutes, millis = divmod(millis, 60000)
    sec, millis = divmod(millis, 1000)
    return f"{hours:02}:{minutes:02}:{sec:02}{',' if srt else '.'}{millis:03}"


def export_transcript(job, manifest, segments, origin, timing_precision, run_id=None):
    doc = {"schema_version": SCHEMA_VERSION, "source": manifest["source"], "source_sha256": manifest["source_sha256"], "duration": manifest["duration"], "origin": origin, "timing_precision": timing_precision, "run_id": run_id, "text_policy": "Unedited recognition/import output. ASR may normalize speech; verify against original audio before verbatim quotation.", "segments": segments, "created_at": now()}
    write_json(job / "transcript.json", doc)
    txt = "\n".join(f"[{timecode(s['start'])} - {timecode(s['end'])}] {s['text']}" for s in segments)
    write_text(job / "transcript.txt", txt + "\n")
    md = f"# 识别文本\n\n时间精度 `{timing_precision}`。此稿未经过 Agent 润色，ASR 本身可能规整口语。引用与裁切请回听原音。\n\n" + "\n\n".join(f"## {s['id']} · {timecode(s['start'])} 至 {timecode(s['end'])}\n\n{s['text']}" for s in segments) + "\n"
    write_text(job / "transcript.md", md)
    srt = "\n\n".join(f"{i}\n{timecode(s['start'], True)} --> {timecode(s['end'], True)}\n{s['text']}" for i, s in enumerate(segments, 1) if s["text"].strip())
    write_text(job / "transcript.srt", srt + "\n")
    write_json(job / "transcription_status.json", {"status": "complete", "origin": origin, "run_id": run_id, "finished_at": now()})
    return doc


def checked_job(job):
    job = Path(job).expanduser().resolve()
    manifest = read_json(job / "media.json")
    source = Path(manifest["source"])
    if not source.is_file() or sha256_file(source) != manifest["source_sha256"]:
        raise SkillError("Original media moved or changed; prepare again before continuing")
    return job, manifest


def credentials_from_env():
    names = ("XFYUN_APP_ID", "XFYUN_API_KEY", "XFYUN_API_SECRET")
    missing = [name for name in names if not os.environ.get(name)]
    if missing:
        raise SkillError("Missing environment variables: " + ", ".join(missing))
    return tuple(os.environ[name] for name in names)


def _transcribe_job_locked(job, context=None, res_id=None, retry_uncertain=False, timeout=120):
    job, manifest = checked_job(job)
    credentials = credentials_from_env()
    spec = {"model": "spark_asr_v2", "context": context, "res_id": res_id, "frame_bytes": FRAME_BYTES, "schema_version": SCHEMA_VERSION}
    run_id = hashlib.sha256(json.dumps(spec, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:16]
    run_dir = job / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    write_json(run_dir / "settings.json", spec)
    write_json(job / "transcription_status.json", {"status": "running", "run_id": run_id, "started_at": now()})
    segments = []
    for chunk in manifest["chunks"]:
        prefix = run_dir / f"{chunk['index']:05d}"
        state_path, result_path = prefix.with_suffix(".state.json"), prefix.with_suffix(".result.json")
        state = read_json(state_path) if state_path.exists() else None
        if state and state.get("status") == "succeeded" and result_path.exists():
            result = read_json(result_path)
            emit({"event": "chunk_cached", "job": str(job), "chunk": chunk["index"]})
        else:
            if state and not retry_uncertain:
                raise SkillError(f"Chunk {chunk['index']} has a previous incomplete/uncertain attempt. Inspect {state_path}; --retry-uncertain explicitly permits another possibly billable request. Completed chunks will be reused.")
            if state:
                archive_dir = run_dir / "previous-attempts" / f"{chunk['index']:05d}-{time.time_ns()}"
                archive_dir.mkdir(parents=True)
                for suffix in (".state.json", ".result.json", ".responses.jsonl"):
                    previous = prefix.with_suffix(suffix)
                    if previous.exists():
                        shutil.copy2(previous, archive_dir / previous.name)
            write_json(state_path, {"status": "started", "started_at": now(), "chunk": chunk, "billing": "Potentially billable; do not automatically retry"})
            try:
                result = stream_chunk(job / chunk["pcm"], credentials, prefix.with_suffix(".responses.jsonl"), context, res_id, timeout)
                write_json(result_path, result)
                write_json(state_path, {"status": "succeeded", "finished_at": now(), "chunk": chunk, "sid": result.get("sid")})
            except Exception as exc:
                message = str(exc) if isinstance(exc, SkillError) else f"Unexpected local failure ({type(exc).__name__})"
                write_json(state_path, {"status": "failed_or_unknown", "finished_at": now(), "chunk": chunk, "error": message, "billing": "Unknown; no automatic retry"})
                raise SkillError(message) from None
            emit({"event": "chunk_transcribed", "job": str(job), "chunk": chunk["index"], "text": result["text"]})
        # Stable offsets, deliberately conservative. No invented word-level alignment.
        segments.append({"id": f"seg-{chunk['index']:05d}", "start": round(chunk["start"], 6), "end": round(min(chunk["end"], manifest["duration"]), 6), "text": result["text"], "timing_precision": "chunk", "chunk_index": chunk["index"]})
        write_json(run_dir / "transcript.partial.json", {"status": "incomplete", "segments": segments, "completed_chunks": len(segments), "total_chunks": len(manifest["chunks"])})
    doc = export_transcript(job, manifest, segments, "xfyun-spark-asr-v2", "chunk", run_id)
    write_json(run_dir / "transcript.json", doc)
    write_json(run_dir / "transcript.partial.json", {"status": "complete", "segments": segments, "completed_chunks": len(segments), "total_chunks": len(manifest["chunks"])})
    return doc


def transcribe_job(job, context=None, res_id=None, retry_uncertain=False, timeout=120):
    job = Path(job).expanduser().resolve()
    if not (job / "media.json").is_file():
        raise SkillError("Prepare the media before transcribing")
    lock = job / ".transcribe.lock"
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        raise SkillError(f"Transcription lock exists at {lock}. Another process may be active. If it crashed, verify its PID is no longer running before removing the lock; uncertain chunks still require explicit retry.") from None
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump({"pid": os.getpid(), "started_at": now()}, handle)
        return _transcribe_job_locked(job, context, res_id, retry_uncertain, timeout)
    except BaseException:
        write_json(job / "transcription_status.json", {"status": "failed_or_interrupted", "finished_at": now(), "note": "Previous transcript files may belong to an earlier completed run. Resume/resolve this attempt or explicitly import the intended transcript before clipping."})
        raise
    finally:
        lock.unlink(missing_ok=True)


def number(value, name):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise SkillError(f"{name} must be a finite number")
    return float(value)


def validate_segments(data, duration):
    segments = data.get("segments") if isinstance(data, dict) else data
    if not isinstance(segments, list) or not segments:
        raise SkillError("Transcript must contain a nonempty segments array")
    clean, seen, previous = [], set(), -1.0
    for index, s in enumerate(segments):
        if not isinstance(s, dict):
            raise SkillError("Each segment must be an object")
        start, end = number(s.get("start"), "start"), number(s.get("end"), "end")
        if not 0 <= start < end <= duration + 0.05 or start < previous:
            raise SkillError(f"Invalid/unordered timestamps in segment {index}")
        previous = start
        if not isinstance(s.get("text"), str):
            raise SkillError(f"Segment {index} needs text")
        sid = str(s.get("id", f"seg-{index:05d}"))
        if sid in seen:
            raise SkillError(f"Duplicate segment id {sid}")
        seen.add(sid)
        segment = {"id": sid, "start": start, "end": min(end, duration), "text": s["text"], "timing_precision": str(s.get("timing_precision", "external-unverified"))}
        if "words" in s:
            if not isinstance(s["words"], list):
                raise SkillError(f"Segment {sid} words must be an array")
            words = []
            for word in s["words"]:
                if not isinstance(word, dict) or not isinstance(word.get("text"), str):
                    raise SkillError(f"Segment {sid} has invalid word text")
                word_start, word_end = number(word.get("start"), "word start"), number(word.get("end"), "word end")
                if not start <= word_start <= word_end <= min(end + 0.25, duration + 0.25):
                    raise SkillError(f"Segment {sid} has invalid word timestamps")
                words.append({**word, "start": word_start, "end": word_end})
            segment["words"] = words
        clean.append(segment)
    return clean


def validate_clips(data, duration, segment_ids=None):
    clips = data.get("clips") if isinstance(data, dict) else None
    if not isinstance(clips, list) or not clips:
        raise SkillError("Highlight manifest must contain a nonempty clips array")
    clean, seen = [], set()
    for i, clip in enumerate(clips):
        if not isinstance(clip, dict):
            raise SkillError("Each clip must be an object")
        start, end = number(clip.get("start"), "start"), number(clip.get("end"), "end")
        if not 0 <= start < end <= duration:
            raise SkillError(f"Clip {i} is outside media duration")
        cid = str(clip.get("id", f"highlight-{i + 1:02d}"))
        if cid in seen:
            raise SkillError(f"Duplicate clip id {cid}")
        seen.add(cid)
        evidence = clip.get("segment_ids", [])
        if not isinstance(evidence, list) or any(not isinstance(x, str) for x in evidence):
            raise SkillError("segment_ids must be an array of strings")
        if segment_ids is not None and any(sid not in segment_ids for sid in evidence):
            raise SkillError(f"Clip {cid} refers to unknown transcript segments")
        item = {"id": cid, "start": start, "end": end, "title": str(clip.get("title", cid)), "reason": str(clip.get("reason", "")), "segment_ids": evidence}
        if "summary" in clip:
            summary = clip["summary"]
            if not isinstance(summary, str) or not summary.strip():
                raise SkillError(f"Clip {cid} summary must be a nonempty string when supplied")
            item.update({"summary": summary.strip(), "summary_origin": "manifest"})
        clean.append(item)
    return clean


def clip_transcript_excerpt(segments, start, end):
    """Use timed words when complete; otherwise label the excerpt as context."""
    blocks, scopes, boundary_words, context_ids = [], set(), [], []
    for segment in segments:
        if segment["end"] <= start or segment["start"] >= end:
            continue
        words = segment.get("words")
        if not words or "".join(w["text"] for w in words) != segment["text"]:
            blocks.append(segment["text"])
            scopes.add("overlapping-context")
            context_ids.append(segment["id"])
            continue
        selected = []
        scopes.add("word-overlap")
        for word in words:
            wb, we = word["start"], word["end"]
            overlaps = we > start and wb < end if wb < we else start < wb <= end
            if overlaps:
                selected.append(word["text"])
                if wb < start < we or wb < end < we:
                    boundary_words.append(word["text"])
        if selected:
            blocks.append("".join(selected))
    scope = next(iter(scopes)) if len(scopes) == 1 else ("mixed-word-and-context" if scopes else "none")
    return {"transcript_excerpt": "\n".join(blocks), "transcript_excerpt_scope": scope, "excerpt_boundary_words": boundary_words, "excerpt_context_segment_ids": context_ids, "excerpt_review_required": True}


def markdown_text(value):
    """Render manifest prose as text, without interpreting embedded links or HTML."""
    text = str(value).replace("\r\n", "\n").replace("\r", "\n")
    return re.sub(r"([\\`*_\[\]<>#!|])", r"\\\1", text)


def highlights_markdown(result):
    """Keep editorial summaries separate from the source recognition transcript."""
    lines = ["# 高光片段与内容摘要", ""]
    if result["status"] != "complete":
        lines.extend(["本次裁切尚未全部完成。以下仅列出已导出的片段，请核对 clips.json 的执行状态。", ""])
    lines.extend(["摘要由清单作者依据转写整理，脚本不调用摘要模型。摘要用于概括，引用原话和发布前请回听音频，核对数字、限定条件及切片边界。", "",
                  f"原素材 {markdown_text(result['source'])}", ""])
    for clip in result["clips"]:
        title = markdown_text(clip["title"]).replace("\n", " ")
        lines.extend([f"## {title}", "", f"时间 {timecode(clip['start'])} 至 {timecode(clip['end'])}", ""])
        if "summary" in clip:
            lines.extend([markdown_text(clip["summary"]), ""])
        else:
            lines.extend(["尚未提供内容摘要。请由 Agent 阅读对应转写后补入清单，再执行裁切导出。", ""])
        if clip.get("reason"):
            lines.extend([f"选择依据 {markdown_text(clip['reason'])}", ""])
        filename = Path(clip["file"]).name
        lines.extend([f"[打开切片]({filename})", "",
                      "原文摘录供校对。摘录可能包含裁切边界附近的上下文，不能直接视为逐字字幕。", "",
                      markdown_text(clip.get("transcript_excerpt", "")), ""])
    return "\n".join(lines)


def cut_clips(job, manifest_path, audio_only=False):
    require_media_tools()
    job, media = checked_job(job)
    doc = read_json(manifest_path)
    if doc.get("source_sha256") and doc["source_sha256"] != media["source_sha256"]:
        raise SkillError("Highlight manifest belongs to different source media")
    status_path = job / "transcription_status.json"
    if status_path.exists() and read_json(status_path).get("status") != "complete":
        raise SkillError("Latest transcription/import is incomplete; do not use potentially stale transcript files")
    transcript = read_json(job / "transcript.json")
    if transcript.get("source_sha256") != media["source_sha256"]:
        raise SkillError("Transcript belongs to different source media")
    segments = validate_segments(transcript, media["duration"])
    clips = validate_clips(doc, media["duration"], {s["id"] for s in segments})
    output_dir = job / "clips"
    output_dir.mkdir(exist_ok=True)
    results = []
    video = media["has_video"] and not audio_only
    mode = "video" if video else "audio"
    partial_path = output_dir / "clips.partial.json"
    pending = {"status": "incomplete", "mode": mode, "source": media["source"], "source_sha256": media["source_sha256"], "clips": results}
    # Reset current-run state before encoding, even if an earlier run completed.
    write_json(partial_path, pending)
    write_json(output_dir / "clips.json", pending)
    write_text(output_dir / "highlights.md", highlights_markdown(pending))
    for clip in clips:
        request_hash = hashlib.sha256(json.dumps([media["source_sha256"], clip["start"], clip["end"], video]).encode()).hexdigest()[:10]
        filename = f"{slug(clip['id'])}-{request_hash}.{'mp4' if video else 'mp3'}"
        target = output_dir / filename
        temp = target.with_name(target.stem + ".partial" + target.suffix)
        duration = clip["end"] - clip["start"]
        command = ["ffmpeg", "-v", "error", "-nostdin", "-y", "-ss", str(clip["start"]), "-i", media["source"], "-t", str(duration)]
        if video:
            command += ["-map", "0:v:0", "-map", "0:a:0", "-c:v", "libx264", "-preset", "medium", "-crf", "18", "-vf", "pad=ceil(iw/2)*2:ceil(ih/2)*2", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart"]
        else:
            command += ["-map", "0:a:0", "-vn", "-c:a", "libmp3lame", "-b:a", "192k"]
        command += ["-map_metadata", "-1", str(temp)]
        run_media(command)
        actual = probe_media(temp)["duration"]
        if abs(actual - duration) > 0.25:
            raise SkillError(f"Clip duration validation failed for {clip['id']}; temporary output retained")
        temp.replace(target)
        results.append({**clip, "file": str(target), "actual_duration": actual, **clip_transcript_excerpt(segments, clip["start"], clip["end"]), "boundary_review_required": True})
        write_json(partial_path, pending)
        write_json(output_dir / "clips.json", pending)
        write_text(output_dir / "highlights.md", highlights_markdown(pending))
    result = {"status": "complete", "mode": mode, "source": media["source"], "source_sha256": media["source_sha256"], "timing_precision": transcript.get("timing_precision"), "clips": results, "highlights_markdown": str(output_dir / f"highlights-{mode}.md")}
    report = highlights_markdown(result)
    write_text(output_dir / f"highlights-{mode}.md", report)
    write_text(output_dir / "highlights.md", report)
    write_json(output_dir / f"clips-{mode}.json", result)
    write_json(output_dir / "clips.json", result)
    partial_path.unlink(missing_ok=True)
    return result


FILE_API_HOST = "office-api-ist-dx.iflyaisol.com"
FILE_ASR_MAX_BYTES = 500_000_000
FILE_ASR_MAX_SECONDS = 5 * 60 * 60
FILE_ASR_DOMAINS = ("court", "finance", "medical", "tech", "sport", "edu", "isp", "gov", "game", "ecom", "mil", "com", "life", "ent", "culture", "car")


def file_api_datetime():
    return datetime.now().astimezone().strftime("%Y-%m-%dT%H:%M:%S%z")


def java_urlencode(value):
    # Match java.net.URLEncoder used by the official HMAC-SHA1 sample.
    return quote_plus(str(value), safe="*").replace("~", "%7E")


def file_signature_base(params):
    return "&".join(f"{java_urlencode(key)}={java_urlencode(value)}" for key, value in sorted(params.items()) if key != "signature" and value is not None and str(value) != "")


def build_file_signature(params, api_secret):
    return base64.b64encode(hmac.new(api_secret.encode("utf-8"), file_signature_base(params).encode("utf-8"), hashlib.sha1).digest()).decode("ascii")


def file_api_request(endpoint, params, api_secret, raw_path, audio_path=None, timeout=120):
    """One HTTPS attempt only. Never expose a signed URL, key, or signature in errors."""
    if endpoint not in ("/v2/upload", "/v2/getResult"):
        raise SkillError("Unsupported file ASR endpoint")
    headers = {"signature": build_file_signature(params, api_secret), "Accept": "application/json"}
    connection = http.client.HTTPSConnection(FILE_API_HOST, timeout=timeout)
    try:
        target = endpoint + "?" + urlencode(params)
        if audio_path is not None:
            headers.update({"Content-Type": "application/octet-stream", "Content-Length": str(Path(audio_path).stat().st_size)})
            with Path(audio_path).open("rb") as body:
                connection.request("POST", target, body=body, headers=headers)
        else:
            headers.update({"Content-Type": "application/json", "Content-Length": "2"})
            connection.request("POST", target, body=b"{}", headers=headers)
        response = connection.getresponse()
        body = response.read(64 * 1024 * 1024 + 1)
        if len(body) > 64 * 1024 * 1024:
            raise SkillError("File ASR response exceeded the 64 MB local limit")
        text = body.decode("utf-8", errors="replace")
        # Retain response evidence, with any reflected credential values redacted.
        safe_text = text
        for value in (api_secret, params.get("accessKeyId"), params.get("appId"), headers["signature"]):
            if value:
                safe_text = safe_text.replace(str(value), "[REDACTED]")
        write_text(raw_path, safe_text)
        if response.status != 200:
            raise SkillError(f"File ASR HTTP status {response.status}; response saved; no automatic resubmission")
        try:
            value = json.loads(text)
        except ValueError:
            raise SkillError("File ASR returned invalid JSON; response saved") from None
        if not isinstance(value, dict):
            raise SkillError("File ASR response must be an object")
        return value
    except SkillError:
        raise
    except Exception as exc:
        raise SkillError(f"File ASR request failed ({type(exc).__name__}); outcome may be unknown; no automatic resubmission") from None
    finally:
        connection.close()


def require_file_success(response):
    if not isinstance(response, dict) or "code" not in response:
        raise SkillError("File ASR response is missing code")
    code = response["code"]
    if not ((isinstance(code, str) and code == "000000") or (type(code) is int and code == 0)):
        shown = str(code) if re.fullmatch(r"[0-9]{1,12}", str(code)) else "invalid-code"
        raise SkillError(f"File ASR returned error code {shown}; inspect the locally saved response")
    content = response.get("content")
    if not isinstance(content, dict):
        raise SkillError("Successful file ASR response is missing content")
    return content


def parse_upload_order(response):
    content = require_file_success(response)
    order_id = content.get("orderId")
    if not isinstance(order_id, str) or not order_id.strip():
        raise SkillError("File ASR upload response has no orderId; do not resubmit automatically")
    return order_id


def protocol_integer(value, name):
    if isinstance(value, bool):
        raise SkillError(f"Invalid {name}")
    if isinstance(value, int):
        return value
    if isinstance(value, str) and re.fullmatch(r"-?\d+", value):
        return int(value)
    raise SkillError(f"Invalid or missing {name}")


def query_file_status(response, order_id):
    if isinstance(response, dict) and response.get("code") in ("100013", 100013):
        return "processing", None  # Documented 'order not finished', never a success.
    content = require_file_success(response)
    info = content.get("orderInfo")
    if not isinstance(info, dict) or info.get("orderId") != order_id:
        raise SkillError("File ASR query returned a missing or mismatched orderId")
    status = protocol_integer(info.get("status"), "order status")
    failure = protocol_integer(info.get("failType"), "failType")
    if failure != 0 or status == -1:
        raise SkillError(f"File ASR order failed (status={status}, failType={failure}); no resubmission")
    if status in (0, 3):
        return "processing", None
    if status != 4:
        raise SkillError(f"Unknown file ASR order status {status}")
    result = content.get("orderResult")
    if not result:
        raise SkillError("Completed file ASR order has an empty orderResult")
    return "complete", result


def parse_file_transcript(order_result, source_duration):
    try:
        decoded = json.loads(order_result) if isinstance(order_result, str) else order_result
    except ValueError:
        raise SkillError("orderResult is not valid JSON") from None
    if not isinstance(decoded, dict):
        raise SkillError("orderResult must contain an object")
    # lattice2 is the original transcript when server-side smoothing is enabled.
    lattice = decoded.get("lattice2") if "lattice2" in decoded else decoded.get("lattice")
    if not isinstance(lattice, list) or not lattice:
        raise SkillError("Completed file ASR result has no original transcript lattice")
    segments = []
    for entry in lattice:
        if not isinstance(entry, dict):
            raise SkillError("Malformed lattice entry")
        try:
            one = entry.get("json_1best")
            one = json.loads(one) if isinstance(one, str) else one
        except ValueError:
            raise SkillError("Invalid json_1best payload") from None
        st = one.get("st") if isinstance(one, dict) else None
        if not isinstance(st, dict):
            raise SkillError("Missing sentence object in file ASR result")
        bg_ms = protocol_integer(st.get("bg"), "sentence bg milliseconds")
        ed_ms = protocol_integer(st.get("ed"), "sentence ed milliseconds")
        start, end = bg_ms / 1000, ed_ms / 1000
        if not 0 <= start < end <= source_duration + 0.25 or start >= source_duration:
            raise SkillError("Sentence timestamp is outside the source media timeline")
        words, texts = [], []
        rt = st.get("rt")
        if not isinstance(rt, list):
            raise SkillError("Missing sentence recognition tokens")
        for recognition in rt:
            if not isinstance(recognition, dict) or not isinstance(recognition.get("ws"), list):
                raise SkillError("Malformed sentence word collection")
            for word in recognition["ws"]:
                if not isinstance(word, dict) or not isinstance(word.get("cw"), list) or not word["cw"]:
                    raise SkillError("Missing word candidates")
                candidate = word["cw"][0]
                if not isinstance(candidate, dict) or not isinstance(candidate.get("w"), str):
                    raise SkillError("Malformed original word text")
                word_text = candidate["w"]
                wb = protocol_integer(word.get("wb"), "word begin 10ms frame")
                we = protocol_integer(word.get("we"), "word end 10ms frame")
                if not 0 <= wb <= we:
                    raise SkillError("Invalid word frame range")
                word_start = (bg_ms + wb * 10) / 1000
                word_end = (bg_ms + we * 10) / 1000
                if word_end > end + 0.05 or word_start > source_duration + 0.05:
                    raise SkillError("Word timestamp exceeds sentence/source boundary")
                texts.append(word_text)
                if word_text:
                    words.append({"text": word_text, "start": word_start, "end": word_end, "property": candidate.get("wp"), "wb_frames": wb, "we_frames": we})
        text = "".join(texts)
        if not text.strip():
            continue
        segment = {"start": start, "end": min(end, source_duration), "text": text, "timing_precision": "sentence", "words": words, "source_bg_ms": bg_ms, "source_ed_ms": ed_ms}
        if st.get("rl") is not None:
            segment["speaker"] = str(st["rl"])
        if end > source_duration:
            segment["end_clamped_to_source"] = True
        segments.append(segment)
    if not segments:
        raise SkillError("Completed file ASR response contains no recognized text")
    segments.sort(key=lambda s: (s["start"], s["end"]))
    for index, segment in enumerate(segments):
        segment["id"] = f"seg-{index:05d}"
    return segments


def validate_file_options(language, role_type, role_num, feature_ids, domain):
    if language not in ("autodialect", "autominor"):
        raise SkillError("File ASR language must be autodialect or autominor")
    if role_type not in (0, 1, 3) or not 0 <= role_num <= 10:
        raise SkillError("Invalid file ASR role configuration")
    if role_num and role_type == 0:
        raise SkillError("--role-num requires --role-type 1 or 3")
    if role_type == 3:
        ids = [s.strip() for s in (feature_ids or "").split(",") if s.strip()]
        if not 1 <= len(ids) <= 64:
            raise SkillError("Voiceprint separation needs 1..64 registered --feature-ids")
    elif feature_ids:
        raise SkillError("--feature-ids requires --role-type 3")
    if domain and domain not in FILE_ASR_DOMAINS:
        raise SkillError("Unsupported file ASR domain")


def _transcribe_file_locked(job, language="autodialect", role_type=0, role_num=0, feature_ids=None, domain=None, max_polls=60, poll_interval=5, timeout=120, resume=False, retry_uncertain=False):
    job, media = checked_job(job)
    validate_file_options(language, role_type, role_num, feature_ids, domain)
    if not isinstance(max_polls, int) or max_polls < 1 or not math.isfinite(poll_interval) or not 0 <= poll_interval <= 60 or not math.isfinite(timeout) or timeout <= 0:
        raise SkillError("Invalid polling controls; use max-polls >= 1, interval 0..60 seconds, positive timeout")
    upload_path = job / media["mp3"]
    upload_info = probe_media(upload_path)
    if upload_info["duration"] > FILE_ASR_MAX_SECONDS or upload_path.stat().st_size > FILE_ASR_MAX_BYTES:
        raise SkillError("File ASR accepts at most 5 hours and 500 MB per upload")
    spec = {"backend": "recording-file-asr-llm", "language": language, "roleType": role_type, "roleNum": role_num, "featureIds": feature_ids, "pd": domain, "eng_smoothproc": False, "eng_colloqproc": False, "upload_sha256": sha256_file(upload_path)}
    run_id = hashlib.sha256(json.dumps(spec, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:16]
    run_dir = job / "file-asr" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    state_path = run_dir / "state.json"
    result_path = run_dir / "transcript.json"
    state = read_json(state_path) if state_path.exists() else None
    if state and state.get("status") == "complete" and result_path.exists():
        saved = read_json(result_path)
        export_transcript(job, media, saved["segments"], "xfyun-recording-file-asr-llm", "sentence-with-word-timestamps", run_id)
        return {"status": "complete", "job": str(job), "order_id": state["order_id"], "cached": True, "run_id": run_id}
    write_json(run_dir / "settings.json", spec)
    # Recover a successful response saved just before a process crash, without re-upload.
    if state and not state.get("order_id") and state.get("upload_response"):
        saved_response = run_dir / state["upload_response"]
        if saved_response.exists():
            try:
                recovered = parse_upload_order(read_json(saved_response))
            except SkillError:
                pass
            else:
                state.update({"order_id": recovered, "status": "submitted", "recovered_from_saved_response": True})
                write_json(state_path, state)
    if resume and (not state or not state.get("order_id")):
        raise SkillError("No persisted orderId for these exact options; --resume will not upload")
    credentials = credentials_from_env()
    app_id, api_key, api_secret = credentials
    if not state or not state.get("order_id"):
        if state and not retry_uncertain:
            raise SkillError("Previous file upload has an unknown or rejected outcome; no automatic re-upload. Inspect the saved response and order status. --retry-uncertain explicitly permits another possibly billable submission.")
        if state:
            write_json(run_dir / f"previous-state-{time.time_ns()}.json", state)
        nonce = "".join(secrets.choice(string.ascii_letters + string.digits) for _ in range(16))
        state = {"status": "upload_started", "signature_random": nonce, "started_at": now(), "upload_response": f"upload-{time.time_ns()}.json", "run_id": run_id}
        write_json(state_path, state)
        write_json(job / "transcription_status.json", {"status": "running", "backend": "recording-file-asr-llm", "run_id": run_id})
        params = {"appId": app_id, "accessKeyId": api_key, "dateTime": file_api_datetime(), "signatureRandom": nonce, "fileSize": str(upload_path.stat().st_size), "fileName": upload_path.name, "durationCheckDisable": "false", "duration": str(round(upload_info["duration"] * 1000)), "language": language, "audioMode": "fileStream", "eng_smoothproc": "false", "eng_colloqproc": "false", "roleType": str(role_type), "roleNum": str(role_num)}
        if feature_ids:
            params["featureIds"] = feature_ids
        if domain:
            params["pd"] = domain
        try:
            response = file_api_request("/v2/upload", params, api_secret, run_dir / state["upload_response"], upload_path, timeout)
            order_id = parse_upload_order(response)
        except SkillError:
            state.update({"status": "submission_unknown_or_rejected", "updated_at": now()})
            write_json(state_path, state)
            raise
        state.update({"order_id": order_id, "status": "submitted", "submitted_at": now()})
        # Persist the order identity BEFORE making even the first poll request.
        write_json(state_path, state)
        emit({"event": "file_asr_submitted", "job": str(job), "order_id": order_id})
    order_id = state["order_id"]
    nonce = state["signature_random"]
    write_json(job / "transcription_status.json", {"status": "running", "backend": "recording-file-asr-llm", "run_id": run_id, "order_id": order_id})
    for poll_index in range(max_polls):
        params = {"accessKeyId": api_key, "dateTime": file_api_datetime(), "signatureRandom": nonce, "orderId": order_id, "resultType": "transfer"}
        try:
            response = file_api_request("/v2/getResult", params, api_secret, run_dir / f"poll-{time.time_ns()}.json", timeout=timeout)
            status, order_result = query_file_status(response, order_id)
        except SkillError:
            state.update({"status": "query_error", "updated_at": now()})
            write_json(state_path, state)
            raise
        state.update({"status": status, "updated_at": now()})
        write_json(state_path, state)
        emit({"event": "file_asr_progress", "job": str(job), "order_id": order_id, "status": status})
        if status == "complete":
            try:
                segments = parse_file_transcript(order_result, media["duration"])
            except SkillError:
                state.update({"status": "result_parse_error", "updated_at": now()})
                write_json(state_path, state)
                raise
            result = export_transcript(job, media, segments, "xfyun-recording-file-asr-llm", "sentence-with-word-timestamps", run_id)
            write_json(result_path, result)
            state.update({"status": "complete", "completed_at": now()})
            write_json(state_path, state)
            return {"status": "complete", "job": str(job), "order_id": order_id, "run_id": run_id, "segments": len(segments), "timing_precision": "sentence-with-word-timestamps"}
        if poll_index + 1 < max_polls:
            time.sleep(poll_interval)
    write_json(job / "transcription_status.json", {"status": "pending", "backend": "recording-file-asr-llm", "run_id": run_id, "order_id": order_id})
    return {"status": "pending", "job": str(job), "order_id": order_id, "run_id": run_id, "next_action": "rerun the same command with --resume; no upload will be repeated"}


def transcribe_file(job, **kwargs):
    job = Path(job).expanduser().resolve()
    if not (job / "media.json").is_file():
        raise SkillError("Prepare the media before transcribing")
    lock = job / ".transcribe.lock"
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        raise SkillError(f"Transcription lock exists at {lock}; resolve the active or interrupted process first") from None
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump({"pid": os.getpid(), "started_at": now(), "backend": "recording-file-asr-llm"}, handle)
        return _transcribe_file_locked(job, **kwargs)
    except BaseException:
        write_json(job / "transcription_status.json", {"status": "failed_or_interrupted", "backend": "recording-file-asr-llm", "finished_at": now(), "note": "Keep the persisted file-asr state. Known orderIds resume queries; unknown uploads must not be automatically repeated."})
        raise
    finally:
        lock.unlink(missing_ok=True)


def load_context(path):
    if not path:
        return None
    context = read_json(path)
    if not isinstance(context, dict):
        raise SkillError("Context file must contain an object")
    if len(json.dumps(context, ensure_ascii=False, separators=(",", ":")).encode()) > 2048:
        raise SkillError("Context exceeds the API 2 KB limit")
    return context


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("doctor", help="Check tools/dependency/credential presence without network calls")
    prep = sub.add_parser("prepare", aliases=["preprocess"], help="Create MP3, PCM and short chunks locally")
    prep.add_argument("inputs", nargs="+")
    prep.add_argument("--out", required=True, help="Root folder for independently named media jobs")
    prep.add_argument("--chunk-seconds", type=float, default=20)
    trans = sub.add_parser("transcribe", help="Stream one or more prepared jobs to ASR V2; potentially billable")
    trans.add_argument("jobs", nargs="+", help="Prepared job folders containing media.json")
    trans.add_argument("--context", help="Optional context JSON (maximum 2 KB)")
    trans.add_argument("--res-id", help="Optional console-provisioned application hotword resource")
    trans.add_argument("--timeout", type=float, default=120)
    trans.add_argument("--retry-uncertain", action="store_true", help="Explicitly allow another request for failed/unknown chunks; it may bill again")
    file_trans = sub.add_parser("transcribe-file", help="Recommended: upload MP3 to recording-file ASR LLM and resume order polling")
    file_trans.add_argument("--job", required=True)
    file_trans.add_argument("--language", choices=["autodialect", "autominor"], default="autodialect")
    file_trans.add_argument("--role-type", type=int, choices=[0, 1, 3], default=0)
    file_trans.add_argument("--role-num", type=int, default=0)
    file_trans.add_argument("--feature-ids", help="Registered voiceprint IDs for role-type 3")
    file_trans.add_argument("--domain", choices=FILE_ASR_DOMAINS)
    file_trans.add_argument("--max-polls", type=int, default=60)
    file_trans.add_argument("--poll-interval", type=float, default=5)
    file_trans.add_argument("--timeout", type=float, default=120)
    file_trans.add_argument("--resume", action="store_true", help="Query a persisted order only; never upload")
    file_trans.add_argument("--retry-uncertain", action="store_true", help="Explicitly allow re-submission when upload outcome is unknown; may bill again")
    imp = sub.add_parser("import-transcript", help="Import existing timestamped JSON without calling ASR")
    imp.add_argument("--job", required=True)
    imp.add_argument("--input", required=True)
    clip = sub.add_parser("clip", help="Cut Agent-selected highlight intervals from original media")
    clip.add_argument("--job", required=True)
    clip.add_argument("--manifest", required=True)
    clip.add_argument("--audio-only", action="store_true", help="Export MP3 even for source video")
    return p


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        if args.command == "doctor":
            checks = {"python": sys.version.split()[0], "ffmpeg": shutil.which("ffmpeg"), "ffprobe": shutil.which("ffprobe"), "websocket_client": importlib.util.find_spec("websocket") is not None, "credentials_present": {key: bool(os.environ.get(key)) for key in ("XFYUN_APP_ID", "XFYUN_API_KEY", "XFYUN_API_SECRET")}, "network_called": False}
            emit(checks)
            return 0 if checks["ffmpeg"] and checks["ffprobe"] else 1
        if args.command in ("prepare", "preprocess"):
            failures = []
            for source in args.inputs:
                try:
                    job = prepare_one(source, args.out, args.chunk_seconds)
                    emit({"status": "prepared", "source": str(Path(source).resolve()), "job": str(job), "network_called": False})
                except SkillError as exc:
                    failures.append(str(exc))
                    emit({"status": "failed", "source": source, "error": str(exc)})
            return 1 if failures else 0
        if args.command == "transcribe":
            if not math.isfinite(args.timeout) or args.timeout <= 0:
                raise SkillError("Timeout must be a positive finite value")
            credentials_from_env()
            context = load_context(args.context)
            # Stop the batch at the first failed/uncertain request; never silently continue spending.
            for job in args.jobs:
                doc = transcribe_job(job, context, args.res_id, args.retry_uncertain, args.timeout)
                emit({"status": "transcribed", "job": str(Path(job).resolve()), "segments": len(doc["segments"]), "timing_precision": "chunk"})
            return 0
        if args.command == "transcribe-file":
            result = transcribe_file(args.job, language=args.language, role_type=args.role_type, role_num=args.role_num, feature_ids=args.feature_ids, domain=args.domain, max_polls=args.max_polls, poll_interval=args.poll_interval, timeout=args.timeout, resume=args.resume, retry_uncertain=args.retry_uncertain)
            emit(result)
            return 0 if result["status"] == "complete" else 2
        if args.command == "import-transcript":
            job, media = checked_job(args.job)
            data = read_json(args.input)
            if isinstance(data, dict) and data.get("source_sha256") and data["source_sha256"] != media["source_sha256"]:
                raise SkillError("Imported transcript belongs to a different source")
            segments = validate_segments(data, media["duration"])
            precision = data.get("timing_precision", "external-unverified") if isinstance(data, dict) else "external-unverified"
            export_transcript(job, media, segments, "imported", precision)
            emit({"status": "imported", "job": str(job), "segments": len(segments), "network_called": False})
            return 0
        if args.command == "clip":
            emit(cut_clips(args.job, args.manifest, args.audio_only))
            return 0
    except SkillError as exc:
        emit({"status": "error", "error": str(exc)})
        return 1
    except (OSError, KeyError, TypeError, ValueError) as exc:
        emit({"status": "error", "error": f"Local input or filesystem failure ({type(exc).__name__}); no automatic retry"})
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
