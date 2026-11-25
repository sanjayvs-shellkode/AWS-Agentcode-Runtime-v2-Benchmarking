"""Update V2 runtime WITHOUT hooksConfiguration to test the difference.

Usage:
    python scripts/remove_hooks.py

Requires AWS_PROFILE and AWS_REGION environment variables.
Reads runtime ID from CloudFormation stack outputs.
"""
import os
import time
import boto3

REGION = os.environ.get("AWS_REGION", "ap-south-1")
STACK_NAME = "wxbench-weather"


def get_v2_runtime_id():
    """Get V2 runtime ID from CloudFormation stack."""
    cfn = boto3.client("cloudformation", region_name=REGION)
    stack = cfn.describe_stacks(StackName=STACK_NAME)["Stacks"][0]
    outputs = {o["OutputKey"]: o["OutputValue"] for o in stack["Outputs"]}
    return outputs["RuntimeV2Arn"].split("/")[-1]


def main():
    runtime_id = get_v2_runtime_id()
    agentcore = boto3.client("bedrock-agentcore-control", region_name=REGION)

    # Get current config
    print(f"Getting current runtime config for {runtime_id}...")
    resp = agentcore.get_agent_runtime(agentRuntimeId=runtime_id)
    artifact = resp["agentRuntimeArtifact"]
    role_arn = resp["roleArn"]
    env_vars = resp.get("environmentVariables", {})

    print(f"Current hooksConfiguration: {resp.get('hooksConfiguration', 'None')}")
    print("Updating WITHOUT hooksConfiguration...")

    # Wait for READY
    while True:
        resp = agentcore.get_agent_runtime(agentRuntimeId=runtime_id)
        if resp["status"] == "READY":
            break
        print(f"  Status: {resp['status']}, waiting...")
        time.sleep(10)

    # Update WITHOUT hooks
    response = agentcore.update_agent_runtime(
        agentRuntimeId=runtime_id,
        agentRuntimeArtifact=artifact,
        roleArn=role_arn,
        networkConfiguration={"networkMode": "PUBLIC"},
        environmentVariables=env_vars,
        # NO hooksConfiguration parameter = removes hooks
    )
    print(f"Update started! Status: {response.get('status')}")

    # Wait for ready
    print("Waiting for runtime to be ready...")
    for i in range(60):
        resp = agentcore.get_agent_runtime(agentRuntimeId=runtime_id)
        status = resp["status"]
        print(f"  Status: {status}")
        if status == "READY":
            print(f"\nRuntime READY. hooksConfiguration: {resp.get('hooksConfiguration', 'None')}")
            break
        elif "FAILED" in status:
            print(f"Failed: {resp.get('failureReason')}")
            break
        time.sleep(10)


if __name__ == "__main__":
    main()
