# AgentCore Runtime V1 vs V2 benchmark

One Strands weather agent, deployed twice on Amazon Bedrock AgentCore Runtime: once on platform version V1
and once on V2, with the same code package and settings. The benchmark compares cold starts, warm calls,
concurrent cold starts, memory, CPU and cost per task.

```
client -> AgentCore Runtime (V1 or V2) -> Strands agent (Claude Haiku 4.5)
                                            |-> AgentCore Memory (conversation history per session)
                                            '-> AgentCore Gateway (MCP) -> Lambda -> Open-Meteo
traces and logs -> CloudWatch (AgentCore Observability)
```

## Layout

| Path | What it is |
|---|---|
| `agent/` | The agent: `entrypoint.py` (served by Runtime) and `gateway_client.py` (SigV4-signed MCP client) |
| `lambda/` | The Gateway's Lambda target: `geocode_city`, `get_forecast`, `convert_temperature` |
| `infra/` | CDK app (`stack.py`) and `build.py`, which packages the Lambda (x86_64) and the agent zip (ARM64) |
| `bench/` | `smoke.py` (quick checks), `questions.py` (ten multi-city questions and the HTML report), `run.py`, `usage.py` |
| `bench/results/` | Raw CSVs and reports from past runs |

## Setup

Python 3.11+, Node.js 20+, `uv`, and the CDK CLI (`npm install -g aws-cdk`).

```powershell
python -m pip install -r agent/requirements.txt -r lambda/requirements.txt -r infra/requirements.txt pytest httpx
python -m pytest agent lambda bench
$env:AWS_PROFILE = "<>"   # account and region are pinned in infra/app.py
$env:AWS_REGION = "<>"
```

## Deploy

```powershell
python infra/build.py
cd infra; cdk deploy; cd ..
python bench/smoke.py wait      # V2 prepares its snapshot, so it takes a few minutes to reach READY
python bench/smoke.py gateway   # lists the three tools
python bench/smoke.py invoke v1
python bench/smoke.py invoke v2
```

## Benchmark

```powershell
python bench/questions.py   # ten multi-city questions; each answer uses all three tools
```

Each run writes `bench/results/<run>.csv` and `<run>.html`. Memory, CPU and cost come from billed usage,
which settles about 30 minutes after the last call. Rerun the report after that:

```powershell
python bench/questions.py --analyze bench/results/<run>.csv
```

The cost rates are in `bench/usage.py`. They come from the AWS pricing page examples (US East); set your
Region's rates before quoting costs.

## Clean up

```powershell
cd infra; cdk destroy
```

This leaves the `CDKToolkit` bootstrap stack and the runtimes' CloudWatch log groups.
