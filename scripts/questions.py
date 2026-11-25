"""Ten-question V1 vs V2 benchmark (LLM mode) with an HTML report.

The questions cover several cities and need a unit conversion, so every answer uses all three
tools: geocode_city, get_forecast and convert_temperature.

For each runtime, with the same ten questions:
  cold        - each question once, each on a new session (new microVM)
  warm        - one session per runtime: a setup call, then each question once on that session
  concurrent  - bursts of 1, 10 and 50 new sessions sent at the same time (questions cycled)

V1 and V2 bursts run one after the other, never together, with a pause before each burst.
No client retries, so throttled calls show up as failures instead of longer latencies.

After the run, each call is matched to its spans for server time, the tools it ran and its model
tokens. The report adds deploy time and billed vCPU/GB-hours (see usage.py) and cost per task.
Billed usage settles about 30 minutes after the last call, when the sessions have ended; run
--analyze again after that for final memory and cost numbers.

Usage (repo root, AWS_PROFILE and AWS_REGION set):
  python scripts/questions.py
  python scripts/questions.py --analyze docs/results/<file>.csv   # redo spans, usage and the report
"""
import argparse
import csv
import html
import time
import uuid
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

import boto3
from botocore.config import Config

import usage
from run import REGION, VERSIONS, fetch_spans, invoke, runtime_arns, stats

RESULTS = Path(__file__).resolve().parents[1] / "docs" / "results"
# Each needs coordinates, current weather, and a unit conversion; most need two or three cities.
QUESTIONS = [
    "Compare the current temperature in London and New York in Fahrenheit. Which city is warmer, and by how many degrees?",
    "I'm flying from Mumbai to Chicago today. Give me the current temperature in both cities in Celsius and Fahrenheit, plus the humidity and wind in each.",
    "Is it colder in Moscow or Toronto right now? Give both temperatures in Fahrenheit and tell me what to wear in the colder one.",
    "Rank Tokyo, Sydney and Paris from warmest to coldest right now, with each temperature in Fahrenheit.",
    "My friend in Denver says it's 50°F there. What is that in Celsius, and is Bangalore warmer or colder than that right now?",
    "Is it above 80°F in Singapore right now? Also tell me the humidity, the wind speed, and whether it's raining.",
    "Plan my morning in Berlin: the current temperature in both Celsius and Fahrenheit, the wind, and whether I need a jacket.",
    "Which is closer to a room temperature of 72°F right now, Cape Town or Chennai? Show both temperatures in Fahrenheit.",
    "What's the temperature difference in Fahrenheit between Delhi and Reykjavik right now, and how do their conditions differ?",
    "I'm choosing between Dubai, Barcelona and Seoul for an outdoor event today. Give each city's temperature in Fahrenheit, humidity and wind, and recommend one.",
]
ALL_TOOLS = {"geocode_city", "get_forecast", "convert_temperature"}
FIELDS = ["run_id", "scenario", "concurrency", "version", "q", "question", "session_id",
          "started_at", "latency_ms", "ok", "error", "server_ms", "overhead_ms", "tools",
          "input_tokens", "output_tokens"]
SPAN_FIELDS = ("server_ms", "overhead_ms", "tools", "input_tokens", "output_tokens")


def order(i: int) -> tuple[str, str]:
    # Alternate which runtime goes first so neither always runs second.
    return VERSIONS if i % 2 == 0 else VERSIONS[::-1]


def make_caller(run_id: str, pool_size: int):
    """Return call(scenario, version, question_index, session_id, concurrency) -> result row."""
    arns = runtime_arns()
    client = boto3.Session(region_name=REGION).client("bedrock-agentcore", config=Config(
        read_timeout=300, retries={"total_max_attempts": 1}, max_pool_connections=pool_size + 10))

    def call(scenario: str, version: str, qi: int, session_id: str, concurrency="") -> dict:
        r = invoke(client, arns[version], session_id, {"prompt": QUESTIONS[qi]})
        return {"run_id": run_id, "scenario": scenario, "concurrency": concurrency,
                "version": version, "q": qi + 1, "question": QUESTIONS[qi], "session_id": session_id, **r}
    return call


