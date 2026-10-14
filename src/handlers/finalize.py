"""Final steps: publish the result with a cost/latency summary, or record a failure."""
from __future__ import annotations

import json
import time

from common import events, observability
from common.config import estimate_cost, get_config
from common.memory import get_store, usage_totals
from common.storage import final_key, get_storage, summary_key

logger = observability.init()


def build_summary(task_id: str) -> dict:
    store = get_store()
    task = store.get_task(task_id)
    totals = usage_totals(store.get_usage(task_id))
    return {
        "task_id": task_id,
        "prompt_version": task.get("prompt_version"),
        "shadow_of": task.get("shadow_of"),
        **totals,
        "est_cost_usd": estimate_cost(totals["input_tokens"], totals["output_tokens"]),
        "wall_clock_s": int(time.time()) - int(task.get("created_at", time.time())),
    }


def handler(event, context=None):
    """Input: critic output. Copies the last draft to final.md and records the run summary."""
    cfg, store, storage = get_config(), get_store(), get_storage()
    task_id = event["task_id"]
    key = final_key(task_id)
    storage.put_text(key, storage.get_text(event["draft_key"]))

    summary = {**build_summary(task_id),
               "approved": bool(event.get("approved", False)),
               "score": event.get("score"),
               "revisions": int(event.get("revision", 0)),
               "stop_reason": event.get("stop_reason") or "approved"}
    storage.put_text(summary_key(task_id), json.dumps(summary, indent=2))
    store.update_status(task_id, "COMPLETED", result_key=key,
                        **{k: v for k, v in summary.items() if k not in ("task_id",) and v is not None})
    events.publish("TaskCompleted", {"bucket": cfg.bucket_name, "result_key": key, **summary})
    return {"task_id": task_id, "status": "COMPLETED", "result_key": key}


def failure_handler(event, context=None):
    """Input: {"task_id", "error": {"Error", "Cause"}} from a Step Functions Catch."""
    task_id = event["task_id"]
    err = event.get("error") or {}
    get_store().update_status(task_id, "FAILED", error=str(err.get("Error", "Unknown")),
                              cause=str(err.get("Cause", ""))[:2000])
    events.publish("TaskFailed", {"task_id": task_id, "error": err.get("Error")})
    return {"task_id": task_id, "status": "FAILED"}
