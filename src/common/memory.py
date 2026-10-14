"""Shared state and agent memory (the "blackboard" all agents read and write).

DynamoDB single-table layout, partition key = task_id:
  sk = "META"                      -> request, status, prompt version, usage summary, result
  sk = "MSG#<agent>#<index:02d>"   -> one agent's output (plan, notes, debate, critique, inbox)
  sk = "USAGE#<agent>#<index:02d>" -> tokens + latency of one agent call (cost tracking)

Who reads / writes what (the blackboard contract):
  planner     writes MSG#planner             reads META
  researcher  writes MSG#researcher#i        reads META
  coder       writes MSG#coder#i             reads META
  analyst     writes MSG#analyst#i           reads META
  <x>-debate  writes MSG#<x>-debate#i        reads peers' MSG#<specialist>#j
  mailbox     writes MSG#inbox-<to>#i        (async peer messages via EventBridge -> SQS)
  writer      writes S3 drafts               reads specialist + debate messages
  critic      writes MSG#critic#rev          reads S3 draft + debate messages
  everyone    writes USAGE#...               finalize sums them into META

Sort keys are deterministic, so a retried Lambda overwrites its own item
instead of creating duplicates (idempotent retries).
"""
from __future__ import annotations

import time
from decimal import Decimal
from typing import Any

from common.config import get_config

TTL_SECONDS = 30 * 24 * 3600  # DynamoDB TTL deletes task data after 30 days


def _now() -> int:
    return int(time.time())


def _to_dynamo(value):
    """DynamoDB rejects Python floats; store them as Decimal."""
    if isinstance(value, float):
        return Decimal(str(value))
    if isinstance(value, dict):
        return {k: _to_dynamo(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_to_dynamo(v) for v in value]
    return value


def _from_dynamo(value):
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, dict):
        return {k: _from_dynamo(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_from_dynamo(v) for v in value]
    return value


def _usage_item(agent, index, input_tokens, output_tokens, latency_ms):
    return {"agent": agent, "index": index, "input_tokens": int(input_tokens),
            "output_tokens": int(output_tokens), "latency_ms": int(latency_ms)}


class DynamoStore:
    def __init__(self, table_name: str):
        import boto3
        self.table = boto3.resource("dynamodb").Table(table_name)

    def put_task(self, task_id: str, request: str, prompt_version: str = "v1",
                 shadow_of: str | None = None) -> bool:
        """Create the task record. Returns False if it already existed."""
        from botocore.exceptions import ClientError
        item = {"pk": task_id, "sk": "META", "request": request, "status": "QUEUED",
                "prompt_version": prompt_version, "created_at": _now(),
                "updated_at": _now(), "expires_at": _now() + TTL_SECONDS}
        if shadow_of:
            item["shadow_of"] = shadow_of
        try:
            self.table.put_item(Item=item, ConditionExpression="attribute_not_exists(pk)")
            return True
        except ClientError as e:
            if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
                return False
            raise

    def get_task(self, task_id: str) -> dict:
        item = self.table.get_item(Key={"pk": task_id, "sk": "META"}).get("Item")
        if not item:
            raise KeyError(f"task {task_id} not found")
        return _from_dynamo(item)

    def update_status(self, task_id: str, status: str, **attrs: Any) -> None:
        attrs = _to_dynamo({"status": status, "updated_at": _now(), **attrs})
        names = {f"#a{i}": k for i, k in enumerate(attrs)}
        values = {f":v{i}": v for i, v in enumerate(attrs.values())}
        expr = "SET " + ", ".join(f"#a{i} = :v{i}" for i in range(len(attrs)))
        self.table.update_item(Key={"pk": task_id, "sk": "META"},
                               UpdateExpression=expr,
                               ExpressionAttributeNames=names,
                               ExpressionAttributeValues=values)

    def add_message(self, task_id: str, agent: str, content: str, index: int = 0) -> str:
        sk = f"MSG#{agent}#{index:02d}"
        self.table.put_item(Item={"pk": task_id, "sk": sk, "agent": agent,
                                  "index": index, "content": content,
                                  "created_at": _now(),
                                  "expires_at": _now() + TTL_SECONDS})
        return sk

    def record_usage(self, task_id, agent, index, input_tokens, output_tokens, latency_ms):
        item = _usage_item(agent, index, input_tokens, output_tokens, latency_ms)
        self.table.put_item(Item={"pk": task_id, "sk": f"USAGE#{agent}#{index:02d}",
                                  **item, "expires_at": _now() + TTL_SECONDS})

    def _query_prefix(self, task_id: str, prefix: str) -> list[dict]:
        from boto3.dynamodb.conditions import Key
        items, kwargs = [], {"KeyConditionExpression":
                             Key("pk").eq(task_id) & Key("sk").begins_with(prefix)}
        while True:  # paginate in case a task accumulates many items
            page = self.table.query(**kwargs)
            items += page["Items"]
            if "LastEvaluatedKey" not in page:
                break
            kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]
        return [_from_dynamo(i) for i in items]

    def get_messages(self, task_id: str) -> list[dict]:
        return self._query_prefix(task_id, "MSG#")

    def get_usage(self, task_id: str) -> list[dict]:
        return self._query_prefix(task_id, "USAGE#")


class InMemoryStore:
    """Same interface, plain dicts. Used for local runs and tests."""

    def __init__(self):
        self.items: dict[str, dict[str, dict]] = {}

    def put_task(self, task_id, request, prompt_version="v1", shadow_of=None):
        if task_id in self.items and "META" in self.items[task_id]:
            return False
        meta = {"pk": task_id, "sk": "META", "request": request, "status": "QUEUED",
                "prompt_version": prompt_version, "created_at": _now(), "updated_at": _now()}
        if shadow_of:
            meta["shadow_of"] = shadow_of
        self.items.setdefault(task_id, {})["META"] = meta
        return True

    def get_task(self, task_id):
        try:
            return dict(self.items[task_id]["META"])
        except KeyError:
            raise KeyError(f"task {task_id} not found") from None

    def update_status(self, task_id, status, **attrs):
        self.items[task_id]["META"].update(status=status, updated_at=_now(), **attrs)

    def add_message(self, task_id, agent, content, index=0):
        sk = f"MSG#{agent}#{index:02d}"
        self.items.setdefault(task_id, {})[sk] = {
            "pk": task_id, "sk": sk, "agent": agent, "index": index,
            "content": content, "created_at": _now()}
        return sk

    def record_usage(self, task_id, agent, index, input_tokens, output_tokens, latency_ms):
        self.items.setdefault(task_id, {})[f"USAGE#{agent}#{index:02d}"] = _usage_item(
            agent, index, input_tokens, output_tokens, latency_ms)

    def get_messages(self, task_id):
        rows = self.items.get(task_id, {})
        return [dict(rows[k]) for k in sorted(rows) if k.startswith("MSG#")]

    def get_usage(self, task_id):
        rows = self.items.get(task_id, {})
        return [dict(rows[k]) for k in sorted(rows) if k.startswith("USAGE#")]


def usage_totals(rows: list[dict]) -> dict:
    tin = sum(r["input_tokens"] for r in rows)
    tout = sum(r["output_tokens"] for r in rows)
    return {"input_tokens": tin, "output_tokens": tout, "total_tokens": tin + tout,
            "agent_calls": len(rows),
            "agent_latency_ms": sum(r["latency_ms"] for r in rows)}


_store = None


def get_store():
    global _store
    if _store is None:
        cfg = get_config()
        _store = InMemoryStore() if cfg.local_mode else DynamoStore(cfg.table_name)
    return _store


def reset() -> None:
    global _store
    _store = None
