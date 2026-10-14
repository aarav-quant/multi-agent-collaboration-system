"""Critic / judge agent: approves the draft or sends it back to the writer.

Pattern: critic-judge conflict resolution. The critic checks that every
disagreement from the peer debate is resolved in the draft.

Loop guards (termination is guaranteed three ways):
  1. revision cap   - after MAX_REVISIONS rewrites the task is finalized anyway
  2. token budget   - once the task used TOKEN_BUDGET tokens it is finalized
  3. state machine  - the Choice state re-checks the revision cap, and the whole
                      execution has a hard timeout (see infra/stack.py)

Input : {"task_id", "revision", "draft_key", ...}
Output: {..., "approved", "score", "next_action": "revise" | "finalize", "stop_reason"}
"""
from __future__ import annotations

import json

from common import events, observability
from common.config import get_config
from common.memory import get_store, usage_totals
from common.storage import critique_key, get_storage
from handlers.common import run_and_record
from handlers.writer import specialist_context

logger = observability.init()


def handler(event, context=None):
    cfg, store, storage = get_config(), get_store(), get_storage()
    task_id, revision, key = event["task_id"], int(event["revision"]), event["draft_key"]
    task = store.get_task(task_id)
    draft = storage.get_text(key)

    store.update_status(task_id, "REVIEWING", revision=revision)
    result = run_and_record(
        task_id, task, "critic", "critic", revision, task["request"],
        f"{specialist_context(task_id)}\n\nDraft to judge (revision {revision}):\n{draft}")
    verdict = result.output if isinstance(result.output, dict) else {}

    approved = verdict.get("approved") is True or str(verdict.get("approved")).lower() == "true"
    conflicts = verdict.get("unresolved_conflicts") or []
    feedback = str(verdict.get("feedback") or "").strip()
    if conflicts and not approved:
        feedback += "\nResolve these disagreements explicitly: " + "; ".join(map(str, conflicts))
    score = verdict.get("score")
    store.add_message(task_id, "critic", json.dumps(verdict), revision)
    storage.put_text(critique_key(task_id, revision), json.dumps(verdict, indent=2))

    used = usage_totals(store.get_usage(task_id))["total_tokens"]
    if approved:
        next_action, stop_reason = "finalize", "approved"
    elif revision >= cfg.max_revisions:
        next_action, stop_reason = "finalize", "max_revisions"
    elif used >= cfg.token_budget:
        next_action, stop_reason = "finalize", "token_budget"
    else:
        next_action, stop_reason = "revise", ""
    next_revision = revision + 1 if next_action == "revise" else revision
    store.update_status(task_id, "REVIEWED", score=score, tokens_so_far=used)

    events.publish("ReviewCompleted", {"task_id": task_id, "revision": revision,
                                       "approved": approved, "score": score,
                                       "next_action": next_action, "stop_reason": stop_reason})
    return {"task_id": task_id, "revision": next_revision, "feedback": feedback,
            "draft_key": key, "approved": approved, "score": score,
            "next_action": next_action, "stop_reason": stop_reason}
