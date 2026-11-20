import aws_cdk as cdk

from stack import REQUIRED_TAGS, WeatherBenchStack

# Pinned so the stack can only deploy to the benchmark account.
BENCH_ACCOUNT = "106611079163"
BENCH_REGION = "ap-south-1"

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
