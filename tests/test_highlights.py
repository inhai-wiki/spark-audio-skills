"""Protocol and offline media tests. Synthetic tones are NOT ASR accuracy tests."""
import base64
import importlib.util
import json
from pathlib import Path
import queue
import shutil
import subprocess
import sys
from urllib.parse import parse_qs, urlsplit

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "skills/xfyun-highlight-clips/scripts/highlight_clips.py"
spec = importlib.util.spec_from_file_location("highlight_clips", SCRIPT)
h = importlib.util.module_from_spec(spec)
spec.loader.exec_module(h)


def piece(sn, text, **kwargs):
    return {"sn": sn, "ws": [{"cw": [{"w": text}, {"w": "alternative-must-not-be-appended"}]}], **kwargs}


def envelope(payload=None, status=1, code=0):
    msg = {"header": {"code": code, "status": status, "sid": "synthetic-test-sid"}}
    if payload is not None:
        msg["payload"] = {"result": {"compress": "raw", "text": base64.b64encode(json.dumps(payload).encode()).decode()}}
    return msg


def test_dynamic_replacement_removes_inclusive_range_and_handles_duplicate_sn():
    acc = h.ResultAccumulator()
    acc.add_message(envelope(piece(0, "开头")))
    acc.add_message(envelope(piece(1, "旧一")))
    acc.add_message(envelope(piece(2, "旧二")))
    acc.add_message(envelope(piece(3, "修正", pgs="rpl", rg=[1, 2])))
    acc.add_message(envelope(piece(4, "结")))
    acc.add_message(envelope(piece(4, "结尾"), status=2))
    assert acc.text() == "开头修正结尾"
    assert acc.server_done
    assert sorted(acc.pieces) == [0, 3, 4]


@pytest.mark.parametrize("message", [envelope(status=2, code=10001), envelope({"ret": 101, "sn": 0}), {"header": {"code": 0}, "payload": {"result": {"text": "not-base64"}}}])
def test_service_errors_and_invalid_payload_are_not_reported_as_success(message):
    with pytest.raises(h.SkillError):
        h.ResultAccumulator().add_message(message)


def test_auth_url_uses_gmt_date_and_expected_request_target():
    url = h.build_auth_url("test-key", "test-secret", "Wed, 25 Nov 2026 01:02:03 GMT")
    parsed = urlsplit(url)
    query = parse_qs(parsed.query)
    assert parsed.scheme == "wss"
    assert parsed.netloc == "iat.cn-huabei-1.xf-yun.com"
    assert parsed.path == "/v1"
    assert query["date"] == ["Wed, 25 Nov 2026 01:02:03 GMT"]
    authorization = base64.b64decode(query["authorization"][0]).decode()
    assert authorization.startswith('api_key="test-key",algorithm="hmac-sha256",headers="host date request-line",signature="')
    assert "test-secret" not in url


def test_frame_protocol_and_context_limit():
    first = h.build_frame("app", b"\x00\x01", 0, 0, {"bias": {"hotword": [{"text": "测试"}]}})
    assert first["parameter"]["iat"]["domain"] == "spark_asr_v2"
    assert first["parameter"]["iat"]["dwa"] == "wpgs"
    assert first["parameter"]["iat"]["svad"] == 0
    assert isinstance(first["parameter"]["iat"]["context"], str)
    assert base64.b64decode(first["payload"]["audio"]["audio"]) == b"\x00\x01"
    assert "parameter" not in h.build_frame("app", b"", 2, 2)
    with pytest.raises(h.SkillError):
        h.build_frame("app", b"x", 0, 0, {"text": "中" * 1000})


