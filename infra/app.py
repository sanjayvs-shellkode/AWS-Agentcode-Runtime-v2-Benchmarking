import os

import aws_cdk as cdk

from stack import REQUIRED_TAGS, WeatherBenchStack

# Use environment variables for account/region, with defaults for the benchmark setup.
# Set AWS_ACCOUNT and AWS_REGION environment variables to deploy to a different account.
BENCH_ACCOUNT = os.environ.get("CDK_DEFAULT_ACCOUNT") or os.environ.get("AWS_ACCOUNT")
BENCH_REGION = os.environ.get("CDK_DEFAULT_REGION") or os.environ.get("AWS_REGION", "ap-south-1")

app = cdk.App()
stack = WeatherBenchStack(
    app,
    "wxbench-weather",
    env=cdk.Environment(account=BENCH_ACCOUNT, region=BENCH_REGION),
)
# Aspect covers taggable CDK-managed resources (roles, Lambda, log groups). The explicit
# tags= on the AgentCore resources cover the ones the aspect misses.
for key, value in REQUIRED_TAGS.items():
    cdk.Tags.of(stack).add(key, value)
app.synth()
