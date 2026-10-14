"""Run the fixed CI scenarios against the DEPLOYED stack (real Bedrock).

Used after a deploy (CI smoke test) and for cost-guardrail checks with real
token counts. Only scenarios marked "live": true are run. Exits with code 1 if
any task fails, does not terminate in time, or exceeds its live_budget.

  python scripts/run_scenarios.py                 # all live scenarios
  python scripts/run_scenarios.py --only happy_path
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from mas import STACK, outputs, submit, wait  # noqa: E402

SCENARIOS = Path(__file__).resolve().parents[1] / "tests" / "scenarios"


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--stack", default=STACK)
    p.add_argument("--only", nargs="*", help="scenario names to run")
    args = p.parse_args()
    out = outputs(args.stack)

    scenarios = [json.loads(f.read_text()) for f in sorted(SCENARIOS.glob("*.json"))]
    scenarios = [s for s in scenarios if s["live"] and (not args.only or s["name"] in args.only)]
    started = {s["name"]: submit(out, s["request"]) for s in scenarios}
    print(f"submitted {len(started)} live scenario(s)")

    failures = []
    for sc in scenarios:
        name, budget = sc["name"], sc["live_budget"]
        try:
            task = wait(out, started[name], timeout=budget["max_wall_clock_s"] + 120)
        except TimeoutError as e:
            failures.append(f"{name}: did not terminate ({e})")
            continue
        tokens, cost = int(task.get("total_tokens", 0)), float(task.get("est_cost_usd", 0))
        print(f"{name:32} status={task['status']} stop={task.get('stop_reason')} "
              f"tokens={tokens} cost=${cost:.5f} time={task.get('wall_clock_s')}s")
        if task["status"] != "COMPLETED":
            failures.append(f"{name}: status {task['status']} ({task.get('error')})")
        if tokens > budget["max_total_tokens"]:
            failures.append(f"COST GUARDRAIL {name}: {tokens} tokens > {budget['max_total_tokens']}")
        if cost > budget["max_cost_usd"]:
            failures.append(f"COST GUARDRAIL {name}: ${cost} > ${budget['max_cost_usd']}")
        if int(task.get("wall_clock_s", 0)) > budget["max_wall_clock_s"]:
            failures.append(f"LATENCY {name}: {task.get('wall_clock_s')}s > {budget['max_wall_clock_s']}s")

    if failures:
        print("\n".join(["", "FAILED:"] + failures))
        sys.exit(1)
    print("\nall live scenarios passed")


if __name__ == "__main__":
    main()
