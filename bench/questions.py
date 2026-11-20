"""Ten-question V1 vs V2 benchmark (LLM mode) with an HTML report.

For each runtime, with the same ten single-city weather questions:
  cold        - each question once, each on a new session (new microVM)
  warm        - one session per runtime: a setup call, then each question once on that session
  concurrent  - bursts of 1, 10 and 50 new sessions sent at the same time (questions cycled)

V1 and V2 bursts run one after the other, never together, with a pause before each burst.
No client retries, so throttled calls show up as failures instead of longer latencies.

Usage (repo root, AWS_PROFILE and AWS_REGION set):
  python bench/questions.py
  python bench/questions.py --analyze bench/results/<file>.csv   # redo spans and the report
"""
import argparse
import csv
import html
import time
import uuid
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import boto3
from botocore.config import Config

from run import REGION, VERSIONS, attach_server_times, invoke, runtime_arns, span_lookup, stats

RESULTS = Path(__file__).resolve().parent / "results"
QUESTIONS = [
    "What's the weather in Paris right now?",
    "What's the current temperature in Mumbai?",
    "Is it raining in London at the moment?",
    "How windy is it in Chennai right now?",
    "What's the humidity in Singapore right now?",
    "Should I carry an umbrella in Bangalore today?",
    "Is it cloudy in Tokyo right now?",
    "How hot is it in Dubai at the moment?",
    "What's the weather like in New York right now?",
    "Is it a good time for a walk in Sydney, based on the current weather?",
]
FIELDS = ["run_id", "scenario", "concurrency", "version", "q", "question", "session_id",
          "started_at", "latency_ms", "ok", "error", "server_ms", "overhead_ms"]


def order(i: int) -> tuple[str, str]:
    # Alternate which runtime goes first so neither always runs second.
    return VERSIONS if i % 2 == 0 else VERSIONS[::-1]


