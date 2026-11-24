#!/usr/bin/env python3
"""Dual-voice podcast production with XFYun super-human TTS and offline assembly."""
from __future__ import annotations
import argparse
import base64
from contextlib import contextmanager
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unicodedata
import wave

ENDPOINT = 'wss://cbm01.cn-huabei-1.xf-yun.com/v1/private/mcd9m97e6'
RATE = 24000
SAMPLE_WIDTH = 2
MAX_REQUEST_BYTES = 12000  # Well below the documented 64 KiB total per session.
MAX_AUDIO_BYTES = 256 * 1024 * 1024
ID_PATTERN = re.compile(r'^[A-Za-z0-9_-]{1,80}$')

class PodcastError(Exception):
    pass


def safe_provider_message(value, sensitive_values=()):
    """Retain service diagnostics without request headers, tokens, or credentials."""
    if not isinstance(value, str):
        return ''
    message = value
    for secret in sensitive_values:
        if secret:
            message = message.replace(secret, '[REDACTED]')
    message = re.sub(r'(?i)(?:https?|wss?)://\S+', '[URL REDACTED]', message)
    message = re.sub(r'(?i)(authorization|x-api-key|api[_-]?key|api[_-]?secret|password|token)\s*[:=]\s*[^,;\s]+', r'\1=[REDACTED]', message)
    message = re.sub(r'[A-Za-z0-9+/_=\-]{24,}', '[REDACTED]', message)
    return ' '.join(message.split())[:500]


class ProviderError(PodcastError):
    def __init__(self, code, message, sensitive_values=()):
        self.provider_code = code
        self.provider_message = safe_provider_message(message, sensitive_values)
        detail = f': {self.provider_message}' if self.provider_message else ''
        super().__init__(f'TTS provider rejected the request (code {code}){detail}; inspect the console')


@contextmanager
def render_lock(output_dir):
    """An exclusive file survives a crashed process and requires manual review."""
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / '.render.lock'
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        raise PodcastError(f'Render output is locked: {path}. Check the owning process before manually removing a stale lock; no request was sent.') from None
    identity = os.fstat(descriptor)
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8') as handle:
            json.dump({'pid': os.getpid(), 'created_at': time.time()}, handle)
            handle.write('\n')
            handle.flush()
            os.fsync(handle.fileno())
        yield
    finally:
        # Release only the file created by this process, even if it was replaced.
        try:
            current = path.stat()
            if (current.st_dev, current.st_ino) == (identity.st_dev, identity.st_ino):
                path.unlink()
        except FileNotFoundError:
            pass


def atomic_json(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + '.tmp')
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    temp.replace(path)


def require_tools():
    for tool in ('ffmpeg', 'ffprobe'):
        if not shutil.which(tool):
            raise PodcastError(f'Missing dependency: {tool}')


def bounded_int(value, low, high, name):
    if type(value) is not int or not low <= value <= high:
        raise PodcastError(f'{name} must be an integer in [{low}, {high}]')
    return value


def plain_text(value, where):
    if not isinstance(value, str) or not value.strip():
        raise PodcastError(f'{where}: non-empty text required')
    for char in value:
        n = ord(char)
        if unicodedata.category(char).startswith('C') and char not in '\n\r':
            raise PodcastError(f'{where}: control/invisible characters are not supported')
        if n > 0xFFFF or 0x2600 <= n <= 0x27BF or n in (0xFE0F, 0x20E3):
            raise PodcastError(f'{where}: remove emoji before synthesis')
    if re.search(r'<[^>]+>|\[[^\]]+\]\([^\)]+\)|[`*#]', value):
        raise PodcastError(f'{where}: convert Markdown/HTML to spoken plain text')
    return value.replace('\r\n', '\n').replace('\r', '\n')


