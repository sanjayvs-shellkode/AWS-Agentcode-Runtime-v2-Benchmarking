"""Build the two code packages that CDK uploads: the Lambda tool and the agent runtime.

Dependencies are installed for Linux (manylinux, Python 3.12) so the packages run in AWS
even when this script runs on Windows: x86_64 for Lambda, ARM64 for AgentCore Runtime.
"""
import shutil
import subprocess
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BUILD = ROOT / "build"
# Lambda runs x86_64 by default; AgentCore Runtime runs Linux ARM64.
LAMBDA_PLATFORM = "x86_64-manylinux2014"
RUNTIME_PLATFORM = "aarch64-manylinux2014"


def _fresh(path: Path) -> Path:
    if path.exists():
        shutil.rmtree(path)
    path.mkdir(parents=True)
    return path


def _install(target: Path, platform: str, args: list[str]) -> None:
    flags = ["--python-platform", platform, "--python-version", "3.12"]
    subprocess.run(["uv", "pip", "install", "--quiet", "--target", str(target), *flags, *args], check=True)


def build_lambda() -> None:
    out = _fresh(BUILD / "lambda")
    shutil.copy(ROOT / "lambda" / "handler.py", out)
    shutil.copy(ROOT / "agent" / "weather.py", out)  # single source for the weather logic
    _install(out, LAMBDA_PLATFORM, ["requests"])


# uv on Windows writes console scripts as .exe launchers, which can't run on Linux.
# The runtime starts the agent through opentelemetry-instrument, so write a Linux one.
OTEL_LAUNCHER = """#!/usr/bin/env python3
import sys
from opentelemetry.instrumentation.auto_instrumentation import run
sys.exit(run())
"""


def build_agent_zip() -> None:
    staging = _fresh(BUILD / "agent_pkg")
    _install(staging, RUNTIME_PLATFORM, ["-r", str(ROOT / "requirements.txt")])
    for exe in (staging / "bin").glob("*.exe"):
        exe.unlink()
    (staging / "bin" / "opentelemetry-instrument").write_text(OTEL_LAUNCHER, newline="\n")
    for src in (ROOT / "agent").glob("*.py"):
        if not src.name.startswith("test_"):
            shutil.copy(src, staging)
    zip_path = BUILD / "agent-package.zip"
    if zip_path.exists():
        zip_path.unlink()
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in staging.rglob("*"):
            if path.is_file():
                info = zipfile.ZipInfo.from_file(path, path.relative_to(staging).as_posix())
                # Windows has no exec bit; set it so Linux can run bin/ scripts.
                info.external_attr = (0o755 if path.parent.name == "bin" else 0o644) << 16
                info.compress_type = zipfile.ZIP_DEFLATED
                zf.writestr(info, path.read_bytes())
    print(f"wrote {zip_path} ({zip_path.stat().st_size / 1_048_576:.1f} MB)")


if __name__ == "__main__":
    build_lambda()
    build_agent_zip()