def test_stream_transport_mock_verifies_frames_and_terminal_result(tmp_path, monkeypatch):
    import websocket
    incoming = queue.Queue()
    sent = []
    class FakeWebSocket:
        def recv(self):
            return incoming.get(timeout=2)
        def send(self, raw):
            frame = json.loads(raw)
            sent.append(frame)
            if frame["header"]["status"] == 2:
                incoming.put(json.dumps(envelope(piece(0, "协议模拟结果"), status=2)))
        def close(self):
            pass
    monkeypatch.setattr(websocket, "create_connection", lambda *a, **k: FakeWebSocket())
    monkeypatch.setattr(h.time, "sleep", lambda _: None)
    pcm = tmp_path / "tiny.pcm"
    pcm.write_bytes(b"\x00\x00" * 800)
    result = h.stream_chunk(pcm, ("app", "key", "secret"), tmp_path / "responses.jsonl", timeout=2)
    assert result["text"] == "协议模拟结果"
    assert [frame["header"]["status"] for frame in sent] == [0, 1, 2]
    assert [frame["payload"]["audio"]["seq"] for frame in sent] == [0, 1, 2]
    joined = b"".join(base64.b64decode(frame["payload"]["audio"]["audio"]) for frame in sent)
    assert joined == pcm.read_bytes()
    assert "secret" not in (tmp_path / "responses.jsonl").read_text()


@pytest.mark.parametrize("start,end", [(float("nan"), 1), (0, float("inf")), (-1, 2), (2, 2), (0, 11), (True, 3)])
def test_clip_bounds_are_validated(start, end):
    with pytest.raises(h.SkillError):
        h.validate_clips({"clips": [{"start": start, "end": end}]}, 10)


def test_clip_unknown_evidence_and_duplicate_ids_are_rejected():
    with pytest.raises(h.SkillError):
        h.validate_clips({"clips": [{"start": 0, "end": 1, "segment_ids": ["invented"]}]}, 10, {"s1"})
    with pytest.raises(h.SkillError):
        h.validate_clips({"clips": [{"id": "x", "start": 0, "end": 1}, {"id": "x", "start": 2, "end": 3}]}, 10)


def test_clip_summaries_are_optional_validated_and_not_invented():
    basic = {"id": "a", "start": 0, "end": 1}
    old = h.validate_clips({"clips": [basic]}, 10)[0]
    assert "summary" not in old
    complete = h.validate_clips({"clips": [{**basic, "summary": "  嘉宾解释使用条件。  "}]}, 10)[0]
    assert complete["summary"] == "嘉宾解释使用条件。"
    assert complete["summary_origin"] == "manifest"
    for invalid in (None, "", " \n", ["不能把列表直接当成摘要"], 123):
        with pytest.raises(h.SkillError):
            h.validate_clips({"clips": [{**basic, "summary": invalid}]}, 10)


@pytest.fixture
def media(tmp_path):
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        pytest.skip("ffmpeg and ffprobe required for offline integration")
    source = tmp_path / "synthetic-tone.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-y", "-f", "lavfi", "-i", "color=c=navy:s=320x180:r=25:d=6", "-f", "lavfi", "-i", "sine=frequency=440:duration=6", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(source)], check=True)
    return source


def test_offline_prepare_import_and_precise_reencoded_cuts(media, tmp_path):
    job = h.prepare_one(media, tmp_path / "output", chunk_seconds=2)
    manifest = h.read_json(job / "media.json")
    assert len(manifest["chunks"]) == 3
    assert all(c["start"] < c["end"] <= manifest["duration"] for c in manifest["chunks"])
    assert manifest["chunks"][0]["start"] == 0
    assert manifest["chunks"][1]["start"] == 2
    assert (job / "analysis.mp3").stat().st_size > 0
    assert h.prepare_one(media, tmp_path / "output", chunk_seconds=2) == job
    external = tmp_path / "timed.json"
    h.write_json(external, {"timing_precision": "manual-test-fixture", "segments": [{"id": "s1", "start": 0, "end": 3, "text": "手工测试文本，源音频是合成音。"}, {"id": "s2", "start": 3, "end": 6, "text": "本测试不评估语音识别效果。"}]})
    assert h.main(["import-transcript", "--job", str(job), "--input", str(external)]) == 0
    for extension in ("json", "txt", "md", "srt"):
        assert (job / f"transcript.{extension}").is_file()
    cuts = tmp_path / "cuts.json"
    h.write_json(cuts, {"clips": [{"id": "../safe-id", "title": "synthetic test", "start": 1.1, "end": 3.3, "segment_ids": ["s1", "s2"]}]})
    result = h.cut_clips(job, cuts)
    output = Path(result["clips"][0]["file"])
    assert output.parent == job / "clips"
    assert output.suffix == ".mp4"
    assert abs(h.probe_media(output)["duration"] - 2.2) < 0.15
    assert h.read_json(job / "clips/clips-video.json")["clips"][0]["file"] == str(output)
    assert not (job / "clips/clips.partial.json").exists()
    mp3_result = h.cut_clips(job, cuts, audio_only=True)
    assert Path(mp3_result["clips"][0]["file"]).suffix == ".mp3"
    assert h.read_json(job / "clips/clips-audio.json")["mode"] == "audio"
    assert h.read_json(job / "clips/clips-video.json")["clips"][0]["file"] == str(output)
    assert h.read_json(job / "clips/clips.json")["mode"] == "audio"
    assert not (job / "clips/clips.partial.json").exists()
    assert media.exists()


