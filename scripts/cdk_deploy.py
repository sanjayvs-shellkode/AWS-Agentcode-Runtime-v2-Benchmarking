"""Deploy CDK stack and capture output.

Usage:
    python scripts/cdk_deploy.py

Requires AWS_PROFILE and AWS_REGION environment variables to be set.
"""
import os
import subprocess
import shutil

os.chdir(os.path.join(os.path.dirname(__file__), "..", "infra"))

# Remove lock
if os.path.exists("cdk.out"):
    shutil.rmtree("cdk.out", ignore_errors=True)

result = subprocess.run(
    ["cdk", "deploy", "--require-approval", "never"],
    capture_output=True,
    text=True,
    shell=True,
)

print("=== STDOUT ===")
print(result.stdout)
print("=== STDERR ===")
print(result.stderr)
print(f"=== EXIT CODE: {result.returncode} ===")
