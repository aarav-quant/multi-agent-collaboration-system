"""Run the full multi-agent workflow on your machine (no AWS resources needed).

  python scripts/run_local.py "Compare SQS and Kinesis for an order pipeline"
  python scripts/run_local.py --fake "anything"   # no Bedrock calls at all

Without --fake it calls Amazon Bedrock with your local AWS credentials.
Outputs are written to ./local_output/tasks/<task_id>/.
"""
import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("request")
    parser.add_argument("--fake", action="store_true", help="use the offline fake model")
    parser.add_argument("--model-id", help="Bedrock model / inference profile id")
    args = parser.parse_args()

    os.environ["LOCAL_MODE"] = "1"
    os.environ.setdefault("LOCAL_OUTPUT_DIR", str(ROOT / "local_output"))
    if args.fake:
        os.environ["FAKE_LLM"] = "1"
    if args.model_id:
        os.environ["MODEL_ID"] = args.model_id

    from common.memory import get_store
    from common.storage import get_storage
    from handlers.intake import create_task
    from workflow.local_graph import build_workflow

    task_id = create_task(args.request)
    print(f"task {task_id}: planner -> specialists -> peer debate -> writer <-> critic")
    result = build_workflow().invoke({"task_id": task_id})

    task = get_store().get_task(task_id)
    for m in get_store().get_messages(task_id):
        print(f"  [{m['agent']} #{m['index']}] {m['content'][:80]!r}")
    print(f"\nstatus={task['status']} stop_reason={task.get('stop_reason')} "
          f"revisions={task.get('revisions')} score={task.get('score')}")
    print(f"tokens={task.get('total_tokens')} est_cost=${task.get('est_cost_usd')} "
          f"agent_calls={task.get('agent_calls')}")
    print(f"result: {get_storage().path(result['result_key'])}\n")
    print(get_storage().get_text(result["result_key"]))


if __name__ == "__main__":
    main()
