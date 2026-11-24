import importlib.util
from pathlib import Path

MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts/run.py"
spec = importlib.util.spec_from_file_location("skill_runner", MODULE_PATH)
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


def test_env_is_literal_and_process_values_take_precedence(tmp_path):
    path = tmp_path / ".env"
    path.write_text('XFYUN_APP_ID="$(touch should-not-exist)"\nXFYUN_API_KEY=from-file\n')
    result = runner.local_environment(path, {"XFYUN_API_KEY": "from-process"})
    assert result["XFYUN_APP_ID"] == "$(touch should-not-exist)"
    assert result["XFYUN_API_KEY"] == "from-process"
    assert not (tmp_path / "should-not-exist").exists()


def test_env_rejects_unrelated_execution_settings_without_exposing_values(tmp_path):
    path = tmp_path / ".env"
    path.write_text("PYTHONPATH=some-private-value\n")
    try:
        runner.local_environment(path, {})
    except ValueError as exc:
        assert "some-private-value" not in str(exc)
    else:
        raise AssertionError("Unrelated settings must not be loaded")