def run_burst(call, version: str, level: int, gap: int) -> list[dict]:
    """Pause, then send `level` new sessions to one runtime at the same time."""
    print(f"pausing {gap}s before the {version} burst of {level}...", flush=True)
    time.sleep(gap)
    with ThreadPoolExecutor(max_workers=level) as pool:
        futures = [pool.submit(call, "concurrent", version, j % len(QUESTIONS), str(uuid.uuid4()), level)
                   for j in range(level)]
        burst = [f.result() for f in futures]
    s = stats([b["latency_ms"] for b in burst if b["ok"]])
    print(f"concurrent x{level:<2} {version}: {sum(b['ok'] for b in burst)}/{level} ok"
          + (f", P50 {s['p50']:.0f} ms, max {s['max']:.0f} ms" if s else ""), flush=True)
    return burst


def rerun_burst(rows: list[dict], level: int, gap: int) -> list[dict]:
    """Replace one burst size's rows in an existing run with a fresh burst on each runtime."""
    call = make_caller(rows[0]["run_id"], level)
    kept = [r for r in rows if not (r["scenario"] == "concurrent" and str(r["concurrency"]) == str(level))]
    for v in VERSIONS:
        kept += run_burst(call, v, level, gap)
    return kept


def run_suite(levels: list[int], gap: int) -> tuple[list[dict], str]:
    questions = QUESTIONS
    run_id = datetime.now().strftime("%Y%m%d-%H%M%S") + "-complex"
    call = make_caller(run_id, max(levels))
    rows: list[dict] = []

    def keep(row: dict) -> None:
        rows.append(row)
        status = "ok" if row["ok"] else "FAIL " + row["error"][:80]
        print(f"{row['scenario']:<10} {row['version']} q{row['q']:<2} {row['latency_ms']:>8.0f} ms  {status}",
              flush=True)

    for qi in range(len(questions)):
        for v in order(qi):
            keep(call("cold", v, qi, str(uuid.uuid4())))
    warm = {v: str(uuid.uuid4()) for v in VERSIONS}
    for v in VERSIONS:
        keep(call("warm-setup", v, 0, warm[v]))
    for qi in range(len(questions)):
        for v in order(qi):
            keep(call("warm", v, qi, warm[v]))

    for n, level in enumerate(levels):
        for v in order(n):
            rows.extend(run_burst(call, v, level, gap))
    return rows, run_id


def attach_span_data(rows: list[dict], lookup) -> int:
    """Fill server time, tools (in call order) and model tokens for each call from its spans.

    Calls in one session are matched to that session's POST /invocations spans in time order, and
    each call gets the spans inside its request. Returns how many calls couldn't be matched.
    """
    by_session: dict[tuple, list[dict]] = {}
    for r in rows:
        by_session.setdefault((r["version"], r["session_id"]), []).append(r)
    missing = 0
    for (version, session_id), group in by_session.items():
        group.sort(key=lambda r: r["started_at"])
        spans = lookup(version, session_id)
        requests = [s for s in spans if s["name"] == "POST /invocations"]
        if len(requests) != len(group):
            missing += len(group)
            for r in group:
                r.update(dict.fromkeys(SPAN_FIELDS, ""))
            continue
        for r, req in zip(group, requests):
            inside = [s for s in spans if req["start"] <= s["start"] <= req["end"]]
            agent = [s["attrs"] for s in inside if s["name"].startswith("invoke_agent")]
            r["server_ms"] = round(req["ms"], 1)
            r["overhead_ms"] = round(float(r["latency_ms"]) - req["ms"], 1)
            r["tools"] = " ".join(s["name"].split("___", 1)[-1] for s in inside if s["name"].startswith("execute_tool "))
            r["input_tokens"] = sum(int(a.get("gen_ai.usage.input_tokens", 0)) for a in agent) if agent else ""
            r["output_tokens"] = sum(int(a.get("gen_ai.usage.output_tokens", 0)) for a in agent) if agent else ""
    return missing


def runtime_info() -> dict:
    ctrl = boto3.Session(region_name=REGION).client("bedrock-agentcore-control")
    info = {}
    for v, arn in runtime_arns().items():
        r = ctrl.get_agent_runtime(agentRuntimeId=arn.split("/")[-1])
        info[v] = f"{r['agentRuntimeName']} (version {r['agentRuntimeVersion']}, platform {r.get('platformVersion', 'V1')})"
    return info


