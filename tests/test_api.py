"""Website + JSON API handler (offline)."""
import json

from handlers import api
from workflow.local_graph import build_workflow


def call(method, path, body=None):
    event = {"requestContext": {"http": {"method": method}}, "rawPath": path}
    if body is not None:
        event["body"] = json.dumps(body)
    out = api.handler(event)
    is_json = out["headers"]["content-type"].startswith("application/json")
    return out["statusCode"], (json.loads(out["body"]) if is_json else out["body"])


def test_serves_the_website():
    status, html = call("GET", "/")
    assert status == 200 and "Ask a team of AI agents" in html


def test_submit_validates_the_question():
    assert call("POST", "/api/tasks", {"request": "hi"})[0] == 400
    assert call("POST", "/api/tasks", {"request": "x" * 2001})[0] == 400


def test_access_code_is_enforced_when_configured(monkeypatch):
    monkeypatch.setenv("SITE_ACCESS_CODE", "viva2026")
    assert call("GET", "/api/config")[1] == {"access_code_required": True}
    assert call("POST", "/api/tasks", {"request": "Compare SQS and Kinesis"})[0] == 403
    assert call("POST", "/api/tasks", {"request": "Compare SQS and Kinesis",
                                       "access_code": "viva2026"})[0] == 202


def test_submit_then_watch_the_task_finish():
    status, body = call("POST", "/api/tasks", {"request": "Compare SQS and Kinesis for orders"})
    assert status == 202
    task_id = body["task_id"]
    build_workflow().invoke({"task_id": task_id})   # what Step Functions does in the cloud
    status, task = call("GET", f"/api/tasks/{task_id}")
    assert status == 200 and task["status"] == "COMPLETED"
    assert task["report"].startswith("# Report") and task["total_tokens"] > 0
    assert {"planner", "critic", "coder-debate"} <= {m["agent"] for m in task["messages"]}


def test_unknown_and_queued_tasks():
    assert call("GET", "/api/tasks/not-yet-created")[1]["status"] == "QUEUED"
    assert call("GET", "/api/tasks/bad id!")[0] == 400
    assert call("DELETE", "/api/tasks/x")[0] == 404