def test_identical_filenames_in_different_folders_get_distinct_jobs(media, tmp_path):
    other = tmp_path / "second" / media.name
    other.parent.mkdir()
    shutil.copyfile(media, other)
    job1 = h.prepare_one(media, tmp_path / "output", 20)
    job2 = h.prepare_one(other, tmp_path / "output", 20)
    assert job1 != job2


def test_clip_exports_editorial_summary_and_preserves_mode_reports(media, tmp_path, monkeypatch):
    job = h.prepare_one(media, tmp_path / "output", 20)
    external = tmp_path / "timed.json"
    h.write_json(external, {"segments": [{"id": "s1", "start": 0, "end": 6, "text": "测试转写原文，不是内容摘要。"}]})
    assert h.main(["import-transcript", "--job", str(job), "--input", str(external)]) == 0
    manifest = tmp_path / "summary-cuts.json"
    summary = "本段说明样例的使用条件，发布前仍需核对原音。"
    h.write_json(manifest, {"clips": [{"id": "scene", "title": "播客片段", "start": 1, "end": 3, "summary": summary, "segment_ids": ["s1"]}]})
    result = h.cut_clips(job, manifest)
    assert result["clips"][0]["summary"] == summary
    assert result["clips"][0]["transcript_excerpt"] != summary
    video_report = Path(result["highlights_markdown"])
    report = video_report.read_text()
    assert summary in report
    assert "00:00:01.000 至 00:00:03.000" in report
    assert f"[打开切片]({Path(result['clips'][0]['file']).name})" in report
    assert "引用原话和发布前请回听音频" in report
    audio = h.cut_clips(job, manifest, audio_only=True)
    assert Path(audio["highlights_markdown"]).name == "highlights-audio.md"
    assert video_report.read_text() == report
    assert (job / "clips/highlights.md").read_text() == Path(audio["highlights_markdown"]).read_text()
    def encoding_fails(*args, **kwargs):
        raise h.SkillError("simulated encoder failure")
    monkeypatch.setattr(h, "run_media", encoding_fails)
    with pytest.raises(h.SkillError):
        h.cut_clips(job, manifest)
    assert "本次裁切尚未全部完成" in (job / "clips/highlights.md").read_text()
    assert video_report.read_text() == report


def test_failed_chunk_is_not_automatically_billed_again(media, tmp_path, monkeypatch):
    job = h.prepare_one(media, tmp_path / "output", 2)
    for name in ("XFYUN_APP_ID", "XFYUN_API_KEY", "XFYUN_API_SECRET"):
        monkeypatch.setenv(name, "synthetic-test-value")
    calls = []
    def mock_stream(path, *args, **kwargs):
        calls.append(Path(path).name)
        if len(calls) == 2:
            raise h.SkillError("Simulated uncertain failure")
        return {"text": "模拟协议文字", "sid": "mock", "raw_pieces": []}
    monkeypatch.setattr(h, "stream_chunk", mock_stream)
    with pytest.raises(h.SkillError, match="uncertain failure"):
        h.transcribe_job(job)
    assert len(calls) == 2
    assert not (job / "transcript.json").exists()
    with pytest.raises(h.SkillError, match="previous incomplete/uncertain"):
        h.transcribe_job(job)
    assert len(calls) == 2
    final = h.transcribe_job(job, retry_uncertain=True)
    assert final["timing_precision"] == "chunk"
    assert final["segments"][1]["start"] == 2
    # First successful chunk is reused; failed and remaining chunks are explicit retry.
    assert calls.count("00000.pcm") == 1
    assert (job / "transcript.srt").exists()