# ---------- report ----------

def _ok(r: dict) -> bool:
    return str(r["ok"]) == "True"


def _pick(rows, scenario, version, level=None, q=None):
    return [r for r in rows if r["scenario"] == scenario and r["version"] == version
            and (level is None or str(r["concurrency"]) == str(level))
            and (q is None or str(r["q"]) == str(q))]


def _stats(rows, key):
    return stats([float(r[key]) for r in rows if _ok(r) and r[key] not in ("", None)])


def _ms(x) -> str:
    return "-" if x is None else f"{x:,.0f}"


def _sec(x) -> str:
    return "-" if x is None else f"{x / 1000:.2f} s"


def _delta(v1, v2) -> str:
    if v1 is None or v2 is None or v1 == 0:
        return "-"
    d = (v2 - v1) / v1 * 100
    cls = "good" if d <= -5 else "bad" if d >= 5 else "flat"
    return f'<span class="{cls}">{d:+.0f}%</span>'


def _get(s, key):
    return s[key] if s else None


def _tools_label(tools: str) -> str:
    if not tools:
        return "-"
    c = Counter(tools.split())
    return ", ".join(f"{name}×{n}" if n > 1 else name for name, n in c.items())


def _uses_all(r: dict) -> bool:
    return ALL_TOOLS <= set(r.get("tools", "").split())


CSS = """
body{font-family:'Segoe UI',Arial,sans-serif;margin:0;color:#1f2937;background:#fff}
.wrap{max-width:1180px;margin:0 auto;padding:18px 28px 40px}
.top{display:flex;justify-content:space-between;align-items:center;border-bottom:1px solid #e5e7eb;padding-bottom:8px}
.logo{font-weight:800;font-size:20px;color:#1d4ed8}.logo span{color:#f97316}
.tag{font-size:12px;color:#6b7280}
h1{font-size:24px;margin:16px 0 4px}
.lead{color:#374151;font-size:14px;margin:0 0 16px;line-height:1.5}
.cards{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin:12px 0 22px}
.card{border-radius:8px;padding:12px 14px;border:1px solid #e5e7eb}
.card.g{background:#f0fdf4;border-color:#bbf7d0}.card.b{background:#eff6ff;border-color:#bfdbfe}
.card .big{font-size:26px;font-weight:700}.card.g .big{color:#15803d}.card.b .big{color:#1d4ed8}
.card .sub{font-size:12px;color:#4b5563;margin-top:4px;line-height:1.4}
h2{font-size:15px;font-weight:800;color:#1d4ed8;text-transform:uppercase;letter-spacing:.3px;margin:26px 0 8px}
table{border-collapse:collapse;width:100%;font-size:13px;margin-bottom:6px}
th{background:#f3f4f6;text-align:left;padding:7px 8px;border-bottom:2px solid #d1d5db;font-weight:700}
td{padding:6px 8px;border-bottom:1px solid #e5e7eb;vertical-align:top}
td.n{text-align:right;font-variant-numeric:tabular-nums}
td.t{font-size:12px;color:#374151}
.good{color:#15803d;font-weight:700}.bad{color:#b91c1c;font-weight:700}.flat{color:#6b7280}
.note{font-size:12px;color:#4b5563;line-height:1.55}
ul.note{padding-left:18px;margin:6px 0}
"""



def _duration(seconds) -> str:
    if seconds is None:
        return "-"
    return f"{seconds:.0f} s" if seconds < 90 else f"{int(seconds // 60)} min {int(seconds % 60)} s"


def _usd(x) -> str:
    return "-" if x is None else f"${x:.5f}"


