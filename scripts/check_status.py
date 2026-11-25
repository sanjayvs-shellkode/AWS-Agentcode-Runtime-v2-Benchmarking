"""Check status of both runtimes.

Usage:
    python scripts/check_status.py

Requires AWS_PROFILE and AWS_REGION environment variables.
"""
import os
import boto3

REGION = os.environ.get("AWS_REGION", "ap-south-1")
RUNTIME_NAMES = ["wxbench_weather_v1", "wxbench_weather_v2"]

client = boto3.client("bedrock-agentcore-control", region_name=REGION)

runtimes = client.list_agent_runtimes()
for r in runtimes["agentRuntimes"]:
    if r["agentRuntimeName"] in RUNTIME_NAMES:
        hooks = ""
        if r["agentRuntimeName"].endswith("v2"):
            # Get detailed info for V2 to check hooks
            detail = client.get_agent_runtime(agentRuntimeId=r["agentRuntimeId"])
            if detail.get("hooksConfiguration"):
                hooks = " (warmup hooks enabled)"
        print(f"{r['agentRuntimeName']}: {r['status']}{hooks}")
