#!/usr/bin/env python3
import os

import aws_cdk as cdk

from infra.github_oidc import GitHubDeployRoleStack
from infra.stack import MultiAgentStack

app = cdk.App()
# `-c agnostic=true` synthesizes without account/region so the exported ASL in git
# is identical on every machine (used by scripts/export_asl.py and CI).
agnostic = str(app.node.try_get_context("agnostic")).lower() == "true"
env = None if agnostic else cdk.Environment(account=os.getenv("CDK_DEFAULT_ACCOUNT"),
                                            region=os.getenv("CDK_DEFAULT_REGION"))

# The multi-agent system itself
MultiAgentStack(
    app, "MultiAgentCollaborationStack",
    description="Multi-agent collaboration system: Bedrock + Step Functions + Lambda + LangGraph",
    env=env,
)
# One-time: lets GitHub Actions deploy without stored AWS keys (CI/CD)
GitHubDeployRoleStack(
    app, "GitHubDeployRoleStack",
    repo=app.node.try_get_context("github_repo") or "aarav-quant/multi-agent-collaboration-system",
    description="OIDC role for GitHub Actions deployments of the multi-agent system",
    env=env,
)
cdk.Tags.of(app).add("project", "multi-agent-collaboration-system")
app.synth()
