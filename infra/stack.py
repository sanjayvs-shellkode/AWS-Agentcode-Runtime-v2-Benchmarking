from pathlib import Path

from aws_cdk import (
    CfnOutput,
    Duration,
    RemovalPolicy,
    Stack,
)
from aws_cdk import aws_bedrockagentcore as agentcore
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as _lambda
from aws_cdk import aws_logs as logs
from aws_cdk import aws_s3_assets as s3_assets
from constructs import Construct

PREFIX = "wxbench"
BUILD = Path(__file__).resolve().parents[1] / "build"
AGENTCORE_PRINCIPAL = "bedrock-agentcore.amazonaws.com"
# Bedrock application inference profile "runtime-v1-v2-benchmark" (Claude Haiku 4.5).
MODEL_PROFILE_ARN = "arn:aws:bedrock:ap-south-1:106611079163:application-inference-profile/46u84t9z5klt"

# Required tags for every resource in this request. Keys and values must match exactly.
REQUIRED_TAGS = {
    "createdby": "sahana.a@shellkode.com",
    "purpose": "trying-new-approach-31054",
}


class WeatherBenchStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # Tool host: one Lambda serving geocode_city and get_forecast.
        tool_logs = logs.LogGroup(
            self, "ToolLogs",
            log_group_name=f"/aws/lambda/{PREFIX}-weather-tools",
            retention=logs.RetentionDays.ONE_WEEK,
            removal_policy=RemovalPolicy.DESTROY,
        )
        tool_fn = _lambda.Function(
            self, "WeatherTools",
            function_name=f"{PREFIX}-weather-tools",
            runtime=_lambda.Runtime.PYTHON_3_12,
            handler="handler.handler",
            code=_lambda.Code.from_asset(str(BUILD / "lambda")),
            timeout=Duration.seconds(15),
            memory_size=256,
            log_group=tool_logs,
        )

        # Gateway service role: lets the Gateway invoke the Lambda target.
        gateway_role = iam.Role(
            self, "GatewayRole",
            role_name=f"{PREFIX}-gateway-role",
            assumed_by=iam.ServicePrincipal(AGENTCORE_PRINCIPAL),
        )
        tool_fn.grant_invoke(gateway_role)

        # Short-term memory only, no long-term strategies.
        memory = agentcore.CfnMemory(
            self, "Memory",
            name=f"{PREFIX}_memory",
            event_expiry_duration=7,
            description="Short-term events for the weather benchmark agent",
            tags=REQUIRED_TAGS,
        )

        gateway = agentcore.CfnGateway(
            self, "Gateway",
            name=f"{PREFIX}-gateway",
            role_arn=gateway_role.role_arn,
            authorizer_type="AWS_IAM",
            protocol_type="MCP",
            tags=REQUIRED_TAGS,
        )

        def tool(name: str, description: str, props: dict, required: list[str]) -> agentcore.CfnGatewayTarget.ToolDefinitionProperty:
            return agentcore.CfnGatewayTarget.ToolDefinitionProperty(
                name=name,
                description=description,
                input_schema=agentcore.CfnGatewayTarget.SchemaDefinitionProperty(
                    type="object", properties=props, required=required,
                ),
            )

        gateway_target = agentcore.CfnGatewayTarget(
            self, "WeatherTarget",
            gateway_identifier=gateway.attr_gateway_identifier,
            name="weather",
            target_configuration=agentcore.CfnGatewayTarget.TargetConfigurationProperty(
                mcp=agentcore.CfnGatewayTarget.McpTargetConfigurationProperty(
                    lambda_=agentcore.CfnGatewayTarget.McpLambdaTargetConfigurationProperty(
                        lambda_arn=tool_fn.function_arn,
                        tool_schema=agentcore.CfnGatewayTarget.ToolSchemaProperty(
                            inline_payload=[
                                tool(
                                    "geocode_city",
                                    "Find the latitude and longitude of a city.",
                                    {"city": agentcore.CfnGatewayTarget.SchemaDefinitionProperty(type="string", description="City name")},
                                    ["city"],
                                ),
                                tool(
                                    "get_forecast",
                                    "Get current weather for a latitude and longitude.",
                                    {
                                        "latitude": agentcore.CfnGatewayTarget.SchemaDefinitionProperty(type="number"),
                                        "longitude": agentcore.CfnGatewayTarget.SchemaDefinitionProperty(type="number"),
                                    },
                                    ["latitude", "longitude"],
                                ),
                            ]
                        ),
                    )
                )
            ),
            credential_provider_configurations=[
                agentcore.CfnGatewayTarget.CredentialProviderConfigurationProperty(
                    credential_provider_type="GATEWAY_IAM_ROLE",
                )
            ],
        )

        # Agent package, uploaded to the CDK staging bucket.
        package = s3_assets.Asset(self, "AgentPackage", path=str(BUILD / "agent-package.zip"))

        runtime_role = iam.Role(
            self, "RuntimeRole",
            role_name=f"{PREFIX}-runtime-role",
            assumed_by=iam.ServicePrincipal(AGENTCORE_PRINCIPAL),
        )
        package.grant_read(runtime_role)
        # Application inference profile for Claude Haiku 4.5. The profile can route to any
        # region (its model list has a region-less ARN), so the foundation-model ARN uses "*".
        runtime_role.add_to_policy(iam.PolicyStatement(
            actions=["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"],
            resources=[
                MODEL_PROFILE_ARN,
                "arn:aws:bedrock:*::foundation-model/anthropic.claude-haiku-4-5-20251001-v1:0",
            ],
        ))
        runtime_role.add_to_policy(iam.PolicyStatement(
            actions=[
                "bedrock-agentcore:CreateEvent",
                "bedrock-agentcore:ListEvents",
                "bedrock-agentcore:GetEvent",
                "bedrock-agentcore:ListSessions",
            ],
            resources=[memory.attr_memory_arn],
        ))
        runtime_role.add_to_policy(iam.PolicyStatement(
            actions=["bedrock-agentcore:InvokeGateway"],
            resources=[f"arn:aws:bedrock-agentcore:{self.region}:{self.account}:gateway/{gateway.attr_gateway_identifier}"],
        ))
        # AgentCore uses this to let X-Ray write spans into the runtime's own log group.
        runtime_role.add_to_policy(iam.PolicyStatement(
            actions=["logs:PutResourcePolicy"],
            resources=[f"arn:aws:logs:{self.region}:{self.account}:log-group:/aws/bedrock-agentcore/runtimes/{PREFIX}_*"],
        ))
        runtime_role.add_to_policy(iam.PolicyStatement(
            actions=[
                "logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents",
                "logs:DescribeLogGroups", "logs:DescribeLogStreams",
                "xray:PutTraceSegments", "xray:PutTelemetryRecords",
                "xray:GetSamplingRules", "xray:GetSamplingTargets",
            ],
            resources=["*"],
        ))

        def runtime(platform: str) -> agentcore.CfnRuntime:
            return agentcore.CfnRuntime(
                self, f"Runtime{platform}",
                agent_runtime_name=f"{PREFIX}_weather_{platform.lower()}",
                role_arn=runtime_role.role_arn,
                platform_version=platform,
                network_configuration=agentcore.CfnRuntime.NetworkConfigurationProperty(network_mode="PUBLIC"),
                agent_runtime_artifact=agentcore.CfnRuntime.AgentRuntimeArtifactProperty(
                    code_configuration=agentcore.CfnRuntime.CodeConfigurationProperty(
                        code=agentcore.CfnRuntime.CodeProperty(
                            s3=agentcore.CfnRuntime.S3LocationProperty(
                                bucket=package.s3_bucket_name,
                                prefix=package.s3_object_key,
                            )
                        ),
                        # The wrapper turns on ADOT tracing; build.py writes its Linux launcher.
                        entry_point=["opentelemetry-instrument", "entrypoint.py"],
                        runtime="PYTHON_3_12",
                    )
                ),
                environment_variables={"GATEWAY_URL": gateway.attr_gateway_url, "MODEL_ID": MODEL_PROFILE_ARN},
                tags=REQUIRED_TAGS,
            )

        runtime_v1 = runtime("V1")
        runtime_v2 = runtime("V2")

        # IAM policies apply with a delay. Without these dependencies, the runtimes were created
        # about one second after their role policy and got AccessDenied on CreateLogGroup.
        runtime_policy = runtime_role.node.find_child("DefaultPolicy")
        runtime_v1.node.add_dependency(runtime_policy)
        runtime_v2.node.add_dependency(runtime_policy)
        gateway_target.node.add_dependency(gateway_role.node.find_child("DefaultPolicy"))

        CfnOutput(self, "RuntimeV1Arn", value=runtime_v1.attr_agent_runtime_arn)
        CfnOutput(self, "RuntimeV2Arn", value=runtime_v2.attr_agent_runtime_arn)
        CfnOutput(self, "GatewayUrl", value=gateway.attr_gateway_url)
        CfnOutput(self, "MemoryArn", value=memory.attr_memory_arn)