def load_script(path):
    script_path = Path(path).resolve()
    try:
        doc = json.loads(script_path.read_text(encoding='utf-8'))
    except (OSError, ValueError) as exc:
        raise PodcastError(f'Cannot read script JSON: {type(exc).__name__}') from None
    if not isinstance(doc, dict) or doc.get('schema_version', 1) != 1:
        raise PodcastError('Expected script object with schema_version 1')
    if not isinstance(doc.get('title'), str) or not doc['title'].strip():
        raise PodcastError('title must be a non-empty string')
    speakers = doc.get('speakers')
    if not isinstance(speakers, dict) or len(speakers) != 2:
        raise PodcastError('Exactly two speakers are required')
    clean_speakers = {}
    for name, spec in speakers.items():
        if not ID_PATTERN.fullmatch(name) or not isinstance(spec, dict):
            raise PodcastError('Speaker keys must be safe identifiers; values must be objects')
        voice = spec.get('voice')
        if not isinstance(voice, str) or not ID_PATTERN.fullmatch(voice):
            raise PodcastError(f'{name}: provide an authorized XFYun voice code')
        clean_speakers[name] = {
            'voice': voice, 'name': str(spec.get('name', name)),
            'speed': bounded_int(spec.get('speed', 50), 0, 100, 'speed'),
            'volume': bounded_int(spec.get('volume', 50), 0, 100, 'volume'),
            'pitch': bounded_int(spec.get('pitch', 50), 0, 100, 'pitch'),
            'reg': bounded_int(spec.get('reg', 0), 0, 2, 'reg'),
            'rdn': bounded_int(spec.get('rdn', 0), 0, 3, 'rdn'),
        }
    if len({s['voice'] for s in clean_speakers.values()}) != 2:
        raise PodcastError('Dual-voice mode requires two different voice codes')
    settings = doc.get('settings', {})
    if not isinstance(settings, dict):
        raise PodcastError('settings must be an object')
    pause = bounded_int(settings.get('pause_ms', 250), 0, 10000, 'pause_ms')
    turns = doc.get('turns')
    if not isinstance(turns, list) or not turns:
        raise PodcastError('turns must be a non-empty array')
    clean_turns, seen, used = [], set(), set()
    for i, turn in enumerate(turns):
        if not isinstance(turn, dict):
            raise PodcastError(f'turn {i + 1}: object required')
        ident = turn.get('id', f'turn_{i + 1:04d}')
        if not isinstance(ident, str) or not ID_PATTERN.fullmatch(ident) or ident in seen:
            raise PodcastError(f'turn {i + 1}: id must be unique and path-safe')
        speaker = turn.get('speaker')
        if speaker not in clean_speakers:
            raise PodcastError(f'{ident}: unknown speaker')
        text = plain_text(turn.get('text'), ident)
        gap = bounded_int(turn.get('pause_after_ms', pause if i < len(turns) - 1 else 0), 0, 10000, 'pause_after_ms')
        clean = {'id': ident, 'speaker': speaker, 'text': text, 'pause_after_ms': gap}
        if 'audio_path' in turn:
            if not isinstance(turn['audio_path'], str) or not turn['audio_path']:
                raise PodcastError(f'{ident}: audio_path must be a non-empty local path')
            clean['audio_path'] = str((script_path.parent / turn['audio_path']).resolve())
        clean_turns.append(clean)
        seen.add(ident)
        used.add(speaker)
    if used != set(clean_speakers):
        raise PodcastError('Both speakers must have at least one turn')
    return {'schema_version': 1, 'title': doc['title'], 'speakers': clean_speakers,
            'settings': {'pause_ms': pause}, 'turns': clean_turns}


