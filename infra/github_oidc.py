"""Lets GitHub Actions deploy this project to AWS without storing access keys.

GitHub's OIDC token is exchanged for short-lived credentials of the role below,
and only workflows from the configured repository can assume it. The role can
assume the CDK bootstrap roles (that is how `cdk deploy` works) and read what
the post-deploy smoke tests need.

Deploy once from CloudShell:  cdk deploy GitHubDeployRoleStack
Then save the RoleArn output as the GitHub secret AWS_DEPLOY_ROLE_ARN.
"""
from aws_cdk import CfnOutput, Duration, Stack, aws_iam as iam
from constructs import Construct


class GitHubDeployRoleStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, repo: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)
        provider = iam.OpenIdConnectProvider(
            self, "GitHubOidc", url="https://token.actions.githubusercontent.com",
            client_ids=["sts.amazonaws.com"])

        role = iam.Role(
            self, "DeployRole", role_name="multi-agent-github-deploy",
            max_session_duration=Duration.hours(1),
            description=f"Assumed by GitHub Actions in {repo} to deploy with CDK",
            assumed_by=iam.WebIdentityPrincipal(provider.open_id_connect_provider_arn, conditions={
                "StringEquals": {"token.actions.githubusercontent.com:aud": "sts.amazonaws.com"},
                "StringLike": {"token.actions.githubusercontent.com:sub": f"repo:{repo}:*"}}))

        # cdk deploy works by assuming the bootstrap roles it created in this account
        role.add_to_policy(iam.PolicyStatement(
            actions=["sts:AssumeRole"],
            resources=[f"arn:aws:iam::{self.account}:role/cdk-*"]))
        # post-deploy smoke scenarios: read stack outputs, submit tasks, read results
        role.add_to_policy(iam.PolicyStatement(
            actions=["cloudformation:DescribeStacks", "sqs:SendMessage", "dynamodb:GetItem",
                     "s3:GetObject"],
            resources=["*"]))

        CfnOutput(self, "RoleArn", value=role.role_arn,
                  description="Save as the GitHub secret AWS_DEPLOY_ROLE_ARN")
