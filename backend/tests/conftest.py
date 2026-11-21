import pytest


@pytest.fixture(autouse=True)
def isolated_ai_storage(tmp_path, monkeypatch):
    monkeypatch.setenv("AI_STATE_PATH", str(tmp_path / "analysis.sqlite3"))
