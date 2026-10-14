"""Specialist agents (researcher, coder, analyst). One Lambda serves every specialist.

mode "work"   (round 1): do the delegated subtask, write notes/code to the
               blackboard + S3, then send a PeerMessage to the other specialists
               asynchronously (EventBridge -> SQS -> mailbox Lambda).
mode "debate" (round 2): peer-to-peer debate. Read the peers' notes from the
               blackboard, state agreements/disagreements and a revised position.
               The critic later acts as judge over these disagreements.

Input : {"task_id", "index", "agent", "instruction", "mode"?}   (one Map item)
Output: {"agent", "index", "mode", "message_id"}               (content stays in DynamoDB/S3)
"""
from __future__ import annotations

import json

from agents.prompts import DEBATE_INSTRUCTION, SPECIALISTS
from common import events, observability
from common.memory import get_store
from common.storage import debate_key, get_storage, notes_key
from handlers.common import run_and_record

logger = observability.init()


def _round1_notes(task_id: str) -> list[dict]:
    return [m for m in get_store().get_messages(task_id) if m["agent"] in SPECIALISTS]


def _work(task_id: str, task: dict, agent: str, index: int, instruction: str) -> dict:
    store = get_store()
    result = run_and_record(task_id, task, agent, agent, index, instruction,
                            context=f"Original user request:\n{task['request']}")
    message_id = store.add_message(task_id, agent, result.output, index)
    get_storage().put_text(notes_key(task_id, agent, index), result.output)

    # Peer-to-peer: tell the other specialists (async, via EventBridge -> SQS mailbox)
    plan = next((m for m in store.get_messages(task_id) if m["agent"] == "planner"), None)
    peers = sorted({s["agent"] for s in json.loads(plan["content"])} - {agent}) if plan else []
    if peers:
        events.publish("PeerMessage", {"task_id": task_id, "from": agent, "index": index,
                                       "to": peers, "summary": result.output[:500]})
    events.publish("AgentCompleted", {"task_id": task_id, "agent": agent, "index": index})
    return {"agent": agent, "index": index, "mode": "work", "message_id": message_id}


def _debate(task_id: str, task: dict, agent: str, index: int) -> dict:
    notes = _round1_notes(task_id)
    own = next((m["content"] for m in notes if m["index"] == index), "")
    peers = [m for m in notes if m["index"] != index]
    if not peers:  # a single specialist has nobody to debate with
        return {"agent": agent, "index": index, "mode": "debate", "skipped": True}

    peer_text = "\n\n".join(f"### {m['agent']} #{m['index']}\n{m['content']}" for m in peers)
    label = f"{agent}-debate"
    result = run_and_record(task_id, task, agent, label, index,
                            DEBATE_INSTRUCTION.format(own=own, peers=peer_text),
                            context=f"Original user request:\n{task['request']}")
    message_id = get_store().add_message(task_id, label, result.output, index)
    get_storage().put_text(debate_key(task_id, agent, index), result.output)
    events.publish("AgentCompleted", {"task_id": task_id, "agent": label, "index": index})
    return {"agent": agent, "index": index, "mode": "debate", "message_id": message_id}


def handler(event, context=None):
    task_id, agent, index = event["task_id"], event["agent"], int(event["index"])
    if agent not in SPECIALISTS:
        raise ValueError(f"unknown specialist {agent!r}")
    task = get_store().get_task(task_id)
    if event.get("mode") == "debate":
        return _debate(task_id, task, agent, index)
    return _work(task_id, task, agent, index, event["instruction"])
