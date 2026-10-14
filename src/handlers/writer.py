"""Writer agent: merges specialist notes + debate into one draft (and revises it).

Input : {"task_id", "revision", "feedback"}
Output: {"task_id", "revision", "feedback", "draft_key"}
"""
from __future__ import annotations

from agents.prompts import SPECIALISTS
from common import events, observability
from common.memory import get_store
from common.storage import draft_key, get_storage
from handlers.common import run_and_record

logger = observability.init()


def specialist_context(task_id: str) -> str:
    msgs = get_store().get_messages(task_id)
    notes = [m for m in msgs if m["agent"] in SPECIALISTS]
    debate = [m for m in msgs if m["agent"].endswith("-debate")]
    text = "Specialist notes:\n" + "\n\n".join(
        f"### {m['agent']} #{m['index']}\n{m['content']}" for m in notes)
    if debate:
        text += "\n\nPeer debate (agreements / disagreements):\n" + "\n\n".join(
            f"### {m['agent']} #{m['index']}\n{m['content']}" for m in debate)
    return text


def handler(event, context=None):
    store, storage = get_store(), get_storage()
    task_id = event["task_id"]
    revision = int(event.get("revision", 0))
    feedback = event.get("feedback", "")
    task = store.get_task(task_id)

    parts = [specialist_context(task_id)]
    if revision > 0:
        parts.append("Your previous draft:\n" + storage.get_text(draft_key(task_id, revision - 1)))
        parts.append("Critic feedback to address:\n" + feedback)

    store.update_status(task_id, "WRITING", revision=revision)
    result = run_and_record(task_id, task, "writer", "writer", revision,
                            task["request"], "\n\n".join(parts))
    key = draft_key(task_id, revision)
    storage.put_text(key, result.output)
    events.publish("AgentCompleted", {"task_id": task_id, "agent": "writer", "revision": revision})
    return {"task_id": task_id, "revision": revision, "feedback": feedback, "draft_key": key}
