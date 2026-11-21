"""Deploy time, billed runtime usage and cost for a benchmark run.

Sources:
  deploy time           CloudFormation events: the latest create/update of each runtime, start to COMPLETE
  vCPU-hours, GB-hours  AgentCore's CPUUsed-vCPUHours and MemoryUsed-GBHours CloudWatch metrics
  model tokens          the agent's invoke_agent span per call (read by questions.py)
"""
from datetime import datetime, timedelta, timezone

import boto3

from run import REGION, VERSIONS, runtime_arns
from smoke import STACK

# USD rates read on 2026-11-21 from aws.amazon.com/bedrock/agentcore/pricing and aws.amazon.com/bedrock/pricing.
# shortcut: the runtime rates come from the pricing page's worked examples (US East); replace them with your
# Region's rates before quoting costs.
RUNTIME_RATES = {
    "V1": {"vcpu_hour": 0.0895, "gb_hour": 0.00945},  # previous-generation microVM consumption rate
    "V2": {"vcpu_hour": 0.1276, "gb_hour": 0.0169},   # new runtime, consumption (no committed baseline)
}
MODEL_RATES_PER_MTOK = {"input": 1.00, "output": 5.00}  # Claude Haiku 4.5, global cross-Region inference
# Sessions are billed until they end after 15 minutes idle, and the metrics arrive a few minutes later.
SETTLE = timedelta(minutes=30)
LOGICAL_IDS = {"V1": "RuntimeV1", "V2": "RuntimeV2"}


def last_operation(events: list[dict]) -> dict | None:
    """Duration of the newest successful create/update in one resource's events (newest first)."""
    for i, e in enumerate(events):
        if e["ResourceStatus"] not in ("CREATE_COMPLETE", "UPDATE_COMPLETE"):
            continue
        start = None
        for older in events[i + 1:]:
            if not older["ResourceStatus"].endswith("IN_PROGRESS"):
                break
            start = older["Timestamp"]
        if start is None:
            return None
        return {"seconds": (e["Timestamp"] - start).total_seconds(),
                "operation": e["ResourceStatus"].split("_")[0].lower(), "finished": e["Timestamp"]}
    return None


def deploy_times() -> dict:
    cfn = boto3.Session(region_name=REGION).client("cloudformation")
    events = [e for page in cfn.get_paginator("describe_stack_events").paginate(StackName=STACK)
              for e in page["StackEvents"]]
    return {v: last_operation([e for e in events if e["LogicalResourceId"] == lid])
            for v, lid in LOGICAL_IDS.items()}


def billed_usage(start: datetime, end: datetime) -> dict:
    cw = boto3.Session(region_name=REGION).client("cloudwatch")
    start = start.replace(second=0, microsecond=0) - timedelta(minutes=start.minute % 5)
    out = {}
    for v, arn in runtime_arns().items():
        name = arn.split("/")[-1].rsplit("-", 1)[0]
        dims = [{"Name": "Resource", "Value": arn}, {"Name": "Service", "Value": "AgentCore.Runtime"},
                {"Name": "Name", "Value": f"{name}::DEFAULT"}]
        out[v] = {}
        for key, metric in (("vcpu_hours", "CPUUsed-vCPUHours"), ("gb_hours", "MemoryUsed-GBHours")):
            r = cw.get_metric_statistics(Namespace="AWS/Bedrock-AgentCore", MetricName=metric, Dimensions=dims,
                                         StartTime=start, EndTime=end, Period=300, Statistics=["Sum"])
            out[v][key] = sum(p["Sum"] for p in r["Datapoints"])
    return out


def collect(rows: list[dict]) -> dict:
    """Deploy times and billed usage for the window of this run, until its sessions have ended."""
    first = min(datetime.fromisoformat(r["started_at"]) for r in rows) - timedelta(minutes=1)
    last = max(datetime.fromisoformat(r["started_at"]) + timedelta(milliseconds=float(r["latency_ms"]))
               for r in rows)
    settled_at = last + SETTLE
    now = datetime.now(timezone.utc)
    return {"deploy": deploy_times(), "billed": billed_usage(first, min(settled_at, now)),
            "settled_at": settled_at, "partial": now < settled_at}


def cost_summary(rows: list[dict], billed: dict) -> dict:
    """Runtime compute and model cost per runtime, and per successful call."""
    out = {}
    for v in VERSIONS:
        mine = [r for r in rows if r["version"] == v]
        ok = [r for r in mine if str(r["ok"]) == "True"]
        counted = [r for r in ok if r.get("input_tokens") not in ("", None)]
        tokens_in = sum(int(r["input_tokens"]) for r in counted)
        tokens_out = sum(int(r["output_tokens"]) for r in counted)
        use, rate = billed[v], RUNTIME_RATES[v]
        runtime_usd = use["vcpu_hours"] * rate["vcpu_hour"] + use["gb_hours"] * rate["gb_hour"]
        model_usd = (tokens_in * MODEL_RATES_PER_MTOK["input"] + tokens_out * MODEL_RATES_PER_MTOK["output"]) / 1e6
        sessions = len({r["session_id"] for r in mine})
        out[v] = {
            "tasks": len(ok), "sessions": sessions, "tokens_missing": len(ok) - len(counted),
            "vcpu_hours": use["vcpu_hours"], "gb_hours": use["gb_hours"],
            "gb_hours_per_session": use["gb_hours"] / sessions if sessions else None,
            "runtime_usd": runtime_usd, "tokens_in": tokens_in, "tokens_out": tokens_out, "model_usd": model_usd,
            "total_usd": runtime_usd + model_usd,
            "per_task_usd": (runtime_usd + model_usd) / len(ok) if ok else None,
            "runtime_per_task_usd": runtime_usd / len(ok) if ok else None,
        }
    return out