def test_source_change_prevents_clipping(media, tmp_path):
    job = h.prepare_one(media, tmp_path / "output")
    with media.open("ab") as handle:
        handle.write(b"modified")
    with pytest.raises(h.SkillError, match="changed"):
        h.checked_job(job)


def test_missing_credentials_fail_before_network(monkeypatch, tmp_path):
    for name in ("XFYUN_APP_ID", "XFYUN_API_KEY", "XFYUN_API_SECRET"):
        monkeypatch.delenv(name, raising=False)
    assert h.main(["transcribe", str(tmp_path)]) == 1


def test_lock_blocks_parallel_request_without_network(tmp_path):
    job = tmp_path / "job"
    job.mkdir()
    (job / "media.json").write_text("{}")
    (job / ".transcribe.lock").write_text('{"pid": 123}')
    with pytest.raises(h.SkillError, match="lock exists"):
        h.transcribe_job(job)


def test_replacement_metadata_can_be_on_outer_result():
    acc = h.ResultAccumulator()
    acc.add_message(envelope(piece(0, "旧文字")))
    updated = envelope(piece(1, "修正文字"), status=2)
    updated["payload"]["result"].update({"pgs": "rpl", "rg": [0, 0]})
    acc.add_message(updated)
    assert acc.text() == "修正文字"


@pytest.mark.parametrize("message", [{"header": {"status": 2}}, {"header": None}, {"header": []}, {"header": {"code": False, "status": 2}}])
def test_malformed_terminal_response_cannot_succeed(message):
    acc = h.ResultAccumulator()
    with pytest.raises(h.SkillError):
        acc.add_message(message)
    assert not acc.server_done


def test_delayed_audio_track_is_rejected_before_timestamps_are_created(tmp_path):
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        pytest.skip("ffmpeg and ffprobe required")
    source = tmp_path / "delayed-audio.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-y", "-f", "lavfi", "-i", "color=c=navy:s=160x90:r=25:d=6", "-itsoffset", "2", "-f", "lavfi", "-i", "sine=frequency=440:duration=4", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", str(source)], check=True)
    with pytest.raises(h.SkillError, match="aligned track starts"):
        h.prepare_one(source, tmp_path / "output")
    assert not (tmp_path / "output").exists()


def test_clip_failure_resets_current_manifest_and_keeps_last_mode_result(media, tmp_path, monkeypatch):
    job = h.prepare_one(media, tmp_path / "output")
    _, metadata = h.checked_job(job)
    h.export_transcript(job, metadata, [{"id": "s1", "start": 0, "end": 6, "text": "Synthetic fixture only"}], "imported", "manual-test-fixture")
    manifest = tmp_path / "cuts.json"
    h.write_json(manifest, {"clips": [{"id": "a", "start": 1, "end": 2, "segment_ids": ["s1"]}]})
    h.cut_clips(job, manifest)
    old_video = h.read_json(job / "clips/clips-video.json")
    def fail_encoding(command):
        raise h.SkillError("Simulated local encoder failure")
    monkeypatch.setattr(h, "run_media", fail_encoding)
    with pytest.raises(h.SkillError, match="encoder failure"):
        h.cut_clips(job, manifest, audio_only=True)
    pending = h.read_json(job / "clips/clips.partial.json")
    assert pending["status"] == "incomplete"
    assert pending["mode"] == "audio"
    assert pending["clips"] == []
    assert h.read_json(job / "clips/clips.json")["status"] == "incomplete"
    assert h.read_json(job / "clips/clips-video.json") == old_video
    assert not (job / "clips/clips-audio.json").exists()


