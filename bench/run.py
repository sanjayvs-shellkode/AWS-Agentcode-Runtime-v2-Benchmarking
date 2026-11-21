"""Benchmark AgentCore Runtime V1 vs V2 on the same agent.

Scenarios (each call is timed on the client, end to end):
  cold  - a new runtimeSessionId per call, so every call starts a new session (microVM)
  warm  - one session per runtime, reused; its first call is a setup call and isn't counted

Calls alternate V1/V2 so both see the same network and Open-Meteo conditions.
After the calls, the script waits for spans and adds each request's server-side time
(the POST /invocations span). overhead = client latency - server time, which is mostly
session startup plus network.

Usage (repo root, AWS_PROFILE and AWS_REGION set):
  python bench/run.py --calls 10                  # LLM mode (default)
  python bench/run.py --mode direct --calls 10    # Gateway tools only, no model
  python bench/run.py --analyze bench/results/<file>.csv   # redo the span lookup later
"""
import argparse
import csv
import json
import statistics
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import boto3
from botocore.config import Config

from smoke import REGION, outputs

RESULTS = Path(__file__).resolve().parent / "results"
VERSIONS = ("V1", "V2")
PAYLOADS = {
    "direct": {"mode": "direct", "city": "Paris"},
    "llm": {"prompt": "What is the weather in Paris right now?"},
}
FIELDS = ["run_id", "mode", "scenario", "version", "call", "session_id", "started_at",
          "latency_ms", "ok", "error", "server_ms", "overhead_ms"]


def runtime_arns() -> dict:
    out = outputs()
    return {"V1": out["RuntimeV1Arn"], "V2": out["RuntimeV2Arn"]}


def invoke(client, arn: str, session_id: str, payload: dict) -> dict:
    started_at = datetime.now(timezone.utc).isoformat()
    t0 = time.perf_counter()
    try:
        resp = client.invoke_agent_runtime(
            agentRuntimeArn=arn, runtimeSessionId=session_id, payload=json.dumps(payload).encode()
        )
        body = resp["response"].read()
        ok = b'"error"' not in body[:300]
        error = "" if ok else body[:200].decode(errors="replace")
    except Exception as e:  # recorded, not raised: one failed call must not stop the run
        ok, error = False, f"{type(e).__name__}: {e}"[:200]
    return {"started_at": started_at, "latency_ms": round((time.perf_counter() - t0) * 1000, 1),
            "ok": ok, "error": error}


def run(mode: str, calls: int) -> tuple[list[dict], str]:
    arns = runtime_arns()
    # No retries: a retry would hide a failure inside one longer latency.
    client = boto3.Session(region_name=REGION).client(
        "bedrock-agentcore", config=Config(read_timeout=180, retries={"total_max_attempts": 1})
    )
    run_id = datetime.now().strftime("%Y%m%d-%H%M%S") + f"-{mode}"
    rows: list[dict] = []

    def record(scenario: str, version: str, call: int, session_id: str) -> None:
        r = invoke(client, arns[version], session_id, PAYLOADS[mode])
        rows.append({"run_id": run_id, "mode": mode, "scenario": scenario, "version": version,
                     "call": call, "session_id": session_id, **r})
        status = "ok" if r["ok"] else "FAIL " + r["error"]
        print(f"{scenario:<10} {version} #{call:<2} {r['latency_ms']:>8.0f} ms  {status}", flush=True)

    def order(i: int) -> tuple[str, str]:
        return VERSIONS if i % 2 else VERSIONS[::-1]

    for i in range(1, calls + 1):
        for v in order(i):
            record("cold", v, i, str(uuid.uuid4()))
    warm = {v: str(uuid.uuid4()) for v in VERSIONS}
    for v in VERSIONS:
        record("warm-setup", v, 0, warm[v])
    for i in range(1, calls + 1):
        for v in order(i):
            record("warm", v, i, warm[v])
    return rows, run_id


def fetch_spans(rows: list[dict]):
    """Return lookup(version, session_id) -> that session's spans, oldest first.

    Each span is {"name", "start" (ns), "end" (ns), "ms", "attrs"}, read from the runtime's spans log stream.
    """
    ids = {v: arn.split("/")[-1] for v, arn in runtime_arns().items()}
    logs = boto3.Session(region_name=REGION).client("logs")
    start_ms = int(min(datetime.fromisoformat(r["started_at"]) for r in rows).timestamp() * 1000) - 60_000

    def lookup(version: str, session_id: str) -> list[dict]:
        kwargs = {"logGroupName": f"/aws/bedrock-agentcore/runtimes/{ids[version]}-DEFAULT",
                  "logStreamNames": ["spans"], "filterPattern": f'"{session_id}"', "startTime": start_ms}
        spans = []
        while True:
            page = logs.filter_log_events(**kwargs)
            for e in page["events"]:
                d = json.loads(e["message"])
                start, dur = d.get("startTimeUnixNano"), d.get("durationNano")
                if d.get("name") and start is not None and dur is not None:
                    spans.append({"name": d["name"], "start": start, "end": start + dur, "ms": dur / 1e6,
                                  "attrs": d.get("attributes", {})})
            if not page.get("nextToken"):
                break
            kwargs["nextToken"] = page["nextToken"]
        return sorted(spans, key=lambda s: s["start"])

    return lookup


