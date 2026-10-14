"""Lifecycle events on EventBridge (TaskStarted, AgentCompleted, TaskCompleted...).

Other systems subscribe to these asynchronously. Publishing is best-effort:
a failed event must never fail the agent workflow itself.
"""
from __future__ import annotations

import json
import logging

from common.config import get_config

SOURCE = "mas.agents"
logger = logging.getLogger(__name__)
LOCAL_EVENTS: list[dict] = []  # captured in local mode, inspected by tests

_client = None


def publish(detail_type: str, detail: dict) -> None:
    global _client
    cfg = get_config()
    if cfg.local_mode:
        LOCAL_EVENTS.append({"detail_type": detail_type, "detail": detail})
        logger.info("event %s %s", detail_type, json.dumps(detail, default=str))
        return
    try:
        if _client is None:
            import boto3
            _client = boto3.client("events")
        _client.put_events(Entries=[{
            "Source": SOURCE, "DetailType": detail_type,
            "Detail": json.dumps(detail, default=str),
            "EventBusName": cfg.event_bus_name}])
    except Exception:  # noqa: BLE001 - observability must not break the workflow
        logger.exception("failed to publish %s", detail_type)


def reset() -> None:
    LOCAL_EVENTS.clear()