def file_order_result(text="离线协议模拟文本", bg=880, ed=1680, wb=22, we=75):
    # Official units: bg/ed milliseconds, wb/we 10ms relative to sentence bg.
    return json.dumps({"lattice": [{"json_1best": json.dumps({"st": {"bg": str(bg), "ed": str(ed), "rl": "1", "rt": [{"ws": [{"wb": wb, "we": we, "cw": [{"w": text, "wp": "n"}]}, {"wb": we, "we": we, "cw": [{"w": "。", "wp": "p"}]}]}]}})}]})


def file_query(order="synthetic-order", status=4, fail_type=0, result=None):
    return {"code": "000000", "content": {"orderInfo": {"orderId": order, "status": status, "failType": fail_type}, "orderResult": file_order_result() if result is None and status == 4 else result}}


def test_file_signature_matches_java_urlencoding_and_known_hmac_vector():
    params = {"fileName": "测试 ~*.mp3", "signatureRandom": "Abcd1234Efgh5678", "dateTime": "2026-11-25T10:00:00+0800", "accessKeyId": "test-key", "empty": "", "null": None, "signature": "must-be-excluded"}
    assert h.file_signature_base(params) == "accessKeyId=test-key&dateTime=2026-11-25T10%3A00%3A00%2B0800&fileName=%E6%B5%8B%E8%AF%95+%7E*.mp3&signatureRandom=Abcd1234Efgh5678"
    assert h.build_file_signature(params, "test-secret") == "V+vejx2feV+x+skMmsZPqM+vdPw="


def test_file_http_transport_uses_binary_upload_json_query_and_header_signature(tmp_path, monkeypatch):
    calls = []
    class FakeResponse:
        status = 200
        def read(self, size):
            return b'{"code":"000000","content":{"orderId":"test-order"}}'
    class FakeConnection:
        def __init__(self, host, timeout):
            assert host == "office-api-ist-dx.iflyaisol.com"
        def request(self, method, target, body, headers):
            calls.append((method, target, body.read() if hasattr(body, "read") else body, headers))
        def getresponse(self):
            return FakeResponse()
        def close(self):
            pass
    monkeypatch.setattr(h.http.client, "HTTPSConnection", FakeConnection)
    audio = tmp_path / "analysis.mp3"
    audio.write_bytes(b"synthetic binary")
    params = {"accessKeyId": "fake-key", "dateTime": "2026-11-25T10:00:00+0800"}
    assert h.parse_upload_order(h.file_api_request("/v2/upload", params, "fake-secret", tmp_path / "upload.json", audio)) == "test-order"
    h.file_api_request("/v2/getResult", params, "fake-secret", tmp_path / "query.json")
    assert calls[0][0] == "POST"
    assert calls[0][2] == b"synthetic binary"
    assert calls[0][3]["Content-Type"] == "application/octet-stream"
    assert calls[0][3]["Content-Length"] == "16"
    assert calls[1][2] == b"{}"
    assert calls[1][3]["Content-Type"] == "application/json"
    assert calls[0][3]["signature"] == h.build_file_signature(params, "fake-secret")
    assert "signature=" not in calls[0][1]
    assert "fake-secret" not in (tmp_path / "upload.json").read_text()


@pytest.mark.parametrize("response", [{}, {"code": False, "content": {}}, {"code": 0.0, "content": {}}, {"code": "0", "content": {}}, {"code": "000000"}, {"code": "100003", "content": {}}, {"code": "000000", "content": {}}])
def test_file_upload_requires_explicit_success_and_order_id(response):
    with pytest.raises(h.SkillError):
        h.parse_upload_order(response)


@pytest.mark.parametrize("response", [file_query(order="different"), file_query(status=2), file_query(status=-1), file_query(fail_type=6), file_query(result=""), {"code": "000000", "content": {"orderInfo": {"orderId": "synthetic-order", "status": 4}}}])
def test_file_query_errors_are_never_empty_success(response):
    with pytest.raises(h.SkillError):
        h.query_file_status(response, "synthetic-order")