def span_lookup(rows: list[dict]):
    """Return lookup(version, session_id) -> POST /invocations durations in ms, oldest first."""
    spans = fetch_spans(rows)
    return lambda version, session_id: [s["ms"] for s in spans(version, session_id)
                                        if s["name"] == "POST /invocations"]


def attach_server_times(rows: list[dict], lookup) -> int:
    """Match each call to its POST /invocations span, in time order per session.

    Returns how many calls got no server time (spans missing or counts don't match).
    """
    by_session: dict[tuple, list[dict]] = {}
    for r in rows:
        by_session.setdefault((r["version"], r["session_id"]), []).append(r)
    missing = 0
    for (version, session_id), group in by_session.items():
        group.sort(key=lambda r: r["started_at"])
        times = lookup(version, session_id)
        if len(times) != len(group):
            missing += len(group)
            for r in group:
                r["server_ms"] = r["overhead_ms"] = ""
            continue
        for r, ms in zip(group, times):
            r["server_ms"] = round(ms, 1)
            r["overhead_ms"] = round(float(r["latency_ms"]) - ms, 1)
    return missing


def stats(values: list[float]) -> dict | None:
    v = sorted(values)
    if not v:
        return None
    # Cut points at every 5%: index 14 = P75, 17 = P90, 18 = P95 (linear interpolation).
    q = statistics.quantiles(v, n=20, method="inclusive") if len(v) > 1 else [v[0]] * 19
    return {"n": len(v), "min": v[0], "p50": statistics.median(v), "p75": q[14], "p90": q[17],
            "p95": q[18], "max": v[-1], "mean": statistics.fmean(v)}


def summarize(rows: list[dict], missing: int, header: str) -> str:
    def fmt(x):
        return f"{x:,.0f}" if x is not None else "-"

    lines = [header, "",
             "| Scenario | Runtime | OK / calls | Min | P50 | P75 | P95 | Max | Mean | Server P50 | Overhead P50 |",
             "|---|---|---|---|---|---|---|---|---|---|---|"]
    for scenario in ("cold", "warm"):
        for v in VERSIONS:
            group = [r for r in rows if r["scenario"] == scenario and r["version"] == v]
            good = [r for r in group if str(r["ok"]) == "True"]
            lat = stats([float(r["latency_ms"]) for r in good])
            srv = stats([float(r["server_ms"]) for r in good if r["server_ms"] != ""])
            ovh = stats([float(r["overhead_ms"]) for r in good if r["overhead_ms"] != ""])
            if not lat:
                lines.append(f"| {scenario} | {v} | 0 / {len(group)} | - | - | - | - | - | - | - | - |")
                continue
            lines.append(
                f"| {scenario} | {v} | {len(good)} / {len(group)} | {fmt(lat['min'])} | {fmt(lat['p50'])} "
                f"| {fmt(lat['p75'])} | {fmt(lat['p95'])} | {fmt(lat['max'])} | {fmt(lat['mean'])} "
                f"| {fmt(srv['p50'] if srv else None)} | {fmt(ovh['p50'] if ovh else None)} |"
            )
    lines += ["", "All times in ms. Latency is measured on the client, end to end. Server = the agent's "
              "POST /invocations span. Overhead = latency - server (session startup + network).",
              f"Calls without a matched span: {missing}."]
    return "\n".join(lines)


def runtime_header(mode: str, run_id: str) -> str:
    ctrl = boto3.Session(region_name=REGION).client("bedrock-agentcore-control")
    parts = []
    for v, arn in runtime_arns().items():
        r = ctrl.get_agent_runtime(agentRuntimeId=arn.split("/")[-1])
        parts.append(f"{v}: {r['agentRuntimeName']} (version {r['agentRuntimeVersion']}, "
                     f"platform {r.get('platformVersion', 'V1')})")
    return f"## Run {run_id} - mode {mode}, region {REGION}\n\n" + "; ".join(parts)


def write_csv(rows: list[dict], path: Path) -> None:
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in FIELDS})


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=PAYLOADS, default="llm")
    ap.add_argument("--calls", type=int, default=10, help="measured calls per runtime per scenario")
    ap.add_argument("--span-wait", type=int, default=150, help="seconds to wait for spans to arrive")
    ap.add_argument("--analyze", type=Path, help="existing CSV: redo the span lookup and summary")
    a = ap.parse_args()

    if a.analyze:
        path = a.analyze
        with path.open(encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        mode, run_id = rows[0]["mode"], rows[0]["run_id"]
    else:
        RESULTS.mkdir(exist_ok=True)
        rows, run_id = run(a.mode, a.calls)
        mode = a.mode
        path = RESULTS / f"{run_id}.csv"
        write_csv(rows, path)
        print(f"\nwaiting {a.span_wait}s for spans...", flush=True)
        time.sleep(a.span_wait)

    missing = attach_server_times(rows, span_lookup(rows))
    write_csv(rows, path)
    summary = summarize(rows, missing, runtime_header(mode, run_id))
    path.with_suffix(".md").write_text(summary + "\n", encoding="utf-8")
    print("\n" + summary)
    print(f"\nraw: {path}\nsummary: {path.with_suffix('.md')}")


if __name__ == "__main__":
    main()
