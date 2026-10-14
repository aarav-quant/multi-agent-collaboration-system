"""Unit tests for agents, handlers and the local workflow graph (offline, fake LLM)."""
import json

import pytest

from agents.base import AgentOutputError, run_agent
from common import events, llm
from common.fake_llm import FakeLLM
from common.memory import get_store
from common.storage import get_storage
from handlers import intake, mailbox, planner
from workflow.local_graph import build_workflow


def run(request="Compare SQS and Kinesis"):
    task_id = intake.create_task(request)
    return task_id, build_workflow().invoke({"task_id": task_id})


def test_full_workflow_with_debate_and_one_revision():
    task_id, result = run()
    task = get_store().get_task(task_id)
    assert task["status"] == "COMPLETED" and task["approved"] is True
    assert task["revisions"] == 1 and task["stop_reason"] == "approved"
    agents = {m["agent"] for m in get_store().get_messages(task_id)}
    # planner, 3 specialists, 3 debate positions, critic
    assert {"planner", "researcher", "coder", "analyst", "critic",
            "researcher-debate", "coder-debate", "analyst-debate"} <= agents
    storage = get_storage()
    assert "draft 2" in storage.get_text(result["result_key"])
    assert "def handler" in storage.get_text(f"tasks/{task_id}/code/coder-1.md")
    assert json.loads(storage.get_text(f"tasks/{task_id}/critiques/v0.json"))["approved"] is False
    summary = json.loads(storage.get_text(f"tasks/{task_id}/run_summary.json"))
    assert summary["total_tokens"] > 0 and summary["est_cost_usd"] > 0
    assert summary["agent_calls"] == 1 + 3 + 3 + 2 + 2   # planner, work, debate, 2x writer, 2x critic
    assert events.LOCAL_EVENTS[-1]["detail_type"] == "TaskCompleted"


def test_specialists_send_peer_messages():
    task_id, _ = run()
    peer = [e["detail"] for e in events.LOCAL_EVENTS if e["detail_type"] == "PeerMessage"]
    assert len(peer) == 3
    assert {p["from"] for p in peer} == {"researcher", "coder", "analyst"}
    assert all(p["from"] not in p["to"] for p in peer)


def test_mailbox_delivers_peer_messages_to_inboxes():
    store = get_store()
    task_id = intake.create_task("hi")
    body = json.dumps({"detail": {"task_id": task_id, "from": "coder", "index": 1,
                                  "to": ["researcher", "analyst"], "summary": "use FastAPI"}})
    out = mailbox.handler({"Records": [{"messageId": "m1", "body": body},
                                       {"messageId": "m2", "body": "{bad"}]})
    assert out == {"batchItemFailures": [{"itemIdentifier": "m2"}]}
    inbox = {m["agent"] for m in store.get_messages(task_id)}
    assert inbox == {"inbox-researcher", "inbox-analyst"}


def test_agent_self_corrects_invalid_json():
    fake = FakeLLM(script={"planner": ["not json", '{"subtasks": []}']})
    llm.set_llm(fake)
    result = run_agent("planner", "x")
    assert result.output == {"subtasks": []} and result.attempts == 2
    assert result.input_tokens > 0 and result.output_tokens > 0


def test_agent_gives_up_after_max_attempts():
    llm.set_llm(FakeLLM(script={"critic": ["still not json"]}))
    with pytest.raises(AgentOutputError):
        run_agent("critic", "x")


def test_planner_drops_unknown_agents_and_falls_back():
    plan = {"subtasks": [{"agent": "hacker", "instruction": "rm -rf"}]}
    subtasks = planner._validate(plan, "req", limit=4)
    assert [s["agent"] for s in subtasks] == ["researcher", "analyst"]


def test_prompt_versions_differ_only_where_intended():
    from agents.prompts import PROMPTS
    v1, v2 = PROMPTS["v1"], PROMPTS["v2"]
    assert v1.keys() == v2.keys()
    assert [r for r in v1 if v1[r] != v2[r]] == ["writer"]


def test_shadow_task_uses_shadow_prompts(monkeypatch):
    monkeypatch.setenv("SHADOW_PROMPT_VERSION", "v2")
    body = json.dumps({"task_id": "t1", "request": "Compare SQS and Kinesis"})
    assert intake.handler({"Records": [{"messageId": "1", "body": body}]})["batchItemFailures"] == []
    store = get_store()
    assert store.get_task("t1")["prompt_version"] == "v1"
    shadow = store.get_task("t1-shadow")
    assert shadow["prompt_version"] == "v2" and shadow["shadow_of"] == "t1"


def test_intake_reports_bad_messages_individually():
    event = {"Records": [{"messageId": "good", "body": json.dumps({"request": "hi"})},
                         {"messageId": "bad", "body": json.dumps({"request": ""})},
                         {"messageId": "ugly", "body": "{not json"}]}
    assert intake.handler(event) == {"batchItemFailures": [{"itemIdentifier": "bad"},
                                                           {"itemIdentifier": "ugly"}]}
