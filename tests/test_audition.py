"""Audition tests mock rendering; they do not read credentials or call services."""
import importlib.util
import json
from pathlib import Path

import pytest

MODULE = Path(__file__).resolve().parents[1] / "scripts/voice_audition.py"
spec = importlib.util.spec_from_file_location("voice_audition_test", MODULE)
audition = importlib.util.module_from_spec(spec)
spec.loader.exec_module(audition)


def fixture(tmp_path):
    path = tmp_path / "audition.json"
    path.write_text(json.dumps({"text": "同一篇新闻。", "voices": [
        {"id": "first", "name": "声音甲", "voice": "voice_a"},
        {"id": "second", "name": "声音乙", "voice": "voice_b"}]}, ensure_ascii=False))
    return audition.load_audition(path)


def test_same_text_plans_single_voice_and_rejects_unsafe_ids(tmp_path):
    data = fixture(tmp_path)
    manifest = audition.audition_manifest(data)
    assert manifest["planned_request_count"] == 2
    assert manifest["planned_characters"] == 12
    for voice in data["voices"]:
        doc = audition.voice_document(data["text"], voice)
        plan = audition.podcast.make_plan(doc)
        assert len(doc["speakers"]) == 1
        assert plan["requests"][0]["text"] == data["text"]
        assert plan["requests"][0]["voice"] == voice["voice"]
    path = tmp_path / "bad.json"
    data["voices"][0]["id"] = "../escape"
    path.write_text(json.dumps(data))
    with pytest.raises(audition.podcast.PodcastError, match="path-safe"):
        audition.load_audition(path)


def test_failure_keeps_completed_voice_and_resume_delegates_without_retry(tmp_path, monkeypatch):
    data = fixture(tmp_path)
    output = tmp_path / "output"
    calls = []

    def reject_second(doc, directory, **options):
        calls.append((Path(directory).name, options))
        if Path(directory).name == "second":
            raise audition.podcast.PodcastError("Previous request has unknown outcome")
        return {"duration_seconds": 1.2}

    monkeypatch.setattr(audition.podcast, "render", reject_second)
    with pytest.raises(audition.podcast.PodcastError, match="unknown"):
        audition.render_auditions(data, output)
    state = json.loads((output / "audition-manifest.json").read_text())
    assert state["status"] == "failed"
    assert [v["status"] for v in state["voices"]] == ["complete", "failed"]
    assert not (output / ".render.lock").exists()
    calls.clear()
    with pytest.raises(audition.podcast.PodcastError, match="Existing audition"):
        audition.render_auditions(data, output)
    assert not calls
    with pytest.raises(audition.podcast.PodcastError, match="unknown"):
        audition.render_auditions(data, output, resume=True)
    assert [name for name, _ in calls] == ["first", "second"]
    assert all(opts["resume"] is True and opts["retry_uncertain"] is False for _, opts in calls)


def test_complete_manifest_and_stale_batch_lock_stop_before_render(tmp_path, monkeypatch):
    data = fixture(tmp_path)
    calls = []

    def fake_render(doc, directory, **options):
        calls.append(Path(directory).name)
        return {"duration_seconds": 2.5}

    monkeypatch.setattr(audition.podcast, "render", fake_render)
    output = tmp_path / "output"
    result = audition.render_auditions(data, output)
    assert result["status"] == "complete"
    state = json.loads((output / "audition-manifest.json").read_text())
    assert all(v["status"] == "complete" for v in state["voices"])
    (output / ".render.lock").write_text("old process")
    calls.clear()
    with pytest.raises(audition.podcast.PodcastError, match="locked"):
        audition.render_auditions(data, output, resume=True)
    assert not calls
