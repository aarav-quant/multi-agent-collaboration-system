import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


@pytest.fixture(autouse=True)
def local_env(monkeypatch, tmp_path):
    """Every test runs fully offline with fresh in-memory backends."""
    monkeypatch.setenv("LOCAL_MODE", "1")
    monkeypatch.setenv("FAKE_LLM", "1")
    monkeypatch.setenv("LOCAL_OUTPUT_DIR", str(tmp_path))
    monkeypatch.setenv("MAX_REVISIONS", "2")
    from common import events, llm, memory, storage
    for mod in (events, llm, memory, storage):
        mod.reset()
    yield
