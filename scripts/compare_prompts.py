"""Compare a live task with its shadow run (staged prompt rollout).

Deploy with a shadow prompt set, e.g.
    cdk deploy -c shadow_prompt_version=v2
Every new task then also runs as "<task_id>-shadow" with the new prompts.
This script puts both runs side by side so you can decide whether to promote:
    cdk deploy -c prompt_version=v2 -c shadow_prompt_version=

  python scripts/compare_prompts.py <task_id> [<task_id> ...]
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from mas import STACK, get_task, outputs, read_s3, wait  # noqa: E402

FIELDS = ("prompt_version", "status", "stop_reason", "score", "revisions",
          "total_tokens", "est_cost_usd", "wall_clock_s")


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("task_ids", nargs="+")
    p.add_argument("--stack", default=STACK)
    p.add_argument("--show", action="store_true", help="print both final reports")
    args = p.parse_args()
    out = outputs(args.stack)

    totals = {"live": [0, 0.0, 0], "shadow": [0, 0.0, 0]}  # score sum, cost sum, count
    for task_id in args.task_ids:
        live = wait(out, task_id)
        if not get_task(out, f"{task_id}-shadow"):
            sys.exit(f"{task_id} has no shadow run (deploy with -c shadow_prompt_version=v2)")
        shadow = wait(out, f"{task_id}-shadow")
        print(f"\nTask {task_id}")
        print(f"{'':16}{'LIVE':>18}{'SHADOW':>18}")
        for f in FIELDS:
            print(f"{f:16}{str(live.get(f, '-')):>18}{str(shadow.get(f, '-')):>18}")
        for name, t in (("live", live), ("shadow", shadow)):
            totals[name][0] += float(t.get("score") or 0)
            totals[name][1] += float(t.get("est_cost_usd") or 0)
            totals[name][2] += 1
        if args.show:
            for name, t in (("LIVE", live), ("SHADOW", shadow)):
                print(f"\n===== {name} ({t.get('prompt_version')}) =====")
                print(read_s3(out, t["result_key"]))

    (ls, lc, n), (ss, sc, _) = totals["live"], totals["shadow"]
    print(f"\nAverage critic score: live {ls / n:.2f} vs shadow {ss / n:.2f}")
    print(f"Average cost:         live ${lc / n:.5f} vs shadow ${sc / n:.5f}")
    verdict = "PROMOTE shadow" if ss >= ls and sc <= lc * 1.2 else "KEEP live"
    print(f"Suggestion: {verdict} (shadow must score >= live and cost <= 120% of live)")


if __name__ == "__main__":
    main()
