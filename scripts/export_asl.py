"""Export the Step Functions definition (Amazon States Language) to asl/ for git.

The state machine is defined in CDK (infra/stack.py). This script synthesizes
the stack and writes the resulting ASL, with CloudFormation references replaced
by readable placeholders such as ${PlannerAgent.Arn}, to
asl/collaboration_workflow.asl.json so every change is reviewed in git.

  python scripts/export_asl.py           # regenerate the file
  python scripts/export_asl.py --check   # CI: fail if the committed file is stale
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ASL_FILE = ROOT / "asl" / "collaboration_workflow.asl.json"
TEMPLATE = ROOT / "cdk.out" / "MultiAgentCollaborationStack.template.json"


def _placeholder(ref: dict, resources: dict) -> str:
    if "Ref" in ref:
        name = ref["Ref"]
    elif "Fn::GetAtt" in ref:
        name = ".".join(ref["Fn::GetAtt"])
    else:
        return "${" + json.dumps(ref, sort_keys=True) + "}"
    # Strip CDK's 8-char hash suffix from logical ids so placeholders stay stable/readable
    name = re.sub(r"[0-9A-F]{8}(?=\.|$)", "", name)
    return "${" + name + "}"


def synth() -> None:
    subprocess.run(["cdk", "synth", "-c", "skip_bundling=true", "-c", "agnostic=true", "-q"], cwd=ROOT, check=True,
                   stdout=subprocess.DEVNULL)


def extract_asl() -> dict:
    template = json.loads(TEMPLATE.read_text())
    resources = template["Resources"]
    machine = next(r for r in resources.values() if r["Type"] == "AWS::StepFunctions::StateMachine")
    definition = machine["Properties"]["DefinitionString"]
    if isinstance(definition, str):
        return json.loads(definition)
    parts = definition["Fn::Join"][1]
    text = "".join(p if isinstance(p, str) else _placeholder(p, resources) for p in parts)
    return json.loads(text)


def render() -> str:
    return json.dumps(extract_asl(), indent=2, sort_keys=False) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true", help="fail if asl/ is out of date")
    parser.add_argument("--no-synth", action="store_true", help="reuse the existing cdk.out")
    args = parser.parse_args()
    if not args.no_synth:
        synth()
    new = render()
    if args.check:
        old = ASL_FILE.read_text() if ASL_FILE.exists() else ""
        if old != new:
            sys.exit("ASL drift: the state machine changed but asl/collaboration_workflow.asl.json "
                     "was not updated. Run `python scripts/export_asl.py` and commit the result.")
        print("ASL definition is up to date")
        return
    ASL_FILE.parent.mkdir(exist_ok=True)
    ASL_FILE.write_text(new)
    print(f"wrote {ASL_FILE.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