def metrics_table(rows: list[dict], usage_data: dict | None) -> str:
    """The headline V1/V2 metrics, each with how it was measured."""
    cold = {v: _stats(_pick(rows, "cold", v), "latency_ms") for v in VERSIONS}
    cold_ovh = {v: _stats(_pick(rows, "cold", v), "overhead_ms") for v in VERSIONS}
    warm_ovh = {v: _stats(_pick(rows, "warm", v), "overhead_ms") for v in VERSIONS}
    levels = sorted({int(r["concurrency"]) for r in rows if r["scenario"] == "concurrent"})
    top = levels[-1] if levels else None
    deploy = (usage_data or {}).get("deploy", {})
    billed = (usage_data or {}).get("billed")
    cost = usage.cost_summary(rows, billed) if billed else {}
    n_cold = {v: _get(cold[v], "n") or 0 for v in VERSIONS}

    def burst(v):
        g = _pick(rows, "concurrent", v, top)
        s = _stats(g, "latency_ms")
        return f"{sum(map(_ok, g))}/{len(g)} succeeded at x{top}, P50 {_sec(_get(s, 'p50'))}" if g else "-"

    def deploy_cell(v):
        d = deploy.get(v)
        return f"{_duration(d['seconds'])} ({d['operation']})" if d else "-"

    def memory_cell(v):
        c = cost.get(v)
        return f"{c['gb_hours']:.2f} GB-h ({c['gb_hours_per_session']:.3f} per session)" if c else "-"

    def cpu_cell(v):
        c = cost.get(v)
        return f"{c['vcpu_hours']:.4f} vCPU-h" if c else "-"

    def cost_cell(v):
        c = cost.get(v)
        if not c:
            return "-"
        return (f"{_usd(c['per_task_usd'])}<br><span class=note>runtime {_usd(c['runtime_per_task_usd'])} + "
                f"model {_usd(c['model_usd'] / c['tasks'] if c['tasks'] else None)}</span>")

    table = [
        ("Initial startup latency", deploy_cell,
         "Latest create/update of the runtime to READY, from CloudFormation events. V2 prepares its snapshot here."),
        ("Session startup latency", lambda v: _sec(_get(cold_ovh[v], "p50")),
         f"Cold-start P50 of client latency minus the agent's own time (POST /invocations span). Includes "
         f"network: warm overhead P50 is {_sec(_get(warm_ovh['V1'], 'p50'))} / {_sec(_get(warm_ovh['V2'], 'p50'))}."),
        ("P50 latency", lambda v: _sec(_get(cold[v], "p50")), f"Cold start, end to end, n = {n_cold['V1']} / {n_cold['V2']}."),
        ("P75 latency", lambda v: _sec(_get(cold[v], "p75")), "Cold start, end to end."),
        ("P95 latency", lambda v: _sec(_get(cold[v], "p95")), "Cold start, end to end. With 10 calls, close to the slowest call."),
        ("Concurrent sessions", burst, "New sessions sent at the same time; full results per burst size below."),
        ("Memory consumption", memory_cell, "Billed MemoryUsed-GBHours for the run, from CloudWatch."),
        ("CPU consumption", cpu_cell, "Billed CPUUsed-vCPUHours for the run, from CloudWatch."),
        ("Cost per task", cost_cell,
         "(Runtime vCPU-h and GB-h × rate + model tokens × rate) / successful calls. Gateway, Lambda and Memory "
         "charges are excluded; they are well under $0.0001 per call here."),
    ]
    body = "".join(f"<tr><td><b>{name}</b></td><td>{cell('V1')}</td><td>{cell('V2')}</td><td class=t>{how}</td></tr>"
                   for name, cell, how in table)
    notes = []
    if usage_data and usage_data.get("partial"):
        notes.append(f"Billed usage is still settling: sessions bill until they end after 15 minutes idle. "
                     f"Rerun with --analyze after {usage_data['settled_at']:%Y-%m-%d %H:%M} UTC for final memory and cost.")
    if cost:
        r1, r2 = usage.RUNTIME_RATES["V1"], usage.RUNTIME_RATES["V2"]
        m = usage.MODEL_RATES_PER_MTOK
        notes.append(f"Rates (USD): V1 ${r1['vcpu_hour']}/vCPU-h and ${r1['gb_hour']}/GB-h; V2 ${r2['vcpu_hour']}/vCPU-h "
                     f"and ${r2['gb_hour']}/GB-h (consumption, from the AgentCore pricing page examples, US East); "
                     f"Claude Haiku 4.5 ${m['input']:.2f}/${m['output']:.2f} per million input/output tokens. "
                     f"Tokens: V1 {cost['V1']['tokens_in']:,} in / {cost['V1']['tokens_out']:,} out; "
                     f"V2 {cost['V2']['tokens_in']:,} in / {cost['V2']['tokens_out']:,} out.")
    elif not usage_data:
        notes.append("Deploy time, memory, CPU and cost weren't collected for this report.")
    return ("<table><tr><th>Metric</th><th>V1</th><th>V2</th><th>How it's measured</th></tr>" + body + "</table>"
            + "".join(f'<p class="note">{html.escape(n)}</p>' for n in notes))


