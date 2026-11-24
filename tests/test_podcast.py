"""Protocol tests are mocked. Tone fixtures validate assembly, not voice quality."""
import base64
import copy
import importlib.util
import json
import math
import os
from pathlib import Path
import struct
import sys
import tempfile
import types
import unittest
from unittest import mock
import wave

MODULE_PATH = Path(__file__).resolve().parents[1] / 'skills/xfyun-dual-voice-podcast/scripts/podcast.py'
spec = importlib.util.spec_from_file_location('podcast_under_test', MODULE_PATH)
podcast = importlib.util.module_from_spec(spec)
spec.loader.exec_module(podcast)


def fixture_doc():
    return {'schema_version': 1, 'title': '离线验证', 'speakers': {
        'a': {'name': '角色甲', 'voice': 'x6_bokenansheng_pro'},
        'b': {'name': '角色乙', 'voice': 'x6_lingyuyan_pro'}},
        'settings': {'pause_ms': 250},
        'turns': [{'id': 'first', 'speaker': 'a', 'text': '第一段。'},
                  {'id': 'second', 'speaker': 'b', 'text': '第二段。'}]}


def tone_bytes(seconds=.2, frequency=440):
    return b''.join(struct.pack('<h', round(5000 * math.sin(2 * math.pi * frequency * i / podcast.RATE)))
                    for i in range(round(seconds * podcast.RATE)))


class FakeSocket:
    def __init__(self, messages):
        self.messages = iter(messages)
        self.sent = []
        self.closed = False

    def send(self, value):
        self.sent.append(json.loads(value))

    def recv(self):
        return next(self.messages, '')

    def settimeout(self, timeout):
        self.timeout = timeout

    def close(self):
        self.closed = True


def frame(seq, pcm, final=False):
    return json.dumps({'header': {'code': 0, 'sid': 'mock-only', 'status': 2 if final else 1},
                       'payload': {'audio': {'encoding': 'raw', 'sample_rate': 24000,
                       'channels': 1, 'bit_depth': 16, 'seq': seq, 'status': 2 if final else 1,
                       'audio': base64.b64encode(pcm).decode()}}})


class PodcastTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.script = self.root / 'script.json'

    def tearDown(self):
        self.tmp.cleanup()

    def doc(self, value=None):
        self.script.write_text(json.dumps(value or fixture_doc(), ensure_ascii=False), encoding='utf8')
        return podcast.load_script(self.script)

    def test_script_validation_and_voice_mapping(self):
        doc = self.doc()
        plan = podcast.make_plan(doc)
        self.assertEqual(plan['request_count'], 2)
        self.assertEqual(plan['characters'], 8)
        self.assertEqual(plan['requests'][0]['voice'], 'x6_bokenansheng_pro')
        self.assertEqual(plan['requests'][1]['voice'], 'x6_lingyuyan_pro')
        self.assertEqual(plan['requests'][-1]['pause_after_ms'], 0)
        self.assertIsNone(plan['price_estimate'])

    def test_reject_unknown_role_and_same_voice(self):
        invalid = fixture_doc()
        invalid['turns'][0]['speaker'] = 'ghost'
        with self.assertRaises(podcast.PodcastError):
            self.doc(invalid)
        invalid = fixture_doc()
        invalid['speakers']['b']['voice'] = invalid['speakers']['a']['voice']
        with self.assertRaises(podcast.PodcastError):
            self.doc(invalid)

    def test_reject_duplicate_id_and_unspoken_markup(self):
        for value in ('hello\tworld', '**讲稿**', '声音😀', '<b>hello</b>'):
            invalid = fixture_doc()
            invalid['turns'][0]['text'] = value
            with self.assertRaises(podcast.PodcastError):
                self.doc(invalid)
        invalid = fixture_doc()
        invalid['turns'][1]['id'] = 'first'
        with self.assertRaises(podcast.PodcastError):
            self.doc(invalid)

    def test_utf8_splitting_retains_characters(self):
        text = ('这是中文。Mix English 123，继续讲解！' * 1000)
        parts = podcast.split_text(text, 70)
        self.assertEqual(''.join(parts), text)
        self.assertTrue(all(len(p.encode('utf8')) <= 70 for p in parts))
        self.assertTrue(all(parts))

    def test_request_protocol(self):
        request = podcast.make_plan(self.doc())['requests'][0]
        data = podcast.request_body('test-app', request)
        self.assertEqual(data['header'], {'app_id': 'test-app', 'status': 2})
        self.assertEqual(data['parameter']['oral'], {})
        self.assertEqual(data['parameter']['tts']['audio']['encoding'], 'raw')
        self.assertEqual(base64.b64decode(data['payload']['text']['text']).decode(), request['text'])
        self.assertEqual(data['payload']['text']['status'], 2)

    def test_response_sorting_duplicates_and_terminal(self):
        a, b = b'\x01\x00', b'\x02\x00'
        socket = FakeSocket([frame(1, b), frame(0, a), frame(0, a, final=True)])
        audio, sid = podcast.receive_audio(socket)
        self.assertEqual(audio, a + b)
        self.assertEqual(sid, 'mock-only')
        with self.assertRaises(podcast.PodcastError):
            podcast.receive_audio(FakeSocket([frame(0, a)]))
        with self.assertRaises(podcast.PodcastError):
            podcast.receive_audio(FakeSocket([frame(0, a), frame(0, b, True)]))

    def test_missing_audio_sequence_rejected(self):
        with self.assertRaises(podcast.PodcastError):
            podcast.receive_audio(FakeSocket([frame(0, b'\x01\x00'), frame(2, b'\x02\x00', True)]))

    def test_provider_error_is_redacted(self):
        error_frame = json.dumps({'header': {'code': 100, 'message': 'pretend-secret-should-not-appear'}})
        with self.assertRaises(podcast.PodcastError) as error:
            podcast.receive_audio(FakeSocket([error_frame]))
        self.assertNotIn('pretend-secret', str(error.exception))
        self.assertIn('100', str(error.exception))

    def test_offline_assembly_durations_and_pause(self):
        doc = self.doc()
        items = []
        for index, turn in enumerate(doc['turns']):
            path = self.root / f'{index}.wav'
            podcast.write_wave(path, tone_bytes(.2 + .1 * index, 440 + 200 * index))
            items.append({**turn, 'audio_path': str(path), 'speaker_name': doc['speakers'][turn['speaker']]['name']})
        out = self.root / 'assembled'
        result = podcast.assemble_items(doc, items, out)
        self.assertAlmostEqual(result['duration_seconds'], .75, places=4)
        self.assertAlmostEqual(podcast.wave_duration(out / 'podcast.wav'), .75, places=2)
        timeline = json.loads((out / 'timeline.json').read_text())
        self.assertAlmostEqual(timeline['segments'][1]['start'], .45)
        self.assertTrue((out / 'podcast.mp3').stat().st_size > 100)
        self.assertIn('00:00:00,450 --> 00:00:00,750', (out / 'podcast.srt').read_text())
        self.assertEqual(json.loads((out / 'provenance.json').read_text())['mode'], 'offline_assembly')
        self.assertEqual((out / 'roles/a.txt').read_text(), '第一段。\n')
        self.assertIn('本地音频拼接脚本', (out / 'script.md').read_text())
        self.assertNotIn('AI 合成播客脚本', (out / 'script.md').read_text())

    def test_render_resume_and_modified_turn_reuse(self):
        doc = self.doc()
        out = self.root / 'rendered'
        created = []
        def connect(url, header, timeout):
            self.assertEqual(url, podcast.ENDPOINT)
            self.assertEqual(header, {'x-api-key': 'fake-test-secret'})
            ws = FakeSocket([frame(0, tone_bytes(), True)])
            created.append(ws)
            return ws
        websocket_stub = types.SimpleNamespace(create_connection=connect)
        with mock.patch.dict(os.environ, {'XFYUN_TTS_APP_ID': 'fake-app', 'XFYUN_TTS_API_PASSWORD': 'fake-test-secret'}), mock.patch.dict(sys.modules, {'websocket': websocket_stub}):
            podcast.render(doc, out)
            self.assertEqual(len(created), 2)
            podcast.render(doc, out, resume=True)
            self.assertEqual(len(created), 2)
            doc['turns'][0]['text'] = '第一段已经修改。'
            podcast.render(doc, out, resume=True)
            self.assertEqual(len(created), 3)
        state_text = (out / 'state.json').read_text()
        self.assertNotIn('fake-test-secret', state_text)
        self.assertNotIn('fake-app', state_text)
        self.assertTrue(json.loads(state_text)['complete'])
        self.assertFalse((out / '.render.lock').exists())
        self.assertIn('AI 合成播客脚本', (out / 'script.md').read_text())

    def test_unknown_outcome_blocks_automatic_retry(self):
        doc = self.doc()
        out = self.root / 'uncertain'
        sockets = []
        def connect(*args, **kwargs):
            ws = FakeSocket([])
            sockets.append(ws)
            return ws
        stub = types.SimpleNamespace(create_connection=connect)
        with mock.patch.dict(os.environ, {'XFYUN_TTS_APP_ID': 'fake-app', 'XFYUN_TTS_API_PASSWORD': 'fake-secret'}), mock.patch.dict(sys.modules, {'websocket': stub}):
            with self.assertRaises(podcast.PodcastError):
                podcast.render(doc, out)
            self.assertEqual(len(sockets), 1)
            state = json.loads((out / 'state.json').read_text())
            self.assertEqual(next(iter(state['requests'].values()))['status'], 'uncertain')
            with self.assertRaises(podcast.PodcastError):
                podcast.render(doc, out, resume=True)
            self.assertEqual(len(sockets), 1)
            self.assertFalse((out / 'podcast.mp3').exists())
            self.assertFalse((out / '.render.lock').exists())

    def test_exclusive_render_lock_blocks_concurrent_and_stale_runs(self):
        doc = self.doc()
        out = self.root / 'locked'
        with podcast.render_lock(out):
            owner = json.loads((out / '.render.lock').read_text())
            self.assertEqual(owner['pid'], os.getpid())
            with mock.patch.object(podcast, '_render_locked') as render_impl:
                with self.assertRaisesRegex(podcast.PodcastError, 'locked'):
                    podcast.render(doc, out)
                render_impl.assert_not_called()
            self.assertTrue((out / '.render.lock').exists())
        self.assertFalse((out / '.render.lock').exists())
        (out / '.render.lock').write_text('stale-lock-sentinel')
        with self.assertRaises(podcast.PodcastError):
            podcast.render(doc, out)
        self.assertEqual((out / '.render.lock').read_text(), 'stale-lock-sentinel')

    def test_provider_diagnostics_persist_without_credentials(self):
        doc = self.doc()
        out = self.root / 'provider-error'
        message = 'Voice is not authorized; x-api-key=fake-password app=fake-app'
        response = json.dumps({'header': {'code': 11200, 'message': message}})
        stub = types.SimpleNamespace(create_connection=lambda *a, **kw: FakeSocket([response]))
        with mock.patch.dict(os.environ, {'XFYUN_TTS_APP_ID': 'fake-app', 'XFYUN_TTS_API_PASSWORD': 'fake-password'}), mock.patch.dict(sys.modules, {'websocket': stub}):
            with self.assertRaisesRegex(podcast.ProviderError, '11200'):
                podcast.render(doc, out)
        saved = (out / 'state.json').read_text()
        record = next(iter(json.loads(saved)['requests'].values()))
        self.assertEqual(record['provider_code'], 11200)
        self.assertIn('Voice is not authorized', record['provider_message'])
        self.assertNotIn('fake-password', saved)
        self.assertNotIn('fake-app', saved)
        self.assertFalse((out / '.render.lock').exists())

    def test_handshake_reports_only_http_status(self):
        doc = self.doc()
        out = self.root / 'handshake-error'
        class HandshakeError(Exception):
            status_code = 401
        def connect(*args, **kwargs):
            raise HandshakeError('Authorization: fake-secret-response-headers')
        stub = types.SimpleNamespace(create_connection=connect)
        with mock.patch.dict(os.environ, {'XFYUN_TTS_APP_ID': 'fake-app', 'XFYUN_TTS_API_PASSWORD': 'fake-password'}), mock.patch.dict(sys.modules, {'websocket': stub}):
            with self.assertRaisesRegex(podcast.PodcastError, 'HTTP 401') as error:
                podcast.render(doc, out)
        saved = (out / 'state.json').read_text()
        record = next(iter(json.loads(saved)['requests'].values()))
        self.assertEqual(record['status'], 'not_submitted')
        self.assertEqual(record['http_status'], 401)
        self.assertNotIn('fake-secret-response-headers', saved + str(error.exception))
        self.assertFalse((out / '.render.lock').exists())

    def test_cache_changes_for_voice_settings_not_pause(self):
        doc = self.doc()
        first = podcast.make_plan(doc)['requests'][0]['cache_key']
        doc['turns'][0]['pause_after_ms'] = 1000
        self.assertEqual(first, podcast.make_plan(doc)['requests'][0]['cache_key'])
        doc['speakers']['a']['speed'] = 60
        self.assertNotEqual(first, podcast.make_plan(doc)['requests'][0]['cache_key'])


if __name__ == '__main__':
    unittest.main()
