"""Agent mailbox: delivers asynchronous peer-to-peer messages.

Specialists publish "PeerMessage" events to EventBridge; a rule routes them to
an SQS queue; this Lambda consumes the queue and drops each message into the
recipient's inbox on the blackboard (MSG#inbox-<agent>#<sender index>).
This decouples sender and receiver: agents never call each other directly.
"""
from __future__ import annotations

import json

from common import observability
from common.memory import get_store

logger = observability.init()


def handler(event, context=None):
    failures = []
    for record in event.get("Records", []):
        try:
            detail = json.loads(record["body"])["detail"]
            for recipient in detail["to"]:
                get_store().add_message(
                    detail["task_id"], f"inbox-{recipient}",
                    json.dumps({"from": detail["from"], "summary": detail["summary"]}),
                    int(detail["index"]))
        except Exception:  # noqa: BLE001
            logger.exception("could not deliver peer message %s", record.get("messageId"))
            failures.append({"itemIdentifier": record["messageId"]})
    return {"batchItemFailures": failures}
