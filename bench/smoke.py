"""Smoke checks for the deployed stack. Reads resource IDs from the CDK outputs.

Usage (from the repo root, with AWS_PROFILE and AWS_REGION set):
  python bench/smoke.py wait              # poll both runtimes until READY
  python bench/smoke.py gateway           # MCP tools/list through the Gateway
  python bench/smoke.py invoke v1         # one agent (LLM) call on weather-v1
  python bench/smoke.py invoke v2 direct  # Gateway tools only, no model
"""
import json
import sys
import time
import uuid
from pathlib import Path

import boto3
import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "agent"))
from gateway_client import GatewayAuth  # noqa: E402

REGION = "ap-south-1"
STACK = "wxbench-weather"
RUNTIMES = {"v1": "wxbench_weather_v1", "v2": "wxbench_weather_v2"}


def outputs() -> dict:
    cfn = boto3.Session(region_name=REGION).client("cloudformation")
    stack = cfn.describe_stacks(StackName=STACK)["Stacks"][0]
    return {o["OutputKey"]: o["OutputValue"] for o in stack["Outputs"]}


def wait() -> None:
    ctrl = boto3.Session(region_name=REGION).client("bedrock-agentcore-control")
    for key, arn in ((k, outputs()[f"Runtime{k.upper()}Arn"]) for k in RUNTIMES):
        runtime_id = arn.split("/")[-1]
        started = time.time()
        while True:
            status = ctrl.get_agent_runtime(agentRuntimeId=runtime_id)["status"]
            if status == "READY" or status.endswith("FAILED"):
                break
            time.sleep(10)
        print(f"{key}: {status} after {time.time() - started:.0f}s")


def gateway() -> None:
    url = outputs()["GatewayUrl"]
    region = url.split(".bedrock-agentcore.")[1].split(".amazonaws.com")[0]
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
    # Same SigV4 signing the runtime uses. Credentials come from AWS_PROFILE.
    resp = httpx.post(
        url,
        json=body,
        headers={"content-type": "application/json", "accept": "application/json, text/event-stream"},
        auth=GatewayAuth(region),
        timeout=60,
    )
    print(resp.status_code)
    print(resp.text[:1500])


def invoke(which: str, mode: str = "llm") -> None:
    arn = outputs()[f"Runtime{which.upper()}Arn"]
    client = boto3.Session(region_name=REGION).client("bedrock-agentcore")
    session_id = str(uuid.uuid4())  # 36 characters, above the 33-character minimum
    if mode == "direct":
        body = {"mode": "direct", "city": "Paris"}
    else:
        body = {"prompt": "What is the weather in Paris right now?"}
    payload = json.dumps(body).encode()
    resp = client.invoke_agent_runtime(agentRuntimeArn=arn, runtimeSessionId=session_id, payload=payload)
    print("session:", session_id)
    print(resp["response"].read().decode())


if __name__ == "__main__":
    cmd = sys.argv[1]
    if cmd == "wait":
        wait()
    elif cmd == "gateway":
        gateway()
    elif cmd == "invoke":
        invoke(sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else "llm")
    else:
        raise SystemExit(f"unknown command: {cmd}")