def build_html(rows: list[dict], run_id: str, runtimes: dict, missing: int, usage_data: dict | None = None) -> str:
    questions = [q for _, q in sorted({(int(r["q"]), r["question"]) for r in rows})]
    levels = sorted({int(r["concurrency"]) for r in rows if r["scenario"] == "concurrent"})
    scen = [("Cold start (new session per call)", "cold", None),
            ("Warm (same session reused)", "warm", None)]
    scen += [(f"Concurrent cold starts x{l}", "concurrent", l) for l in levels]

    def lat(sc, v, l=None):
        return _stats(_pick(rows, sc, v, l), "latency_ms")

    def ovh(sc, v, l=None):
        return _stats(_pick(rows, sc, v, l), "overhead_ms")

    c1, c2 = lat("cold", "V1"), lat("cold", "V2")
    o1, o2 = ovh("cold", "V1"), ovh("cold", "V2")
    w1, w2 = lat("warm", "V1"), lat("warm", "V2")
    top = max(levels) if levels else None
    t1, t2 = (_pick(rows, "concurrent", v, top) for v in VERSIONS) if top else ([], [])
    tl1, tl2 = (lat("concurrent", v, top) for v in VERSIONS) if top else (None, None)
    speedup = (1 - _get(c2, "p50") / _get(c1, "p50")) * 100 if c1 and c2 else None

    cards = f"""
<div class="cards">
 <div class="card g"><div class="big">{_sec(_get(c1,'p50'))} → {_sec(_get(c2,'p50'))}</div>
  <div class="sub">Cold start P50, V1 → V2. {f'{speedup:.0f}% faster on V2.' if speedup is not None else ''}</div></div>
 <div class="card g"><div class="big">{_sec(_get(o1,'p50'))} → {_sec(_get(o2,'p50'))}</div>
  <div class="sub">Startup overhead P50 on a new session (client latency minus the agent's own work).</div></div>
 <div class="card b"><div class="big">{_sec(_get(w1,'p50'))} / {_sec(_get(w2,'p50'))}</div>
  <div class="sub">Warm P50, V1 / V2. Once a session is running, both versions serve at the same speed.</div></div>
 <div class="card b"><div class="big">{sum(_ok(r) for r in t1)}/{len(t1)} · {sum(_ok(r) for r in t2)}/{len(t2)}</div>
  <div class="sub">Successful calls in the x{top} burst, V1 · V2. P50 {_sec(_get(tl1,'p50'))} / {_sec(_get(tl2,'p50'))}.</div></div>
</div>"""

    comp = ["<tr><th>Scenario</th><th>Calls OK (V1 / V2)</th><th>V1 P50</th><th>V2 P50</th><th>V2 vs V1</th>"
            "<th>V1 P95</th><th>V2 P95</th><th>V1 overhead P50</th><th>V2 overhead P50</th></tr>"]
    for label, sc, l in scen:
        g1, g2 = _pick(rows, sc, "V1", l), _pick(rows, sc, "V2", l)
        a, b = lat(sc, "V1", l), lat(sc, "V2", l)
        oa, ob = ovh(sc, "V1", l), ovh(sc, "V2", l)
        comp.append(
            f"<tr><td>{label}</td><td>{sum(map(_ok, g1))}/{len(g1)} · {sum(map(_ok, g2))}/{len(g2)}</td>"
            f"<td class=n>{_ms(_get(a,'p50'))}</td><td class=n>{_ms(_get(b,'p50'))}</td>"
            f"<td class=n>{_delta(_get(a,'p50'), _get(b,'p50'))}</td>"
            f"<td class=n>{_ms(_get(a,'p95'))}</td><td class=n>{_ms(_get(b,'p95'))}</td>"
            f"<td class=n>{_ms(_get(oa,'p50'))}</td><td class=n>{_ms(_get(ob,'p50'))}</td></tr>")

    detail = ["<tr><th>Scenario</th><th>Runtime</th><th>OK / calls</th><th>Min</th><th>P50</th><th>P75</th>"
              "<th>P95</th><th>Max</th><th>Mean</th><th>Server P50</th><th>Overhead P50</th></tr>"]
    for label, sc, l in scen:
        for v in VERSIONS:
            g = _pick(rows, sc, v, l)
            a, s, o = lat(sc, v, l), _stats(g, "server_ms"), ovh(sc, v, l)
            detail.append(
                f"<tr><td>{label}</td><td>{v}</td><td>{sum(map(_ok, g))}/{len(g)}</td>"
                + "".join(f"<td class=n>{_ms(_get(a, k))}</td>" for k in ("min", "p50", "p75", "p95", "max", "mean"))
                + f"<td class=n>{_ms(_get(s,'p50'))}</td><td class=n>{_ms(_get(o,'p50'))}</td></tr>")

    perq = ["<tr><th>#</th><th>Question</th><th>Cold V1</th><th>Cold V2</th><th>V2 vs V1</th>"
            "<th>Warm V1</th><th>Warm V2</th><th>Cold overhead V1</th><th>Cold overhead V2</th>"
            "<th>Tools called (cold V1)</th><th>Tools called (cold V2)</th></tr>"]
    for i, q in enumerate(questions, 1):
        def one(sc, v, key):
            g = [r for r in _pick(rows, sc, v, q=i) if _ok(r) and r.get(key) not in ("", None)]
            return g[0][key] if g else None

        def num(sc, v, key):
            x = one(sc, v, key)
            return float(x) if x is not None else None
        a, b = num("cold", "V1", "latency_ms"), num("cold", "V2", "latency_ms")
        perq.append(
            f"<tr><td>{i}</td><td>{html.escape(q)}</td><td class=n>{_ms(a)}</td><td class=n>{_ms(b)}</td>"
            f"<td class=n>{_delta(a, b)}</td>"
            f"<td class=n>{_ms(num('warm','V1','latency_ms'))}</td><td class=n>{_ms(num('warm','V2','latency_ms'))}</td>"
            f"<td class=n>{_ms(num('cold','V1','overhead_ms'))}</td><td class=n>{_ms(num('cold','V2','overhead_ms'))}</td>"
            f"<td class=t>{html.escape(_tools_label(one('cold', 'V1', 'tools') or ''))}</td>"
            f"<td class=t>{html.escape(_tools_label(one('cold', 'V2', 'tools') or ''))}</td></tr>")

    traced = [r for r in rows if _ok(r) and r.get("tools")]
    tool_note = ""
    if traced:
        per_v = " · ".join(f"{v} {sum(map(_uses_all, [r for r in traced if r['version'] == v]))}"
                           f"/{sum(r['version'] == v for r in traced)}" for v in VERSIONS)
        tool_note = f'<p class="note">Successful calls that ran all three tools: {per_v}.</p>'

    metrics = metrics_table(rows, usage_data)

    errors = Counter((r["scenario"], str(r["concurrency"]), r["version"], r["error"][:160])
                     for r in rows if not _ok(r) and r["scenario"] != "warm-setup")
    err_rows = "".join(
        f"<tr><td>{sc}{' x' + l if l else ''}</td><td>{v}</td><td class=n>{n}</td><td>{html.escape(e)}</td></tr>"
        for (sc, l, v, e), n in sorted(errors.items()))
    err_html = (f"<table><tr><th>Scenario</th><th>Runtime</th><th>Count</th><th>Error</th></tr>{err_rows}</table>"
                if errors else '<p class="note">No failed calls.</p>')

    started = min(r["started_at"] for r in rows)[:19].replace("T", " ")
    total = sum(1 for r in rows if r["scenario"] != "warm-setup")
    return f"""<!doctype html><html><head><meta charset="utf-8"><title>AgentCore Runtime V1 vs V2</title>
<style>{CSS}</style></head><body><div class="wrap">
<div class="top"><div class="logo">ShellKode<span>.</span></div><div class="tag">AgentCore Runtime benchmark · run {html.escape(run_id)}</div></div>
<h1>AgentCore Runtime V1 vs V2: Strands Weather Agent</h1>
<p class="lead">The same Strands agent (Claude Haiku 4.5, three tools behind AgentCore Gateway and Lambda: geocode_city,
get_forecast and convert_temperature; conversation history in AgentCore Memory) is deployed twice, once on platform
version V1 and once on V2, with the same code package and settings. Each runtime answered ten multi-city questions
that need all three tools
as cold starts, as warm calls on one session, and in concurrent bursts of {', '.join(map(str, levels))} new sessions.
{total} measured calls, region {REGION}, started {started} UTC.</p>
{cards}
<h2>Benchmark metrics</h2>
{metrics}

<h2>V1 vs V2 at a glance</h2>
<table>{''.join(comp)}</table>
<p class="note">Times in ms, measured on the client from request to full answer. Green = V2 at least 5% faster, red = at least 5% slower.</p>

<h2>Per-question latency and tool use</h2>
<table>{''.join(perq)}</table>
<p class="note">One call per question per scenario, so single values vary with the model and the weather API. Compare the summary rows for trends.
Tools are read from the agent's execute_tool spans, in call order.</p>
{tool_note}

<h2>Full statistics by scenario</h2>
<table>{''.join(detail)}</table>

<h2>Failed calls</h2>
{err_html}

<h2>Method and limits</h2>
<ul class="note">
<li>Runtimes: V1 = {html.escape(runtimes.get('V1', '-'))}; V2 = {html.escape(runtimes.get('V2', '-'))}.</li>
<li>Request path: client → AgentCore Runtime → Strands agent → Bedrock (application inference profile, Claude Haiku 4.5) and AgentCore Gateway → Lambda → Open-Meteo.</li>
<li>Session IDs are new UUIDs per cold call and per burst call. Warm calls reuse one UUID per runtime; its first (setup) call is excluded.</li>
<li>Server time is the agent's <code>POST /invocations</code> span from AgentCore Observability. Overhead = client latency − server time, which covers session startup and network between this client and {REGION}. Calls without a matched span: {missing}.</li>
<li>Bursts send all calls at once from one client. AgentCore's quota in this account is 25 new sessions per second, so the x50 bursts can be throttled; throttled calls are listed as failures, not retried.</li>
<li>Bedrock throttling inside the agent is retried by Strands, so it shows up as longer server time, not as a failure.</li>
<li>The agent keeps conversation history in AgentCore Memory per session, so warm calls carry the earlier turns of their session and send more tokens as the session grows.</li>
<li>Small samples: 10 calls per sequential scenario. P50 is a usable comparison; P75, P95 and Max are rough.</li>
</ul>
</div></body></html>"""


