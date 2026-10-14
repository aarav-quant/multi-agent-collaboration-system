"""Client for the deployed stack.

  python scripts/mas.py submit "Plan a migration from cron jobs to EventBridge"
  python scripts/mas.py submit --via eventbridge "..."
  python scripts/mas.py status <task_id>
  python scripts/mas.py result <task_id> --wait
  python scripts/mas.py summary <task_id>          # tokens, cost, latency, stop reason

Resource names are read from the CloudFormation stack outputs.
"""
from __future__ import annotations
import argparse
import json
import sys
import time
import uuid

import boto3

STACK = "MultiAgentCollaborationStack"


def outputs(stack: str) -> dict:
    desc = boto3.client("cloudformation").describe_stacks(StackName=stack)["Stacks"][0]
    return {o["OutputKey"]: o["OutputValue"] for o in desc.get("Outputs", [])}


def submit(out: dict, request: str, via: str = "sqs") -> str:
    task_id = uuid.uuid4().hex
    body = {"task_id": task_id, "request": request}
    if via == "sqs":
        boto3.client("sqs").send_message(QueueUrl=out["TaskQueueUrl"], MessageBody=json.dumps(body))
    else:
        resp = boto3.client("events").put_events(Entries=[{
            "Source": "mas.client", "DetailType": "TaskSubmitted",
            "Detail": json.dumps(body), "EventBusName": out["EventBusName"]}])
        if resp.get("FailedEntryCount"):
            sys.exit(f"EventBridge rejected the event: {resp['Entries']}")
    return task_id


def wait(out: dict, task_id: str, timeout: int = 900) -> dict:
    """Poll DynamoDB until the task is COMPLETED or FAILED."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        task = get_task(out, task_id)
        if task and task["status"] in ("COMPLETED", "FAILED"):
            return task
        time.sleep(5)
    raise TimeoutError(f"task {task_id} did not finish within {timeout}s")


def read_s3(out: dict, key: str) -> str:
    obj = boto3.client("s3").get_object(Bucket=out["ArtifactBucketName"], Key=key)
    return obj["Body"].read().decode("utf-8")


def get_task(out: dict, task_id: str) -> dict | None:
    table = boto3.resource("dynamodb").Table(out["StateTableName"])
    return table.get_item(Key={"pk": task_id, "sk": "META"}).get("Item")


SUMMARY_KEYS = ("status", "prompt_version", "stop_reason", "approved", "score", "revisions",
                "agent_calls", "input_tokens", "output_tokens", "total_tokens", "est_cost_usd",
                "wall_clock_s", "title")


def summary_line(task: dict) -> str:
    return " | ".join(f"{k}={task[k]}" for k in SUMMARY_KEYS if k in task)


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--stack", default=STACK)
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("submit"); s.add_argument("request")
    s.add_argument("--via", choices=["sqs", "eventbridge"], default="sqs")
    st = sub.add_parser("status"); st.add_argument("task_id")
    r = sub.add_parser("result"); r.add_argument("task_id")
    r.add_argument("--wait", action="store_true"); r.add_argument("--timeout", type=int, default=900)
    sm = sub.add_parser("summary"); sm.add_argument("task_id")
    args = p.parse_args()
    out = outputs(args.stack)

    if args.cmd == "submit":
        print(submit(out, args.request, args.via))
        return

    if args.cmd == "summary":
        task = get_task(out, args.task_id) or {}
        print(json.dumps({k: task[k] for k in SUMMARY_KEYS if k in task}, indent=2, default=str))
        return

    deadline = time.time() + getattr(args, "timeout", 0)
    while True:
        task = get_task(out, args.task_id)
        status = task["status"] if task else "NOT_FOUND (still queued?)"
        if args.cmd == "status" or not getattr(args, "wait", False) \
                or status in ("COMPLETED", "FAILED") or time.time() > deadline:
            break
        print(f"status: {status} ...", file=sys.stderr)
        time.sleep(5)

    if args.cmd == "status" or status != "COMPLETED":
        print(json.dumps(task or {"status": status}, indent=2, default=str))
        return
    obj = boto3.client("s3").get_object(Bucket=out["ArtifactBucketName"], Key=task["result_key"])
    print(obj["Body"].read().decode("utf-8"))
    print("\n--- run summary: " + summary_line(task), file=sys.stderr)


if __name__ == "__main__":
    main()
