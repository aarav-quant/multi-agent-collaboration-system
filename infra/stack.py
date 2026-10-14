"""All AWS resources for the multi-agent collaboration system.

  client --> EventBridge (TaskSubmitted) --+
  client --> SQS task queue <--------------+--> intake Lambda --> Step Functions "live" alias
                 +--> DLQ (alarm)

  Step Functions (agent graph):
    Planner -> Map(researcher | coder | analyst) -> Map(peer debate) -> Writer <-> Critic/judge
            -> Finalize -> S3 / SQS / CloudWatch / EventBridge steps
  Specialists --PeerMessage--> EventBridge -> SQS -> mailbox Lambda -> DynamoDB inboxes

  Bedrock = LLM per agent (via LangGraph in each Lambda + one direct Step Functions call)
  DynamoDB = blackboard (shared state, agent memory, token usage)
  S3 = notes, code, debate, critiques, drafts, final report, run summary
  CloudWatch + X-Ray = logs, dashboard, alarms, custom metrics, traces
"""
import shutil
import subprocess
import sys
from pathlib import Path

import jsii
from aws_cdk import (BundlingOptions, CfnDeletionPolicy, CfnOutput, Duration, ILocalBundling, RemovalPolicy,
                     Stack, aws_cloudwatch as cw, aws_dynamodb as ddb, aws_events as events,
                     aws_events_targets as targets, aws_iam as iam, aws_lambda as _lambda,
                     aws_lambda_event_sources as sources, aws_logs as logs, aws_s3 as s3,
                     aws_sqs as sqs, aws_stepfunctions as sfn,
                     aws_stepfunctions_tasks as tasks, aws_apigatewayv2 as apigw,
                     aws_apigatewayv2_integrations as apigw_int)
from constructs import Construct

SRC = str(Path(__file__).resolve().parents[1] / "src")