def write_csv(rows: list[dict], path: Path) -> None:
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in FIELDS})


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--levels", default="1,10,50", help="burst sizes for concurrent cold starts")
    ap.add_argument("--gap", type=int, default=120, help="seconds to pause before each burst")
    ap.add_argument("--span-wait", type=int, default=150, help="seconds to wait for spans to arrive")
    ap.add_argument("--analyze", type=Path, help="existing CSV: redo the span lookup and report")
    ap.add_argument("--rerun-burst", type=int, metavar="N",
                    help="with --analyze: rerun the xN burst on both runtimes and replace its rows in that CSV")
    a = ap.parse_args()

    if a.analyze:
        path = a.analyze
        with path.open(encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        run_id = rows[0]["run_id"]
        if a.rerun_burst:
            rows = rerun_burst(rows, a.rerun_burst, a.gap)
            write_csv(rows, path)
            print(f"\nwaiting {a.span_wait}s for spans...", flush=True)
            time.sleep(a.span_wait)
    else:
        RESULTS.mkdir(exist_ok=True)
        rows, run_id = run_suite([int(x) for x in a.levels.split(",")], a.gap)
        path = RESULTS / f"{run_id}.csv"
        write_csv(rows, path)
        print(f"\nwaiting {a.span_wait}s for spans...", flush=True)
        time.sleep(a.span_wait)

    missing = attach_span_data(rows, fetch_spans(rows))
    write_csv(rows, path)
    usage_data = usage.collect(rows)
    report = path.with_suffix(".html")
    report.write_text(build_html(rows, run_id, runtime_info(), missing, usage_data), encoding="utf-8")
    print(f"\nraw: {path}\nreport: {report}")
    if usage_data["partial"]:
        print(f"memory and cost are partial; rerun --analyze after {usage_data['settled_at']:%H:%M} UTC")


if __name__ == "__main__":
    main()
