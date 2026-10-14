"""Artifact storage: requests, research notes, generated code, critiques, drafts, results.

S3 in the cloud; a local folder in local mode (so you can open the files).

  tasks/<id>/request.txt
  tasks/<id>/notes/<agent>-<i>.md          research / analysis notes
  tasks/<id>/code/coder-<i>.md             generated code
  tasks/<id>/debate/<agent>-<i>.md         peer-debate positions
  tasks/<id>/critiques/v<N>.json           critic verdicts
  tasks/<id>/drafts/v<N>.md                writer drafts
  tasks/<id>/final.md                      approved result
  tasks/<id>/run_summary.json              tokens, cost, latency, stop reason
"""
from __future__ import annotations

from pathlib import Path

from common.config import get_config


def request_key(task_id: str) -> str:
    return f"tasks/{task_id}/request.txt"


def notes_key(task_id: str, agent: str, index: int) -> str:
    folder = "code" if agent == "coder" else "notes"
    return f"tasks/{task_id}/{folder}/{agent}-{index}.md"


def debate_key(task_id: str, agent: str, index: int) -> str:
    return f"tasks/{task_id}/debate/{agent}-{index}.md"


def critique_key(task_id: str, revision: int) -> str:
    return f"tasks/{task_id}/critiques/v{revision}.json"


def draft_key(task_id: str, revision: int) -> str:
    return f"tasks/{task_id}/drafts/v{revision}.md"


def final_key(task_id: str) -> str:
    return f"tasks/{task_id}/final.md"


def summary_key(task_id: str) -> str:
    return f"tasks/{task_id}/run_summary.json"


def _content_type(key: str) -> str:
    if key.endswith(".md"):
        return "text/markdown"
    if key.endswith(".json"):
        return "application/json"
    return "text/plain"


class S3Storage:
    def __init__(self, bucket: str):
        import boto3
        self.bucket = bucket
        self.s3 = boto3.client("s3")

    def put_text(self, key: str, text: str) -> None:
        self.s3.put_object(Bucket=self.bucket, Key=key, Body=text.encode("utf-8"),
                           ContentType=f"{_content_type(key)}; charset=utf-8")

    def get_text(self, key: str) -> str:
        return self.s3.get_object(Bucket=self.bucket, Key=key)["Body"].read().decode("utf-8")


class LocalStorage:
    def __init__(self, root: str):
        self.root = Path(root)

    def put_text(self, key, text):
        path = self.root / key
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def get_text(self, key):
        return (self.root / key).read_text(encoding="utf-8")

    def path(self, key) -> Path:
        return self.root / key


_storage = None


def get_storage():
    global _storage
    if _storage is None:
        cfg = get_config()
        _storage = LocalStorage(cfg.local_output_dir) if cfg.local_mode else S3Storage(cfg.bucket_name)
    return _storage


def reset() -> None:
    global _storage
    _storage = None
