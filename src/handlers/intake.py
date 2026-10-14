"""SQS -> intake Lambda -> starts one Step Functions execution per task.

Message body: {"request": "...", "task_id": "optional-client-id"}
Tasks also arrive via EventBridge ("TaskSubmitted" events are routed to the
same queue), so SQS buffers bursts and the Step Functions concurrency stays
under control. Bad messages are reported individually (partial batch
failure) and end up in the dead-letter queue after 3 attempts.

Staged prompt rollout: if SHADOW_PROMPT_VERSION is set, every task is also
started as a shadow task "<task_id>-shadow" using the new prompt set. The
user only ever sees the live result; scripts/compare_prompts.py compares both.
"""
from __future__ import annotations

import json
import re
import uuid

from common import events, observability
from common.config import get_config
from common.memory import get_store
from common.storage import get_storage, request_key

logger = observability.init()
_TASK_ID = re.compile(r"^[A-Za-z0-9_-]{1,70}$")  # leaves room for the "-shadow" suffix
_sfn = None


def create_task(request: str, task_id: str | None = None, prompt_version: str | None = None,
                shadow_of: str | None = None) -> str:
    request = (request or "").strip()
    if not request:
        raise ValueError("message needs a non-empty 'request'")
    task_id = task_id or uuid.uuid4().hex
    if not _TASK_ID.match(task_id.removesuffix("-shadow")):
        raise ValueError(f"invalid task_id {task_id!r}")
    version = prompt_version or get_config().prompt_version
    if get_store().put_task(task_id, request, version, shadow_of):
        get_storage().put_text(request_key(task_id), request)
    return task_id


def start_workflow(task_id: str) -> None:
    global _sfn
    cfg = get_config()
    if cfg.local_mode:
        logger.info("local mode: run scripts/run_local.py to execute the workflow")
        return
    import boto3
    from botocore.exceptions import ClientError
    _sfn = _sfn or boto3.client("stepfunctions")
    try:
        # STATE_MACHINE_ARN is the "live" alias, so executions run the promoted version
        _sfn.start_execution(stateMachineArn=cfg.state_machine_arn, name=task_id,
                             input=json.dumps({"task_id": task_id}))
    except ClientError as e:
        if e.response["Error"]["Code"] != "ExecutionAlreadyExists":
            raise
        logger.info("execution for %s already running (redelivered message)", task_id)


def handler(event, context=None):
    cfg = get_config()
    failures = []
    for record in event.get("Records", []):
        try:
            body = json.loads(record["body"])
            task_id = create_task(body.get("request", ""), body.get("task_id"))
            start_workflow(task_id)
            get_store().update_status(task_id, "RUNNING")
            events.publish("TaskStarted", {"task_id": task_id,
                                           "prompt_version": cfg.prompt_version})
            if cfg.shadow_prompt_version:
                shadow_id = create_task(body["request"], f"{task_id}-shadow",
                                        cfg.shadow_prompt_version, shadow_of=task_id)
                start_workflow(shadow_id)
                events.publish("ShadowTaskStarted", {"task_id": shadow_id, "shadow_of": task_id,
                                                     "prompt_version": cfg.shadow_prompt_version})
        except Exception:  # noqa: BLE001
            logger.exception("could not start task from message %s", record.get("messageId"))
            failures.append({"itemIdentifier": record["messageId"]})
    return {"batchItemFailures": failures}