def run_suite(levels: list[int], gap: int) -> tuple[list[dict], str]:
    arns = runtime_arns()
    client = boto3.Session(region_name=REGION).client("bedrock-agentcore", config=Config(
        read_timeout=300, retries={"total_max_attempts": 1}, max_pool_connections=max(levels) + 10))
    run_id = datetime.now().strftime("%Y%m%d-%H%M%S") + "-questions"
    rows: list[dict] = []

    def call(scenario: str, version: str, qi: int, session_id: str, concurrency="") -> dict:
        r = invoke(client, arns[version], session_id, {"prompt": QUESTIONS[qi]})
        return {"run_id": run_id, "scenario": scenario, "concurrency": concurrency, "version": version,
                "q": qi + 1, "question": QUESTIONS[qi], "session_id": session_id, **r}

    def keep(row: dict) -> None:
        rows.append(row)
        status = "ok" if row["ok"] else "FAIL " + row["error"][:80]
        print(f"{row['scenario']:<10} {row['version']} q{row['q']:<2} {row['latency_ms']:>8.0f} ms  {status}",
              flush=True)

    for qi in range(len(QUESTIONS)):
        for v in order(qi):
            keep(call("cold", v, qi, str(uuid.uuid4())))
    warm = {v: str(uuid.uuid4()) for v in VERSIONS}
    for v in VERSIONS:
        keep(call("warm-setup", v, 0, warm[v]))
    for qi in range(len(QUESTIONS)):
        for v in order(qi):
            keep(call("warm", v, qi, warm[v]))

    for n, level in enumerate(levels):
        for v in order(n):
            print(f"pausing {gap}s before the {v} burst of {level}...", flush=True)
            time.sleep(gap)
            with ThreadPoolExecutor(max_workers=level) as pool:
                futures = [pool.submit(call, "concurrent", v, j % len(QUESTIONS), str(uuid.uuid4()), level)
                           for j in range(level)]
                burst = [f.result() for f in futures]
            rows.extend(burst)
            ok = [b["latency_ms"] for b in burst if b["ok"]]
            s = stats(ok)
            print(f"concurrent x{level:<2} {v}: {len(ok)}/{level} ok"
                  + (f", P50 {s['p50']:.0f} ms, max {s['max']:.0f} ms" if s else ""), flush=True)
    return rows, run_id


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
.good{color:#15803d;font-weight:700}.bad{color:#b91c1c;font-weight:700}.flat{color:#6b7280}
.note{font-size:12px;color:#4b5563;line-height:1.55}
ul.note{padding-left:18px;margin:6px 0}
"""


def build_html(rows: list[dict], run_id: str, runtimes: dict, missing: int) -> str:
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
            "<th>V1 P90</th><th>V2 P90</th><th>V1 overhead P50</th><th>V2 overhead P50</th></tr>"]
    for label, sc, l in scen:
        g1, g2 = _pick(rows, sc, "V1", l), _pick(rows, sc, "V2", l)
        a, b = lat(sc, "V1", l), lat(sc, "V2", l)
        oa, ob = ovh(sc, "V1", l), ovh(sc, "V2", l)
        comp.append(
            f"<tr><td>{label}</td><td>{sum(map(_ok, g1))}/{len(g1)} · {sum(map(_ok, g2))}/{len(g2)}</td>"
            f"<td class=n>{_ms(_get(a,'p50'))}</td><td class=n>{_ms(_get(b,'p50'))}</td>"
            f"<td class=n>{_delta(_get(a,'p50'), _get(b,'p50'))}</td>"
            f"<td class=n>{_ms(_get(a,'p90'))}</td><td class=n>{_ms(_get(b,'p90'))}</td>"
            f"<td class=n>{_ms(_get(oa,'p50'))}</td><td class=n>{_ms(_get(ob,'p50'))}</td></tr>")

    detail = ["<tr><th>Scenario</th><th>Runtime</th><th>OK / calls</th><th>Min</th><th>P50</th><th>P90</th>"
              "<th>Max</th><th>Mean</th><th>Server P50</th><th>Overhead P50</th></tr>"]
    for label, sc, l in scen:
        for v in VERSIONS:
            g = _pick(rows, sc, v, l)
            a, s, o = lat(sc, v, l), _stats(g, "server_ms"), ovh(sc, v, l)
            detail.append(
                f"<tr><td>{label}</td><td>{v}</td><td>{sum(map(_ok, g))}/{len(g)}</td>"
                + "".join(f"<td class=n>{_ms(_get(a, k))}</td>" for k in ("min", "p50", "p90", "max", "mean"))
                + f"<td class=n>{_ms(_get(s,'p50'))}</td><td class=n>{_ms(_get(o,'p50'))}</td></tr>")

    perq = ["<tr><th>#</th><th>Question</th><th>Cold V1</th><th>Cold V2</th><th>V2 vs V1</th>"
            "<th>Warm V1</th><th>Warm V2</th><th>Cold overhead V1</th><th>Cold overhead V2</th></tr>"]
    for i, q in enumerate(QUESTIONS, 1):
        def one(sc, v, key):
            g = [r for r in _pick(rows, sc, v, q=i) if _ok(r) and r[key] not in ("", None)]
            return float(g[0][key]) if g else None
        a, b = one("cold", "V1", "latency_ms"), one("cold", "V2", "latency_ms")
        perq.append(
            f"<tr><td>{i}</td><td>{html.escape(q)}</td><td class=n>{_ms(a)}</td><td class=n>{_ms(b)}</td>"
            f"<td class=n>{_delta(a, b)}</td>"
            f"<td class=n>{_ms(one('warm','V1','latency_ms'))}</td><td class=n>{_ms(one('warm','V2','latency_ms'))}</td>"
            f"<td class=n>{_ms(one('cold','V1','overhead_ms'))}</td><td class=n>{_ms(one('cold','V2','overhead_ms'))}</td></tr>")

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
<p class="lead">The same Strands agent (Claude Haiku 4.5, two weather tools behind AgentCore Gateway and Lambda) is deployed twice,
once on platform version V1 and once on V2, with the same code package and settings. Each runtime answered ten single-city weather
questions as cold starts, as warm calls on one session, and in concurrent bursts of {', '.join(map(str, levels))} new sessions.
{total} measured calls, region {REGION}, started {started} UTC.</p>
{cards}
<h2>V1 vs V2 at a glance</h2>
<table>{''.join(comp)}</table>
<p class="note">Times in ms, measured on the client from request to full answer. Green = V2 at least 5% faster, red = at least 5% slower.</p>

<h2>Per-question latency</h2>
<table>{''.join(perq)}</table>
<p class="note">One call per question per scenario, so single values vary with the model and the weather API. Compare the summary rows for trends.</p>

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
<li>Small samples: 10 calls per sequential scenario. P50 is a usable comparison; P90 and Max are rough.</li>
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
    a = ap.parse_args()

    if a.analyze:
        path = a.analyze
        with path.open(encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        run_id = rows[0]["run_id"]
    else:
        RESULTS.mkdir(exist_ok=True)
        rows, run_id = run_suite([int(x) for x in a.levels.split(",")], a.gap)
        path = RESULTS / f"{run_id}.csv"
        write_csv(rows, path)
        print(f"\nwaiting {a.span_wait}s for spans...", flush=True)
        time.sleep(a.span_wait)

    missing = attach_server_times(rows, span_lookup(rows))
    write_csv(rows, path)
    report = path.with_suffix(".html")
    report.write_text(build_html(rows, run_id, runtime_info(), missing), encoding="utf-8")
    print(f"\nraw: {path}\nreport: {report}")


if __name__ == "__main__":
    main()
