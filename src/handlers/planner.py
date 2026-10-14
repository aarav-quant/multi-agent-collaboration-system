"""Planner (supervisor) agent: plans the work and delegates to specialists.

Pattern: supervisor-worker / hierarchical delegation.
Input : {"task_id"}
Output: {"task_id", "request", "subtasks": [{"task_id", "index", "agent", "instruction"}],
         "revision": 0, "feedback": ""}
The Step Functions Map state fans out over "subtasks" in parallel.
"""
from __future__ import annotations

import json

from agents.base import AgentOutputError
from agents.prompts import SPECIALISTS
from common import events, observability
from common.config import get_config
from common.memory import get_store
from handlers.common import run_and_record

logger = observability.init()


def _validate(plan, request: str, limit: int) -> list[dict]:
    """Never trust model output blindly: keep only well-formed, known agents."""
    raw = plan.get("subtasks", []) if isinstance(plan, dict) else []
    subtasks = [
        {"agent": s["agent"], "instruction": s["instruction"].strip()}
        for s in raw
        if isinstance(s, dict) and s.get("agent") in SPECIALISTS
        and isinstance(s.get("instruction"), str) and s["instruction"].strip()
    ][:limit]
    if not subtasks:  # fallback plan so the workflow still produces an answer
        logger.warning("planner output unusable, using default plan: %s", plan)
        subtasks = [{"agent": "researcher", "instruction": f"Research: {request}"},
                    {"agent": "analyst", "instruction": f"Analyse and recommend: {request}"}]
    return subtasks


def handler(event, context=None):
    cfg, store = get_config(), get_store()
    task_id = event["task_id"]
    task = store.get_task(task_id)
    request = task["request"]
    store.update_status(task_id, "PLANNING")

    try:
        plan = run_and_record(task_id, task, "planner", "planner", 0, request).output
    except AgentOutputError:  # model never produced valid JSON -> use the fallback plan
        logger.warning("planner output unparseable, falling back to default plan")
        plan = {}
    subtasks = _validate(plan, request, cfg.max_subtasks)
    store.add_message(task_id, "planner", json.dumps(subtasks))
    events.publish("AgentCompleted", {"task_id": task_id, "agent": "planner",
                                      "delegated_to": [s["agent"] for s in subtasks]})
    return {"task_id": task_id, "request": request[:2000],  # used by the Bedrock title step
            "subtasks": [{"task_id": task_id, "index": i, **s} for i, s in enumerate(subtasks)],
            "revision": 0, "feedback": ""}
