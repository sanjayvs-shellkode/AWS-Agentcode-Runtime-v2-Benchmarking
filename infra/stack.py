import os
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

# Model configuration - set MODEL_PROFILE_ARN environment variable or update this default.
# This should be a Bedrock application inference profile ARN for Claude Haiku 4.5.
# Example: arn:aws:bedrock:REGION:ACCOUNT:application-inference-profile/PROFILE_ID
MODEL_PROFILE_ARN = os.environ.get("MODEL_PROFILE_ARN", "")

# Required tags for every resource. Update these for your environment.
REQUIRED_TAGS = {
    "createdby": os.environ.get("CREATED_BY", "benchmark-user"),
    "purpose": os.environ.get("PURPOSE_TAG", "agentcore-runtime-benchmark"),
}


class WeatherBenchStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # Tool host: one Lambda serving geocode_city, get_forecast and convert_temperature.
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

        # Short-term memory: the agent's conversation history per session and user. No long-term strategies.
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

        def prop(kind: str, description: str | None = None) -> agentcore.CfnGatewayTarget.SchemaDefinitionProperty:
            return agentcore.CfnGatewayTarget.SchemaDefinitionProperty(type=kind, description=description)

        def tool(name: str, description: str, props: dict) -> agentcore.CfnGatewayTarget.ToolDefinitionProperty:
            # Every argument is required.
            return agentcore.CfnGatewayTarget.ToolDefinitionProperty(
                name=name,
                description=description,
                input_schema=agentcore.CfnGatewayTarget.SchemaDefinitionProperty(
                    type="object", properties=props, required=list(props),
                ),
            )

        unit = "celsius or fahrenheit"
        weather_tools = [
            tool("geocode_city", "Find the latitude and longitude of a city by name.",
                 {"city": prop("string", "City name, for example Paris")}),
            tool("get_forecast",
                 "Get current weather at a location: temperature in Celsius, relative humidity in %, "
                 "wind speed in km/h, sky conditions and the local time.",
                 {"latitude": prop("number"), "longitude": prop("number")}),
            tool("convert_temperature", "Convert a temperature between Celsius and Fahrenheit.",
                 {"value": prop("number"), "from_unit": prop("string", unit), "to_unit": prop("string", unit)}),
        ]

        gateway_target = agentcore.CfnGatewayTarget(
            self, "WeatherTarget",
            gateway_identifier=gateway.attr_gateway_identifier,
            name="weather",
            target_configuration=agentcore.CfnGatewayTarget.TargetConfigurationProperty(
                mcp=agentcore.CfnGatewayTarget.McpTargetConfigurationProperty(
                    lambda_=agentcore.CfnGatewayTarget.McpLambdaTargetConfigurationProperty(
                        lambda_arn=tool_fn.function_arn,
                        tool_schema=agentcore.CfnGatewayTarget.ToolSchemaProperty(inline_payload=weather_tools),
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
            # What the Strands Memory session manager calls to load and save conversation turns.
            actions=[
                "bedrock-agentcore:CreateEvent",
                "bedrock-agentcore:ListEvents",
                "bedrock-agentcore:GetEvent",
                "bedrock-agentcore:DeleteEvent",
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
            # V2 warmup hook DISABLED for A/B testing
            hooks = None
            
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
                environment_variables={
                    "GATEWAY_URL": gateway.attr_gateway_url,
                    "MODEL_ID": MODEL_PROFILE_ARN,
                    "MEMORY_ID": memory.attr_memory_id,
                    "WARMUP_ENABLED": "true",  # Force CDK to update the runtime
                },
                hooks_configuration=hooks,
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