@jsii.implements(ILocalBundling)
class PipLocalBundling:
    """Build the Lambda package without Docker (works in AWS CloudShell).

    pip downloads Linux x86_64 / Python 3.12 wheels matching the Lambda runtime,
    whatever machine you deploy from. If that fails, CDK falls back to Docker.
    """

    def try_bundle(self, output_dir, *args, **kwargs) -> bool:
        cmd = [sys.executable, "-m", "pip", "install", "--quiet", "--no-cache-dir",
               "-r", str(Path(SRC) / "requirements.txt"), "-t", output_dir,
               "--platform", "manylinux2014_x86_64", "--implementation", "cp",
               "--python-version", "3.12", "--only-binary=:all:", "--upgrade"]
        try:
            subprocess.run(cmd, check=True)
        except (subprocess.CalledProcessError, FileNotFoundError):
            print("local pip bundling failed, falling back to Docker", file=sys.stderr)
            return False
        shutil.copytree(SRC, output_dir, dirs_exist_ok=True,
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        return True


class MultiAgentStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)
        ctx = self.node.try_get_context
        model_id = ctx("model_id") or "apac.amazon.nova-lite-v1:0"
        max_revisions = int(ctx("max_revisions") or 2)
        max_subtasks = str(ctx("max_subtasks") or 4)
        token_budget = str(ctx("token_budget") or 60000)
        prompt_version = str(ctx("prompt_version") or "v1")
        shadow_prompt_version = str(ctx("shadow_prompt_version") or "")
        git_sha = str(ctx("git_sha") or "local")

        # ---------- Storage: shared state (DynamoDB) + documents (S3) ----------
        # DESTROY policies suit a dev/demo stack; switch to RETAIN for production.
        table = ddb.Table(
            self, "StateTable",
            partition_key=ddb.Attribute(name="pk", type=ddb.AttributeType.STRING),
            sort_key=ddb.Attribute(name="sk", type=ddb.AttributeType.STRING),
            billing_mode=ddb.BillingMode.PAY_PER_REQUEST,
            time_to_live_attribute="expires_at",
            removal_policy=RemovalPolicy.DESTROY)

        bucket = s3.Bucket(
            self, "ArtifactBucket",
            encryption=s3.BucketEncryption.S3_MANAGED,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            enforce_ssl=True,
            lifecycle_rules=[s3.LifecycleRule(expiration=Duration.days(90))],
            removal_policy=RemovalPolicy.DESTROY, auto_delete_objects=True)

        # ---------- Async messaging: SQS (+DLQ) and EventBridge ----------
        dlq = sqs.Queue(self, "TaskDLQ", retention_period=Duration.days(14),
                        enforce_ssl=True)
        task_queue = sqs.Queue(
            self, "TaskQueue", visibility_timeout=Duration.seconds(180),
            enforce_ssl=True,
            dead_letter_queue=sqs.DeadLetterQueue(queue=dlq, max_receive_count=3))

        bus = events.EventBus(self, "AgentBus", event_bus_name=f"{self.stack_name}-bus")
        # Producers can put "TaskSubmitted" events instead of talking to SQS directly.
        events.Rule(self, "SubmitTaskRule", event_bus=bus,
                    event_pattern=events.EventPattern(source=["mas.client"],
                                                      detail_type=["TaskSubmitted"]),
                    targets=[targets.SqsQueue(
                        task_queue, message=events.RuleTargetInput.from_event_path("$.detail"))])
        # Every agent lifecycle event is archived to CloudWatch Logs for debugging.
        events_log = logs.LogGroup(self, "AgentEventsLog",
                                   log_group_name=f"/aws/events/{self.stack_name}-agent-events",
                                   retention=logs.RetentionDays.ONE_WEEK,
                                   removal_policy=RemovalPolicy.DESTROY)
        events.Rule(self, "AgentLifecycleRule", event_bus=bus,
                    event_pattern=events.EventPattern(source=["mas.agents", "mas.workflow"]),
                    targets=[targets.CloudWatchLogGroup(events_log)])

        # ---------- Lambda functions (one code asset, one handler per agent) ----------
        # Dependencies are bundled with pip (no Docker needed); Docker is the fallback.
        # `-c skip_bundling=true` synthesizes without dependencies (template checks only).
        if str(ctx("skip_bundling")).lower() == "true":
            code = _lambda.Code.from_asset(SRC)
        else:
            code = _lambda.Code.from_asset(SRC, exclude=["**/__pycache__"], bundling=BundlingOptions(
                local=PipLocalBundling(),
                image=_lambda.Runtime.PYTHON_3_12.bundling_image,
                platform="linux/amd64",
                command=["bash", "-c",
                         "pip install --no-cache-dir -r requirements.txt -t /asset-output "
                         "&& cp -au . /asset-output"]))

        common_env = {
            "STATE_TABLE": table.table_name, "ARTIFACT_BUCKET": bucket.bucket_name,
            "EVENT_BUS_NAME": bus.event_bus_name, "MODEL_ID": model_id,
            "MAX_REVISIONS": str(max_revisions), "MAX_SUBTASKS": max_subtasks,
            "TOKEN_BUDGET": token_budget, "PROMPT_VERSION": prompt_version,
            "SHADOW_PROMPT_VERSION": shadow_prompt_version,
            "PRICE_INPUT_PER_1K": str(ctx("price_input_per_1k") or "0.00006"),
            "PRICE_OUTPUT_PER_1K": str(ctx("price_output_per_1k") or "0.00024"),
        }
        bedrock_invoke = iam.PolicyStatement(
            actions=["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"],
            resources=["arn:aws:bedrock:*::foundation-model/*",
                       f"arn:aws:bedrock:*:{self.account}:inference-profile/*"])

        def make_fn(name: str, handler: str, timeout_s: int, uses_llm: bool = True):
            log_group = logs.LogGroup(self, f"{name}Logs", retention=logs.RetentionDays.ONE_WEEK,
                                      removal_policy=RemovalPolicy.DESTROY)
            fn = _lambda.Function(
                self, name, runtime=_lambda.Runtime.PYTHON_3_12,
                architecture=_lambda.Architecture.X86_64,
                code=code, handler=handler, memory_size=1024,
                timeout=Duration.seconds(timeout_s), environment=common_env,
                tracing=_lambda.Tracing.ACTIVE,   # X-Ray
                log_group=log_group)
            table.grant_read_write_data(fn)
            bucket.grant_read_write(fn)
            bus.grant_put_events_to(fn)
            if uses_llm:
                fn.add_to_role_policy(bedrock_invoke)
            return fn

        planner_fn = make_fn("PlannerAgent", "handlers.planner.handler", 120)
        specialist_fn = make_fn("SpecialistAgent", "handlers.specialist.handler", 300)
        writer_fn = make_fn("WriterAgent", "handlers.writer.handler", 300)
        critic_fn = make_fn("CriticAgent", "handlers.critic.handler", 180)
        finalize_fn = make_fn("FinalizeTask", "handlers.finalize.handler", 30, uses_llm=False)
        failure_fn = make_fn("MarkTaskFailed", "handlers.finalize.failure_handler", 30, uses_llm=False)
        intake_fn = make_fn("TaskIntake", "handlers.intake.handler", 30, uses_llm=False)
        mailbox_fn = make_fn("AgentMailbox", "handlers.mailbox.handler", 30, uses_llm=False)
        agent_fns = [planner_fn, specialist_fn, writer_fn, critic_fn]

        # ---------- Peer-to-peer messaging: EventBridge -> SQS -> mailbox Lambda ----------
        peer_queue = sqs.Queue(self, "PeerMessageQueue", enforce_ssl=True,
                               visibility_timeout=Duration.seconds(60),
                               retention_period=Duration.days(1))
        events.Rule(self, "PeerMessageRule", event_bus=bus,
                    event_pattern=events.EventPattern(source=["mas.agents"],
                                                      detail_type=["PeerMessage"]),
                    targets=[targets.SqsQueue(peer_queue)])
        mailbox_fn.add_event_source(sources.SqsEventSource(
            peer_queue, batch_size=10, report_batch_item_failures=True))

        # ---------- Step Functions: orchestration + conditional routing ----------
        def invoke(step_id: str, fn: _lambda.IFunction) -> tasks.LambdaInvoke:
            step = tasks.LambdaInvoke(self, step_id, lambda_function=fn,
                                      payload_response_only=True)
            # Bedrock throttling -> RetryableAgentError -> exponential backoff
            step.add_retry(errors=["RetryableAgentError"], interval=Duration.seconds(10),
                           max_attempts=4, backoff_rate=2.0)
            return step

        mark_failed = tasks.LambdaInvoke(
            self, "MarkFailed", lambda_function=failure_fn, payload_response_only=True,
            payload=sfn.TaskInput.from_object({
                "task_id": sfn.JsonPath.string_at("$.task_id"),
                "error": sfn.JsonPath.object_at("$.error")}),
        ).next(sfn.Fail(self, "WorkflowFailed"))

        # Results queue: downstream consumers (e.g. a notifier) read finished tasks here.
        results_queue = sqs.Queue(self, "ResultsQueue", enforce_ssl=True,
                                  retention_period=Duration.days(4))
        DISCARD = sfn.JsonPath.DISCARD
        task_id = sfn.JsonPath.string_at("$.task_id")

        # --- Direct service integrations: each AWS service is its own step in the graph ---
        # DynamoDB: record that the workflow started (with its execution id)
        record_started = tasks.DynamoUpdateItem(
            self, "DynamoDB - Record task started", table=table,
            key={"pk": tasks.DynamoAttributeValue.from_string(task_id),
                 "sk": tasks.DynamoAttributeValue.from_string("META")},
            update_expression="SET #s = :s, execution_id = :e",
            expression_attribute_names={"#s": "status"},
            expression_attribute_values={
                ":s": tasks.DynamoAttributeValue.from_string("RUNNING"),
                ":e": tasks.DynamoAttributeValue.from_string(
                    sfn.JsonPath.string_at("$$.Execution.Id"))},
            result_path=DISCARD)

        def put_event(step_id: str, detail_type: str) -> tasks.EventBridgePutEvents:
            return tasks.EventBridgePutEvents(
                self, step_id, result_path=DISCARD,
                entries=[tasks.EventBridgePutEventsEntry(
                    event_bus=bus, source="mas.workflow", detail_type=detail_type,
                    detail=sfn.TaskInput.from_object({"task_id": task_id}))])

        # EventBridge: broadcast lifecycle events
        announce_started = put_event("EventBridge - Announce task started", "WorkflowStarted")
        announce_done = put_event("EventBridge - Announce task completed", "WorkflowCompleted")

        # Bedrock: the workflow calls the model directly to title the report
        geo_prefixes = ("us.", "eu.", "apac.", "global.", "jp.", "au.", "ca.", "us-gov.")
        model_arn = (f"arn:aws:bedrock:{self.region}:{self.account}:inference-profile/{model_id}"
                     if model_id.startswith(geo_prefixes)
                     else f"arn:aws:bedrock:{self.region}::foundation-model/{model_id}")
        generate_title = sfn.CustomState(self, "Bedrock - Generate report title", state_json={
            "Type": "Task",
            "Resource": f"arn:{self.partition}:states:::bedrock:invokeModel",
            "Parameters": {
                "ModelId": model_arn,
                "Body": {
                    "schemaVersion": "messages-v1",
                    "messages": [{"role": "user", "content": [{"text.$": (
                        "States.Format('Write a short title (max 8 words, no quotes) "
                        "for a report answering this request: {}', $.request)")}]}],
                    "inferenceConfig": {"max_new_tokens": 40, "temperature": 0.2}}},
            "ResultSelector": {"text.$": "$.Body.output.message.content[0].text"},
            "ResultPath": "$.title"})
        default_title = sfn.Pass(self, "Use default title", result=sfn.Result.from_object(
            {"text": "Multi-agent report"}), result_path="$.title")
        generate_title.add_catch(default_title, errors=["States.ALL"], result_path="$.title_error")

        # DynamoDB: store the title next to the task
        save_title = tasks.DynamoUpdateItem(
            self, "DynamoDB - Save report title", table=table,
            key={"pk": tasks.DynamoAttributeValue.from_string(task_id),
                 "sk": tasks.DynamoAttributeValue.from_string("META")},
            update_expression="SET title = :t",
            expression_attribute_values={
                ":t": tasks.DynamoAttributeValue.from_string(sfn.JsonPath.string_at("$.title.text"))},
            result_path=DISCARD)

        # S3: confirm the final report exists and read its size
        verify_report = tasks.CallAwsService(
            self, "S3 - Verify final report", service="s3", action="headObject",
            parameters={"Bucket": bucket.bucket_name, "Key": sfn.JsonPath.string_at("$.result_key")},
            iam_resources=[bucket.arn_for_objects("*")], iam_action="s3:GetObject",
            result_selector={"size_bytes.$": "$.ContentLength"}, result_path="$.report")

        # SQS: hand the finished task to downstream consumers
        notify_results = tasks.SqsSendMessage(
            self, "SQS - Send result notification", queue=results_queue, result_path=DISCARD,
            message_body=sfn.TaskInput.from_object({
                "task_id": task_id, "bucket": bucket.bucket_name,
                "result_key": sfn.JsonPath.string_at("$.result_key"),
                "size_bytes": sfn.JsonPath.number_at("$.report.size_bytes")}))

        # CloudWatch: custom business metric
        publish_metric = tasks.CallAwsService(
            self, "CloudWatch - Publish completion metric", service="cloudwatch",
            action="putMetricData", iam_resources=["*"], result_path=DISCARD,
            parameters={"Namespace": "MultiAgentSystem", "MetricData": [
                {"MetricName": "TasksCompleted", "Value": 1, "Unit": "Count"}]})

        # --- Lambda agents ---
        plan = invoke("Lambda - Planner agent (supervisor)", planner_fn)
        # Round 1: supervisor-worker fan-out (researcher / coder / analyst in parallel)
        run_specialists = sfn.Map(self, "Run specialist agents in parallel",
                                  items_path="$.subtasks", max_concurrency=4,
                                  result_path="$.specialist_results")
        run_specialists.item_processor(invoke("Lambda - Specialist agent", specialist_fn))
        # Round 2: peer-to-peer debate, each specialist critiques the others' notes
        debate = sfn.Map(self, "Peer debate between specialists",
                         items_path="$.subtasks", max_concurrency=4,
                         result_path="$.debate_results",
                         item_selector={
                             "task_id": sfn.JsonPath.string_at("$$.Map.Item.Value.task_id"),
                             "index": sfn.JsonPath.number_at("$$.Map.Item.Value.index"),
                             "agent": sfn.JsonPath.string_at("$$.Map.Item.Value.agent"),
                             "instruction": sfn.JsonPath.string_at("$$.Map.Item.Value.instruction"),
                             "mode": "debate"})
        debate.item_processor(invoke("Lambda - Debate round", specialist_fn))
        write = invoke("Lambda - Writer agent", writer_fn)
        judge = invoke("Lambda - Critic agent (judge)", critic_fn)
        finalize = invoke("Lambda - Finalize task", finalize_fn)

        for step in (record_started, announce_started, plan, save_title, run_specialists,
                     debate, write, judge, finalize, verify_report, notify_results,
                     publish_metric, announce_done):
            step.add_catch(mark_failed, result_path="$.error")

        default_title.next(save_title)
        finish = (finalize.next(verify_report).next(notify_results).next(publish_metric)
                  .next(announce_done).next(sfn.Succeed(self, "Done")))
        # Cyclic edge writer <-> critic. Loop guard: the critic stops at MAX_REVISIONS or
        # the token budget, and this Choice independently refuses to loop past the cap.
        revise_allowed = sfn.Condition.and_(
            sfn.Condition.string_equals("$.next_action", "revise"),
            sfn.Condition.number_less_than_equals("$.revision", max_revisions))
        definition = (record_started.next(announce_started).next(plan).next(generate_title)
                      .next(save_title).next(run_specialists).next(debate).next(write)
                      .next(judge).next(
            sfn.Choice(self, "Approved or revise?")
            .when(revise_allowed, write)
            .otherwise(finish)))

        sm_logs = logs.LogGroup(self, "WorkflowLogs", retention=logs.RetentionDays.ONE_WEEK,
                                removal_policy=RemovalPolicy.DESTROY)
        state_machine = sfn.StateMachine(
            self, "CollaborationWorkflow",
            definition_body=sfn.DefinitionBody.from_chainable(definition),
            timeout=Duration.minutes(30), tracing_enabled=True,
            logs=sfn.LogOptions(destination=sm_logs, level=sfn.LogLevel.ERROR))

        state_machine.add_to_role_policy(bedrock_invoke)  # for the direct Bedrock step

        # ---------- Versioned definitions: immutable version per deploy + "live" alias ----------
        # Every definition change publishes a new Step Functions version (old versions are
        # kept on update). Executions start through the alias, so rollback = point the
        # alias at an older version. The ASL itself is tracked in git (asl/ folder).
        version = sfn.CfnStateMachineVersion(
            self, "WorkflowVersion", state_machine_arn=state_machine.state_machine_arn,
            state_machine_revision_id=state_machine.state_machine_revision_id,
            description=f"git {git_sha}")
        version.cfn_options.update_replace_policy = CfnDeletionPolicy.RETAIN
        version.cfn_options.deletion_policy = CfnDeletionPolicy.RETAIN
        alias = sfn.CfnStateMachineAlias(
            self, "LiveAlias", name="live", description="Version that serves traffic",
            routing_configuration=[sfn.CfnStateMachineAlias.RoutingConfigurationVersionProperty(
                state_machine_version_arn=version.attr_arn, weight=100)])

        intake_fn.add_environment("STATE_MACHINE_ARN", alias.attr_arn)
        intake_fn.add_to_role_policy(iam.PolicyStatement(
            actions=["states:StartExecution"],
            resources=[alias.attr_arn, state_machine.state_machine_arn]))
        intake_fn.add_event_source(sources.SqsEventSource(
            task_queue, batch_size=5, report_batch_item_failures=True))

        # ---------- Website + JSON API (API Gateway HTTP API -> Lambda) ----------
        # One Lambda serves the single-page site at / and the API under /api, so the
        # page and API share an origin (no CORS). Submissions go through the task queue.
        api_fn = make_fn("WebApi", "handlers.api.handler", 15, uses_llm=False)
        api_fn.add_environment("TASK_QUEUE_URL", task_queue.queue_url)
        api_fn.add_environment("SITE_ACCESS_CODE", str(ctx("site_access_code") or ""))
        task_queue.grant_send_messages(api_fn)
        http_api = apigw.HttpApi(self, "WebsiteApi", api_name=f"{self.stack_name}-web",
                                 description="Website and API for the multi-agent system")
        integration = apigw_int.HttpLambdaIntegration("WebApiIntegration", api_fn)
        http_api.add_routes(path="/", methods=[apigw.HttpMethod.GET], integration=integration)
        http_api.add_routes(path="/api/{proxy+}", methods=[apigw.HttpMethod.GET, apigw.HttpMethod.POST],
                            integration=integration)
        # Throttle the public endpoint so nobody can run up the Bedrock bill
        stage = http_api.default_stage.node.default_child
        stage.default_route_settings = apigw.CfnStage.RouteSettingsProperty(
            throttling_rate_limit=2, throttling_burst_limit=5)

        # ---------- CloudWatch dashboard + alarms ----------
        dashboard = cw.Dashboard(self, "Dashboard", dashboard_name=f"{self.stack_name}-dashboard")
        dashboard.add_widgets(
            cw.GraphWidget(title="Workflow executions", width=12, left=[
                state_machine.metric_started(), state_machine.metric_succeeded(),
                state_machine.metric_failed()]),
            cw.GraphWidget(title="Task queue / DLQ", width=12, left=[
                task_queue.metric_approximate_number_of_messages_visible(),
                dlq.metric_approximate_number_of_messages_visible()]))
        dashboard.add_widgets(
            cw.GraphWidget(title="Agent errors", width=12,
                           left=[f.metric_errors() for f in agent_fns]),
            cw.GraphWidget(title="Agent duration p90", width=12,
                           left=[f.metric_duration(statistic="p90") for f in agent_fns]))
        dashboard.add_widgets(
            cw.SingleValueWidget(title="Tasks completed (custom metric)", width=12, metrics=[
                cw.Metric(namespace="MultiAgentSystem", metric_name="TasksCompleted",
                          statistic="Sum", period=Duration.days(1))]),
            cw.GraphWidget(title="Results queue / peer messages", width=12, left=[
                results_queue.metric_approximate_number_of_messages_visible(),
                peer_queue.metric_number_of_messages_sent()]))
        cw.Alarm(self, "DLQNotEmptyAlarm",
                 metric=dlq.metric_approximate_number_of_messages_visible(),
                 threshold=1, evaluation_periods=1,
                 comparison_operator=cw.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
                 alarm_description="Tasks could not be started (see DLQ)")
        cw.Alarm(self, "WorkflowFailedAlarm", metric=state_machine.metric_failed(),
                 threshold=1, evaluation_periods=1,
                 comparison_operator=cw.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
                 alarm_description="A multi-agent workflow execution failed")

        # ---------- Outputs (read by scripts/mas.py) ----------
        CfnOutput(self, "WebsiteUrl", value=f"{http_api.api_endpoint}/")
        CfnOutput(self, "TaskQueueUrl", value=task_queue.queue_url)
        CfnOutput(self, "ResultsQueueUrl", value=results_queue.queue_url)
        CfnOutput(self, "EventBusName", value=bus.event_bus_name)
        CfnOutput(self, "StateTableName", value=table.table_name)
        CfnOutput(self, "ArtifactBucketName", value=bucket.bucket_name)
        CfnOutput(self, "StateMachineArn", value=state_machine.state_machine_arn)
        CfnOutput(self, "LiveAliasArn", value=alias.attr_arn)
        CfnOutput(self, "PeerMessageQueueUrl", value=peer_queue.queue_url)
        CfnOutput(self, "PromptVersion", value=prompt_version)
        CfnOutput(self, "ShadowPromptVersion", value=shadow_prompt_version or "none")
        CfnOutput(self, "DashboardName", value=f"{self.stack_name}-dashboard")