def test_file_sentence_and_word_times_have_distinct_units_and_original_text():
    segments = h.parse_file_transcript(file_order_result(), 6)
    assert segments[0]["start"] == 0.88
    assert segments[0]["end"] == 1.68
    assert segments[0]["words"][0]["start"] == 1.10
    assert segments[0]["words"][0]["end"] == 1.63
    assert segments[0]["words"][1]["start"] == segments[0]["words"][1]["end"] == 1.63
    assert segments[0]["speaker"] == "1"
    assert segments[0]["text"] == "离线协议模拟文本。"
    assert segments[0]["timing_precision"] == "sentence"
    smoothed = json.loads(file_order_result("已规整文本"))
    smoothed["lattice2"] = json.loads(file_order_result("保留原话"))["lattice"]
    assert h.parse_file_transcript(json.dumps(smoothed), 6)[0]["text"] == "保留原话。"
    padded = h.parse_file_transcript(file_order_result(bg=5000, ed=6100, wb=0, we=90), 6)[0]
    assert padded["end"] == 6
    assert padded["end_clamped_to_source"] is True
    assert padded["source_ed_ms"] == 6100
    with pytest.raises(h.SkillError, match="timestamp"):
        h.parse_file_transcript(file_order_result(ed=9000), 6)
    with pytest.raises(h.SkillError, match="timestamp"):
        h.parse_file_transcript(file_order_result(we=900), 6)
    assert h.query_file_status({"code": "100013"}, "synthetic-order") == ("processing", None)


@pytest.fixture
def file_job(media, tmp_path, monkeypatch):
    for name in ("XFYUN_APP_ID", "XFYUN_API_KEY", "XFYUN_API_SECRET"):
        monkeypatch.setenv(name, "synthetic-long-credential-not-real")
    return h.prepare_one(media, tmp_path / "output")


def test_file_order_persisted_before_query_and_pending_resume_never_reuploads(file_job, monkeypatch):
    calls, upload_nonces, query_nonces = [], [], []
    def fake_request(endpoint, params, secret, raw_path, audio_path=None, timeout=120):
        calls.append(endpoint)
        if endpoint == "/v2/upload":
            assert params["duration"] == str(round(h.probe_media(file_job / "analysis.mp3")["duration"] * 1000))
            assert params["eng_smoothproc"] == params["eng_colloqproc"] == "false"
            assert params["language"] == "autodialect"
            assert audio_path == file_job / "analysis.mp3"
            upload_nonces.append(params["signatureRandom"])
            value = {"code": "000000", "content": {"orderId": "synthetic-order"}}
        else:
            state = h.read_json(next((file_job / "file-asr").glob("*/state.json")))
            assert state["order_id"] == "synthetic-order"
            query_nonces.append(params["signatureRandom"])
            value = file_query(status=3) if calls.count("/v2/getResult") == 1 else file_query()
        h.write_json(raw_path, value)
        return value
    monkeypatch.setattr(h, "file_api_request", fake_request)
    first = h.transcribe_file(file_job, max_polls=1, poll_interval=0)
    assert first["status"] == "pending"
    assert h.read_json(file_job / "transcription_status.json")["status"] == "pending"
    assert not (file_job / "transcript.json").exists()
    result = h.transcribe_file(file_job, max_polls=1, poll_interval=0, resume=True)
    assert result["status"] == "complete"
    assert calls == ["/v2/upload", "/v2/getResult", "/v2/getResult"]
    assert query_nonces == upload_nonces * 2
    output = h.read_json(file_job / "transcript.json")
    assert output["segments"][0]["words"][0]["start"] == 1.1
    assert "00:00:00,880 --> 00:00:01,680" in (file_job / "transcript.srt").read_text()
    for name in ("XFYUN_APP_ID", "XFYUN_API_KEY", "XFYUN_API_SECRET"):
        monkeypatch.delenv(name)
    assert h.transcribe_file(file_job)["cached"] is True
    assert len(calls) == 3


