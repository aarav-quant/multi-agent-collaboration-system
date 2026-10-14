"""Web front door: serves the website and a small JSON API (API Gateway HTTP API).

  GET  /                  the single-page website (src/web/index.html)
  GET  /api/config        {"access_code_required": bool}
  POST /api/tasks         {"request": "...", "access_code"?: "..."} -> 202 {"task_id"}
  GET  /api/tasks/{id}    live state: status, every agent message so far, usage, final report

Submitting goes through the same SQS task queue as every other client, so the
website gets the queue's buffering, retries and dead-letter handling for free.
"""
from __future__ import annotations

import base64
import hmac
import json
import os
import re
import uuid
from pathlib import Path

from common import observability
from common.config import get_config
from common.memory import get_store
from common.storage import get_storage

logger = observability.init()
WEB_PAGE = Path(__file__).resolve().parents[1] / "web" / "index.html"
TASK_ID = re.compile(r"^[A-Za-z0-9_-]{1,80}$")
MAX_REQUEST_CHARS = 2000
MAX_MESSAGE_CHARS = 6000
SUMMARY_FIELDS = ("status", "title", "prompt_version", "stop_reason", "approved", "score",
                  "revisions", "agent_calls", "input_tokens", "output_tokens", "total_tokens",
                  "est_cost_usd", "wall_clock_s", "error")
_sqs = None


def _response(status: int, body, content_type: str = "application/json") -> dict:
    text = body if isinstance(body, str) else json.dumps(body, default=str)
    return {"statusCode": status, "headers": {"content-type": content_type,
                                              "cache-control": "no-store"}, "body": text}


def _body(event) -> dict:
    raw = event.get("body") or "{}"
    if event.get("isBase64Encoded"):
        raw = base64.b64decode(raw).decode("utf-8")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _access_code() -> str:
    return os.environ.get("SITE_ACCESS_CODE", "")


def submit(body: dict) -> dict:
    request = str(body.get("request") or "").strip()
    if len(request) < 10:
        return _response(400, {"error": "Write a question of at least 10 characters."})
    if len(request) > MAX_REQUEST_CHARS:
        return _response(400, {"error": f"Keep the question under {MAX_REQUEST_CHARS} characters."})
    code = _access_code()
    if code and not hmac.compare_digest(str(body.get("access_code") or ""), code):
        return _response(403, {"error": "That access code is not right."})

    global _sqs
    task_id = uuid.uuid4().hex
    if get_config().local_mode:
        from handlers.intake import create_task
        create_task(request, task_id)
    else:
        import boto3
        _sqs = _sqs or boto3.client("sqs")
        _sqs.send_message(QueueUrl=os.environ["TASK_QUEUE_URL"],
                          MessageBody=json.dumps({"task_id": task_id, "request": request}))
    return _response(202, {"task_id": task_id})


def status(task_id: str) -> dict:
    if not TASK_ID.match(task_id):
        return _response(400, {"error": "Unknown task id."})
    store = get_store()
    try:
        task = store.get_task(task_id)
    except KeyError:  # still in the queue: intake has not created it yet
        return _response(200, {"task_id": task_id, "status": "QUEUED", "messages": []})
    messages = [{"agent": m["agent"], "index": m["index"],
                 "content": m["content"][:MAX_MESSAGE_CHARS]}
                for m in sorted(store.get_messages(task_id), key=lambda m: m.get("created_at", 0))]
    out = {"task_id": task_id, "request": task.get("request", ""), "messages": messages,
           **{k: task[k] for k in SUMMARY_FIELDS if k in task}}
    if task.get("status") == "COMPLETED" and task.get("result_key"):
        out["report"] = get_storage().get_text(task["result_key"])
    return _response(200, out)


def handler(event, context=None):
    method = event.get("requestContext", {}).get("http", {}).get("method", "GET")
    path = event.get("rawPath", "/")
    try:
        if method == "GET" and path in ("/", "/index.html"):
            return _response(200, WEB_PAGE.read_text(encoding="utf-8"), "text/html; charset=utf-8")
        if method == "GET" and path == "/api/config":
            return _response(200, {"access_code_required": bool(_access_code())})
        if method == "POST" and path == "/api/tasks":
            return submit(_body(event))
        if method == "GET" and path.startswith("/api/tasks/"):
            return status(path.removeprefix("/api/tasks/"))
        return _response(404, {"error": "Not found."})
    except Exception:  # noqa: BLE001
        logger.exception("api request failed: %s %s", method, path)
        return _response(500, {"error": "Something went wrong on the server. Try again."})
