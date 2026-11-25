# AWS Bedrock AgentCore Runtime V1 vs V2 Benchmark

A comprehensive benchmark comparing Amazon Bedrock AgentCore Runtime V1 vs V2, measuring cold starts, warm calls, concurrent performance, and the impact of V2's warmup hooks optimization.

## Architecture

```
client -> AgentCore Runtime (V1 or V2) -> Strands agent (Claude Sonnet 4)
                                            |-> AgentCore Memory (conversation history per session)
                                            '-> AgentCore Gateway (MCP) -> Lambda -> Open-Meteo
traces and logs -> CloudWatch (AgentCore Observability)
```

## Project Structure

```
├── backend/                    # Agent runtime code
│   ├── entrypoint.py          # Main agent entry point (served by Runtime)
│   ├── gateway_client.py      # SigV4-signed MCP client for Gateway
│   ├── requirements.txt       # Agent dependencies
│   └── lambda/                # Gateway Lambda target
│       ├── handler.py         # Lambda handler
│       └── weather.py         # Weather tools (geocode, forecast, convert)
├── docs/                      # Documentation and reports
│   ├── images/                # Benchmark charts and diagrams
│   └── results/               # Benchmark CSV results and HTML reports
├── infra/                     # Infrastructure as Code (CDK)
│   ├── app.py                 # CDK app entry point
│   ├── stack.py               # AgentCore stack definition
│   └── build.py               # Package builder for Lambda and agent
├── scripts/                   # Benchmark and utility scripts
│   ├── run.py                 # Main benchmark runner
│   ├── smoke.py               # Quick health checks
│   ├── questions.py           # Multi-city question benchmark
│   ├── usage.py               # Cost calculation utilities
│   ├── update_with_hooks.py   # Enable/update warmup hooks
│   └── check_status.py        # Runtime status checker
├── .env.example               # Environment variables template
├── pyproject.toml             # Project configuration
├── requirements-dev.txt       # Development dependencies
└── README.md
```

## Key Results

| Metric | V1 | V2 (no warmup) | V2 (with warmup) |
|--------|----:|---------------:|------------------:|
| Cold Start Overhead | 4,918ms | 1,626ms | 1,413ms |
| Improvement vs V1 | - | 67% faster | 71% faster |
| 50 Concurrent Calls | 25,261ms | 1,832ms | - |

**Warmup optimization saves ~200ms (13%) on cold starts.**

## Setup

**Requirements:** Python 3.11+, Node.js 20+, AWS CDK CLI

```powershell
# Install dependencies
pip install -r requirements-dev.txt

# Configure AWS
cp .env.example .env
# Edit .env with your AWS profile and region

$env:AWS_PROFILE = "your-profile"
$env:AWS_REGION = "ap-south-1"
```

## Deploy

```powershell
# Build packages
python infra/build.py

# Deploy infrastructure
cd infra && cdk deploy && cd ..

# Wait for V2 to be ready (snapshot preparation takes a few minutes)
python scripts/smoke.py wait

# Verify Gateway tools
python scripts/smoke.py gateway

# Test invocations
python scripts/smoke.py invoke v1
python scripts/smoke.py invoke v2
```

## Run Benchmarks

```powershell
# Quick benchmark (5 calls)
python scripts/run.py --calls 5

# Full benchmark with questions
python scripts/questions.py

# Concurrency test
python scripts/run.py --concurrent 10
```

Results are saved to `docs/results/`.

## Enable Warmup Hooks (V2 only)

```powershell
# Add warmup hooks to V2 runtime
python scripts/update_with_hooks.py

# Remove hooks
python scripts/remove_hooks.py
```

## Clean Up

```powershell
cd infra && cdk destroy
```


