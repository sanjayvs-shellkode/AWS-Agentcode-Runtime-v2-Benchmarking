"""Upload new package and update runtime with hooksConfiguration.

Usage:
    python scripts/update_with_hooks.py

Requires AWS_PROFILE and AWS_REGION environment variables.
Reads runtime IDs and other config from CloudFormation stack outputs.
"""
import os
import time
import hashlib
from pathlib import Path

import boto3

# Config from environment
REGION = os.environ.get("AWS_REGION", "ap-south-1")
STACK_NAME = "wxbench-weather"
LOCAL_ZIP = Path(__file__).resolve().parents[1] / "build" / "agent-package.zip"


def get_stack_outputs():
    """Get outputs from CloudFormation stack."""
    cfn = boto3.client("cloudformation", region_name=REGION)
    stack = cfn.describe_stacks(StackName=STACK_NAME)["Stacks"][0]
    return {o["OutputKey"]: o["OutputValue"] for o in stack["Outputs"]}


def get_cdk_bucket():
    """Get the CDK assets bucket for this account/region."""
    sts = boto3.client("sts", region_name=REGION)
    account = sts.get_caller_identity()["Account"]
    return f"cdk-hnb659fds-assets-{account}-{REGION}"


def get_runtime_config(runtime_arn: str):
    """Get current runtime configuration."""
    agentcore = boto3.client("bedrock-agentcore-control", region_name=REGION)
    runtime_id = runtime_arn.split("/")[-1]
    return agentcore.get_agent_runtime(agentRuntimeId=runtime_id)


def main():
    if not LOCAL_ZIP.exists():
        print(f"ERROR: {LOCAL_ZIP} not found. Run 'python infra/build.py' first.")
        return

    # Get stack outputs
    print("Getting stack outputs...")
    outputs = get_stack_outputs()
    runtime_arn = outputs["RuntimeV2Arn"]
    runtime_id = runtime_arn.split("/")[-1]
    gateway_url = outputs["GatewayUrl"]
    memory_id = outputs["MemoryArn"].split("/")[-1]

    # Get CDK bucket and current runtime config
    bucket = get_cdk_bucket()
    print(f"CDK bucket: {bucket}")

    # Get current runtime to find role ARN and model ID
    print(f"Getting current runtime config for {runtime_id}...")
    current = get_runtime_config(runtime_arn)
    role_arn = current["roleArn"]
    current_env = current.get("environmentVariables", {})
    model_id = current_env.get("MODEL_ID", "")

    if not model_id:
        print("WARNING: MODEL_ID not found in current runtime config. You may need to set it manually.")

    # Calculate hash for unique S3 key
    with open(LOCAL_ZIP, "rb") as f:
        content_hash = hashlib.sha256(f.read()).hexdigest()[:12]
    s3_key = f"agent-warmup-{content_hash}.zip"

    # Upload package
    s3 = boto3.client("s3", region_name=REGION)
    print(f"Uploading {LOCAL_ZIP.name} to s3://{bucket}/{s3_key} ...")
    s3.upload_file(str(LOCAL_ZIP), bucket, s3_key)
    print("Upload complete.")

    # Wait for runtime to be ready
    agentcore = boto3.client("bedrock-agentcore-control", region_name=REGION)
    print("Checking runtime status...")
    while True:
        resp = agentcore.get_agent_runtime(agentRuntimeId=runtime_id)
        status = resp["status"]
        print(f"  Status: {status}")
        if status == "READY":
            break
        elif status in ("FAILED", "UPDATE_FAILED", "CREATE_FAILED"):
            print(f"  Failure reason: {resp.get('failureReason', 'unknown')}")
            print("Proceeding with update anyway...")
            break
        time.sleep(10)

    # Environment variables
    env_vars = {
        "GATEWAY_URL": gateway_url,
        "MEMORY_ID": memory_id,
    }
    if model_id:
        env_vars["MODEL_ID"] = model_id
    print(f"Environment variables: {list(env_vars.keys())}")

    # Update with hooks
    print("Updating runtime with hooksConfiguration...")
    try:
        response = agentcore.update_agent_runtime(
            agentRuntimeId=runtime_id,
            agentRuntimeArtifact={
                "codeConfiguration": {
                    "code": {
                        "s3": {
                            "bucket": bucket,
                            "prefix": s3_key,
                        }
                    },
                    "runtime": "PYTHON_3_12",
                    "entryPoint": ["opentelemetry-instrument", "entrypoint.py"],
                }
            },
            roleArn=role_arn,
            networkConfiguration={"networkMode": "PUBLIC"},
            environmentVariables=env_vars,
            hooksConfiguration={
                "build": {
                    "warmup": {
                        "action": {
                            "httpPost": {
                                "path": "/warmup",
                                "port": 8080,
                            }
                        }
                    }
                }
            },
        )
        print(f"Update started! Status: {response.get('status')}")
        print("SUCCESS: Runtime updated with warmup hooks!")
    except Exception as e:
        print(f"ERROR: {type(e).__name__}: {e}")


if __name__ == "__main__":
    main()