def test_file_unknown_upload_blocks_repeat_and_resume_without_order(file_job, monkeypatch):
    calls = []
    def disconnected(*args, **kwargs):
        calls.append(1)
        raise h.SkillError("simulated network disconnection")
    monkeypatch.setattr(h, "file_api_request", disconnected)
    with pytest.raises(h.SkillError, match="disconnection"):
        h.transcribe_file(file_job)
    with pytest.raises(h.SkillError, match="no automatic re-upload"):
        h.transcribe_file(file_job)
    with pytest.raises(h.SkillError, match="will not upload"):
        h.transcribe_file(file_job, resume=True)
    assert len(calls) == 1
    assert not (file_job / ".transcribe.lock").exists()
    state = h.read_json(next((file_job / "file-asr").glob("*/state.json")))
    assert state["status"] == "submission_unknown_or_rejected"
    assert "order_id" not in state


def test_file_upload_saved_response_recovers_crash_and_query_errors_resume(file_job, monkeypatch):
    calls = []
    def crash_after_response(endpoint, params, secret, raw_path, audio_path=None, timeout=120):
        calls.append(endpoint)
        if endpoint == "/v2/upload":
            h.write_json(raw_path, {"code": "000000", "content": {"orderId": "synthetic-order"}})
            raise KeyboardInterrupt("simulated crash")
        if calls.count("/v2/getResult") == 1:
            raise h.SkillError("simulated query error")
        return file_query()
    monkeypatch.setattr(h, "file_api_request", crash_after_response)
    with pytest.raises(KeyboardInterrupt):
        h.transcribe_file(file_job)
    with pytest.raises(h.SkillError, match="query error"):
        h.transcribe_file(file_job, resume=True)
    state = h.read_json(next((file_job / "file-asr").glob("*/state.json")))
    assert state["order_id"] == "synthetic-order"
    assert state["recovered_from_saved_response"] is True
    assert h.transcribe_file(file_job, resume=True)["status"] == "complete"
    assert calls == ["/v2/upload", "/v2/getResult", "/v2/getResult"]


def test_file_lock_blocks_concurrent_backend_and_option_validation(file_job, monkeypatch):
    (file_job / ".transcribe.lock").write_text('{"pid":123}')
    with pytest.raises(h.SkillError, match="lock exists"):
        h.transcribe_file(file_job)
    (file_job / ".transcribe.lock").unlink()
    for options in ({"role_type": 3}, {"role_num": 2}, {"feature_ids": "not-allowed"}, {"max_polls": 0}):
        with pytest.raises(h.SkillError):
            h.transcribe_file(file_job, **options)
    assert not (file_job / "file-asr").exists()


def test_clip_excerpt_uses_timed_words_excludes_removed_speaker_and_labels_context(media, tmp_path):
    job = h.prepare_one(media, tmp_path / "output")
    _, metadata = h.checked_job(job)
    segment = {"id": "s1", "start": 0, "end": 6, "text": "主持人提问。讲解者回答。", "words": [{"text": "主持人提问", "start": 0.2, "end": 2.5}, {"text": "。", "start": 2.5, "end": 2.5}, {"text": "讲解者", "start": 3.0, "end": 4.0}, {"text": "回答", "start": 4.1, "end": 5.5}, {"text": "。", "start": 5.5, "end": 5.5}]}
    h.export_transcript(job, metadata, [segment], "synthetic-protocol-test", "sentence-with-word-timestamps")
    manifest = tmp_path / "word-cuts.json"
    h.write_json(manifest, {"clips": [{"id": "answer", "start": 3.2, "end": 5.8, "segment_ids": ["s1"]}]})
    result = h.cut_clips(job, manifest)["clips"][0]
    assert result["transcript_excerpt"] == "讲解者回答。"
    assert result["transcript_excerpt_scope"] == "word-overlap"
    assert result["excerpt_boundary_words"] == ["讲解者"]
    assert result["excerpt_review_required"] is True
    no_words = {key: value for key, value in segment.items() if key != "words"}
    fallback = h.clip_transcript_excerpt([no_words], 3.2, 5.8)
    assert fallback["transcript_excerpt"] == segment["text"]
    assert fallback["transcript_excerpt_scope"] == "overlapping-context"
    assert fallback["excerpt_context_segment_ids"] == ["s1"]