def split_text(text, max_bytes=MAX_REQUEST_BYTES):
    """Preserve every character and prefer a sentence boundary within a byte budget."""
    if max_bytes < 4:
        raise PodcastError('max_bytes must be at least 4')
    parts = []
    remaining = text
    while remaining:
        total, end, boundary = 0, 0, 0
        for index, char in enumerate(remaining):
            length = len(char.encode('utf-8'))
            if total + length > max_bytes:
                break
            total += length
            end = index + 1
            if char in '。！？!?；;\n':
                boundary = end
        if end == len(remaining):
            parts.append(remaining)
            break
        cut = boundary if boundary >= end // 2 else end
        parts.append(remaining[:cut])
        remaining = remaining[cut:]
    return parts


def make_plan(doc):
    requests = []
    for turn in doc['turns']:
        speaker = doc['speakers'][turn['speaker']]
        pieces = split_text(turn['text'])
        for index, text in enumerate(pieces):
            acoustic = {key: value for key, value in speaker.items() if key != 'name'}
            identity = {'protocol_version': 1, 'endpoint': ENDPOINT, 'text': text, 'speaker': acoustic,
                        'sample_rate': RATE, 'encoding': 'raw'}
            digest = hashlib.sha256(json.dumps(identity, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
            requests.append({'id': f"{turn['id']}_{index + 1:03d}", 'turn_id': turn['id'],
                             'speaker': turn['speaker'], 'speaker_name': speaker['name'],
                             'voice': speaker['voice'], 'settings': acoustic, 'text': text,
                             'characters': len(text), 'utf8_bytes': len(text.encode('utf-8')),
                             'pause_after_ms': turn['pause_after_ms'] if index == len(pieces) - 1 else 0,
                             'cache_key': digest})
    plan_hash = hashlib.sha256(json.dumps(requests, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    return {'schema_version': 1, 'title': doc['title'], 'plan_hash': plan_hash,
            'request_count': len(requests), 'characters': sum(x['characters'] for x in requests),
            'price_estimate': None, 'billing_note': 'Character count only; not a price quote or actual charge.',
            'requests': requests}


def request_body(app_id, request):
    opts = request['settings']
    return {'header': {'app_id': app_id, 'status': 2},
            'parameter': {'oral': {}, 'tts': {
                'vcn': opts['voice'], 'speed': opts['speed'], 'volume': opts['volume'],
                'pitch': opts['pitch'], 'reg': opts['reg'], 'rdn': opts['rdn'], 'bgs': 0, 'rhy': 0,
                'audio': {'encoding': 'raw', 'sample_rate': RATE, 'channels': 1, 'bit_depth': 16, 'frame_size': 0}}},
            'payload': {'text': {'encoding': 'utf8', 'compress': 'raw', 'format': 'plain',
                                 'status': 2, 'seq': 0,
                                 'text': base64.b64encode(request['text'].encode('utf-8')).decode('ascii')}}}


def receive_audio(ws, timeout=120, sensitive_values=()):
    deadline = time.monotonic() + timeout
    chunks, total, sid = {}, 0, None
    while time.monotonic() < deadline:
        ws.settimeout(max(0.1, min(30, deadline - time.monotonic())))
        raw_message = ws.recv()
        if not raw_message:
            raise PodcastError('TTS connection ended before a terminal response; billing outcome unknown')
        try:
            msg = json.loads(raw_message)
        except (TypeError, ValueError):
            raise PodcastError('TTS returned a non-JSON frame') from None
        header = msg.get('header', {})
        if not isinstance(header, dict) or 'code' not in header:
            raise PodcastError('TTS response lacks a status code')
        if type(header['code']) is not int:
            raise PodcastError('TTS response has an invalid status code')
        if header['code'] != 0:
            raise ProviderError(header['code'], header.get('message', ''), sensitive_values)
        sid = header.get('sid', sid)
        audio = msg.get('payload', {}).get('audio', {})
        if audio:
            if audio.get('encoding', 'raw') != 'raw' or audio.get('sample_rate', RATE) != RATE or audio.get('channels', 1) != 1 or audio.get('bit_depth', 16) != 16:
                raise PodcastError('TTS returned an unexpected audio format')
            if audio.get('audio'):
                seq = audio.get('seq')
                if type(seq) is not int or seq < 0:
                    raise PodcastError('TTS audio frame lacks a valid sequence number')
                try:
                    chunk = base64.b64decode(audio['audio'], validate=True)
                except (ValueError, TypeError):
                    raise PodcastError('TTS returned invalid base64 audio') from None
                if seq in chunks and chunks[seq] != chunk:
                    raise PodcastError('TTS returned conflicting duplicate audio frames')
                if seq not in chunks:
                    total += len(chunk)
                    chunks[seq] = chunk
                if total > MAX_AUDIO_BYTES:
                    raise PodcastError('TTS audio exceeds the per-request local safety limit')
        if header.get('status') == 2 or audio.get('status') == 2:
            sequence = sorted(chunks)
            if sequence and sequence != list(range(sequence[0], sequence[-1] + 1)):
                raise PodcastError('TTS audio sequence is incomplete')
            pcm = b''.join(chunks[key] for key in sequence)
            if not pcm or len(pcm) % SAMPLE_WIDTH:
                raise PodcastError('TTS returned empty or invalid PCM audio')
            return pcm, sid
    raise PodcastError('TTS response deadline exceeded; billing outcome unknown')


def write_wave(path, pcm):
    temp = path.with_name(path.name + '.tmp')
    with wave.open(str(temp), 'wb') as output:
        output.setnchannels(1)
        output.setsampwidth(SAMPLE_WIDTH)
        output.setframerate(RATE)
        output.writeframes(pcm)
    temp.replace(path)


def wave_duration(path):
    with wave.open(str(path), 'rb') as audio:
        if (audio.getnchannels(), audio.getsampwidth(), audio.getframerate()) != (1, SAMPLE_WIDTH, RATE):
            raise PodcastError('Expected 24 kHz mono 16-bit WAV')
        if audio.getnframes() <= 0:
            raise PodcastError('Empty audio file')
        return audio.getnframes() / RATE


def run_media(args):
    result = subprocess.run(args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    if result.returncode:
        # Commands only receive local media paths; never credentials.
        message = result.stderr.decode('utf-8', errors='replace')[-2000:]
        raise PodcastError(f'Media command failed: {message}')


def srt_time(seconds):
    total = round(seconds * 1000)
    hours, rem = divmod(total, 3600000)
    minutes, rem = divmod(rem, 60000)
    secs, ms = divmod(rem, 1000)
    return f'{hours:02}:{minutes:02}:{secs:02},{ms:03}'


def assemble_items(doc, items, output_dir, normalize=True, mode='offline_assembly'):
    require_tools()
    output_dir.mkdir(parents=True, exist_ok=True)
    cues, cursor = [], 0
    with tempfile.TemporaryDirectory(prefix='podcast-', dir=output_dir) as tmp:
        temp_dir = Path(tmp)
        raw_mix = temp_dir / 'mix.wav'
        with wave.open(str(raw_mix), 'wb') as result:
            result.setnchannels(1)
            result.setsampwidth(SAMPLE_WIDTH)
            result.setframerate(RATE)
            for index, item in enumerate(items):
                source = Path(item['audio_path']).resolve()
                if not source.is_file():
                    raise PodcastError(f"Missing audio for {item['id']}: {source}")
                normalized = temp_dir / f'{index:05d}.wav'
                run_media(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-y', '-i', str(source), '-vn',
                           '-ac', '1', '-ar', str(RATE), '-c:a', 'pcm_s16le', str(normalized)])
                with wave.open(str(normalized), 'rb') as audio:
                    frames = audio.getnframes()
                    if frames == 0:
                        raise PodcastError(f"Empty audio for {item['id']}")
                    start = cursor / RATE
                    end = (cursor + frames) / RATE
                    while True:
                        chunk = audio.readframes(65536)
                        if not chunk:
                            break
                        result.writeframesraw(chunk)
                cues.append({'id': item['id'], 'speaker': item['speaker'], 'speaker_name': item['speaker_name'],
                             'start': start, 'end': end, 'text': item['text'], 'source': str(source)})
                cursor += frames
                gap = round(item['pause_after_ms'] * RATE / 1000)
                if gap:
                    result.writeframesraw(b'\x00' * gap * SAMPLE_WIDTH)
                    cursor += gap
        final_temp = temp_dir / 'podcast.wav'
        filters = ['-af', 'loudnorm=I=-16:TP=-1.5:LRA=11'] if normalize else []
        run_media(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-y', '-i', str(raw_mix), *filters,
                   '-ac', '1', '-ar', str(RATE), '-c:a', 'pcm_s16le', str(final_temp)])
        if abs(wave_duration(final_temp) - cursor / RATE) > .1:
            raise PodcastError('Final audio duration changed unexpectedly; subtitle timing requires review')
        mp3_temp = temp_dir / 'podcast.mp3'
        run_media(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-y', '-i', str(final_temp),
                   '-c:a', 'libmp3lame', '-b:a', '128k', str(mp3_temp)])
        final_temp.replace(output_dir / 'podcast.wav')
        mp3_temp.replace(output_dir / 'podcast.mp3')
    lines = []
    for index, cue in enumerate(cues, 1):
        lines.extend([str(index), f"{srt_time(cue['start'])} --> {srt_time(cue['end'])}",
                      f"{cue['speaker_name']}：{cue['text'].replace(chr(10), ' ')}", ''])
    (output_dir / 'podcast.srt').write_text('\n'.join(lines) + '\n', encoding='utf-8')
    script_notice = ('AI 合成播客脚本。' if mode == 'xfyun_tts' else '本地音频拼接脚本。来源以 provenance.json 为准。')
    script_notice += '字幕按音频段计时，未作字词级强制对齐。'
    manuscript = [f"# {doc['title']}", '', f'> {script_notice}', '']
    for turn in doc['turns']:
        manuscript.extend([f"**{doc['speakers'][turn['speaker']]['name']}**", '', turn['text'], ''])
    (output_dir / 'script.md').write_text('\n'.join(manuscript), encoding='utf-8')
    role_dir = output_dir / 'roles'
    role_dir.mkdir(exist_ok=True)
    for key, speaker in doc['speakers'].items():
        text = '\n\n'.join(turn['text'] for turn in doc['turns'] if turn['speaker'] == key)
        (role_dir / f'{key}.txt').write_text(text + '\n', encoding='utf-8')
    atomic_json(output_dir / 'script.json', doc)
    atomic_json(output_dir / 'timeline.json', {'schema_version': 1, 'mode': mode,
                'duration_seconds': cursor / RATE, 'subtitle_accuracy': 'segment', 'segments': cues})
    atomic_json(output_dir / 'provenance.json', {'mode': mode, 'audio_origin': 'xfyun_generated_or_cached' if mode == 'xfyun_tts' else 'user_supplied_audio',
                'notice': 'AI-generated audio when rendered with XFYun. Offline inputs retain their original provenance.',
                'loudness_normalized': normalize, 'sample_rate': RATE})
    return {'output_dir': str(output_dir), 'duration_seconds': cursor / RATE,
            'files': ['podcast.mp3', 'podcast.wav', 'podcast.srt', 'script.md', 'script.json', 'timeline.json', 'provenance.json']}


def render(doc, output_dir, resume=False, retry_uncertain=False, timeout=120, normalize=True):
    with render_lock(Path(output_dir).resolve()):
        return _render_locked(doc, output_dir, resume, retry_uncertain, timeout, normalize)


def _render_locked(doc, output_dir, resume=False, retry_uncertain=False, timeout=120, normalize=True):
    require_tools()
    app_id, password = os.environ.get('XFYUN_TTS_APP_ID'), os.environ.get('XFYUN_TTS_API_PASSWORD')
    if not app_id or not password:
        raise PodcastError('Set XFYUN_TTS_APP_ID and XFYUN_TTS_API_PASSWORD locally; never put credentials in the script JSON')
    try:
        import websocket
    except ImportError:
        raise PodcastError('Install websocket-client in the Python environment') from None
    if not math.isfinite(timeout) or timeout <= 0:
        raise PodcastError('timeout must be a positive finite number')
    plan = make_plan(doc)
    output_dir = output_dir.resolve()
    state_path = output_dir / 'state.json'
    if retry_uncertain and not resume:
        raise PodcastError('--retry-uncertain requires --resume')
    if state_path.exists() and not resume:
        raise PodcastError('Existing render state; use --resume to reuse completed segments')
    if not state_path.exists() and any((output_dir / p).exists() for p in ('podcast.mp3', 'podcast.wav')):
        raise PodcastError('Output contains unrelated assembled media; choose a separate render directory')
    output_dir.mkdir(parents=True, exist_ok=True)
    cache = output_dir / 'segments'
    cache.mkdir(exist_ok=True)
    if state_path.exists():
        try:
            state = json.loads(state_path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            raise PodcastError('Cannot read render state') from None
        if state.get('schema_version') != 1 or not isinstance(state.get('requests'), dict):
            raise PodcastError('Invalid render state')
    else:
        state = {'schema_version': 1, 'requests': {}}
    # Inspect all uncertain attempts before spending on any new turn.
    for request in plan['requests']:
        record = state['requests'].get(request['cache_key'], {})
        if record.get('status') in ('inflight', 'uncertain') and not retry_uncertain:
            raise PodcastError('Previous request has unknown outcome. Review the service console; retry only with --resume --retry-uncertain (may bill again).')
    atomic_json(output_dir / 'requests.json', plan)
    state['plan_hash'] = plan['plan_hash']
    state['complete'] = False
    atomic_json(state_path, state)
    items = []
    for request in plan['requests']:
        key = request['cache_key']
        audio_path = cache / f'{key}.wav'
        record = state['requests'].get(key, {})
        if record.get('status') == 'succeeded':
            if not audio_path.is_file():
                raise PodcastError('Completed segment cache is missing; restore it or choose a new output directory to regenerate intentionally')
            wave_duration(audio_path)
            if hashlib.sha256(audio_path.read_bytes()).hexdigest() != record.get('audio_sha256'):
                raise PodcastError('Completed segment cache failed integrity verification; restore it before resuming')
        else:
            ws = None
            sent = False
            attempts = int(record.get('attempts', 0)) + 1
            try:
                ws = websocket.create_connection(ENDPOINT, header={'x-api-key': password}, timeout=min(timeout, 30))
                record = {'status': 'inflight', 'attempts': attempts, 'characters': request['characters']}
                state['requests'][key] = record
                atomic_json(state_path, state)
                sent = True  # Set BEFORE send: transport errors can occur after acceptance.
                ws.send(json.dumps(request_body(app_id, request), ensure_ascii=False))
                pcm, sid = receive_audio(ws, timeout, sensitive_values=(app_id, password))
                write_wave(audio_path, pcm)
                record.update({'status': 'succeeded', 'sid': sid, 'duration_seconds': wave_duration(audio_path),
                               'audio_sha256': hashlib.sha256(audio_path.read_bytes()).hexdigest()})
                atomic_json(state_path, state)
            except Exception as exc:
                state['requests'][key] = {'status': 'uncertain' if sent else 'not_submitted',
                    'attempts': attempts, 'characters': request['characters'], 'error_type': type(exc).__name__}
                failure = state['requests'][key]
                if isinstance(exc, PodcastError):
                    failure['error'] = str(exc)
                if isinstance(exc, ProviderError):
                    failure['provider_code'] = exc.provider_code
                    failure['provider_message'] = exc.provider_message
                http_status = getattr(exc, 'status_code', None)
                if type(http_status) is int and 100 <= http_status <= 599:
                    failure['http_status'] = http_status
                atomic_json(state_path, state)
                if isinstance(exc, PodcastError):
                    raise
                if 'http_status' in failure:
                    raise PodcastError(f'TTS WebSocket handshake failed (HTTP {http_status}); check service credentials and permissions. No automatic retry was made.') from None
                raise PodcastError(f'TTS request stopped ({type(exc).__name__}); inspect state.json before retrying. No automatic retry was made.') from None
            finally:
                if ws is not None:
                    try:
                        ws.close()
                    except Exception:
                        pass
        items.append({**request, 'audio_path': str(audio_path)})
    result = assemble_items(doc, items, output_dir, normalize, mode='xfyun_tts')
    state['complete'] = True
    state['requested_characters'] = plan['characters']
    atomic_json(state_path, state)
    return {**result, 'request_count': plan['request_count'], 'characters': plan['characters'],
            'billing_note': 'Account console is authoritative for actual usage and charges.'}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('doctor', help='Check dependencies and credential presence without showing values')
    for name in ('validate', 'plan', 'render', 'assemble'):
        cmd = sub.add_parser(name)
        cmd.add_argument('script', type=Path, help='UTF-8 podcast JSON')
        if name == 'plan':
            cmd.add_argument('--out', type=Path, help='Optional request manifest JSON')
        if name in ('render', 'assemble'):
            cmd.add_argument('--out', required=True, type=Path, help='Output directory')
            cmd.add_argument('--no-normalize', action='store_true', help='Disable final loudness normalization')
        if name == 'render':
            cmd.add_argument('--resume', action='store_true')
            cmd.add_argument('--retry-uncertain', action='store_true', help='Explicitly retry a request with unknown billing outcome')
            cmd.add_argument('--timeout', type=float, default=120)
        if name == 'assemble':
            cmd.add_argument('--overwrite', action='store_true')
    args = parser.parse_args(argv)
    try:
        if args.command == 'doctor':
            result = {'python': sys.version.split()[0], 'ffmpeg': bool(shutil.which('ffmpeg')),
                      'ffprobe': bool(shutil.which('ffprobe')),
                      'websocket_client': importlib.util.find_spec('websocket') is not None,
                      'credentials': {key: bool(os.environ.get(key)) for key in ('XFYUN_TTS_APP_ID', 'XFYUN_TTS_API_PASSWORD')}}
        else:
            doc = load_script(args.script)
            if args.command == 'validate':
                result = {'valid': True, 'speakers': list(doc['speakers']), 'turn_count': len(doc['turns'])}
            elif args.command == 'plan':
                result = make_plan(doc)
                if args.out:
                    atomic_json(args.out, result)
            elif args.command == 'render':
                result = render(doc, args.out, args.resume, args.retry_uncertain, args.timeout, not args.no_normalize)
            else:
                if (args.out / 'state.json').exists():
                    raise PodcastError('This is a paid render directory; choose a separate offline assembly directory')
                if not args.overwrite and any((args.out / p).exists() for p in ('podcast.mp3', 'podcast.wav')):
                    raise PodcastError('Output already exists; use --overwrite intentionally')
                items = []
                for turn in doc['turns']:
                    if 'audio_path' not in turn:
                        raise PodcastError(f"{turn['id']}: offline assemble requires audio_path")
                    items.append({**turn, 'speaker_name': doc['speakers'][turn['speaker']]['name']})
                result = assemble_items(doc, items, args.out.resolve(), not args.no_normalize)
                atomic_json(args.out / 'requests.json', {'mode': 'offline_assembly', 'requests_sent': 0,
                    'items': [{'id': t['id'], 'source': t['audio_path']} for t in items]})
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (PodcastError, OSError, ValueError, wave.Error) as exc:
        print(json.dumps({'error': str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
