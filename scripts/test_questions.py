import questions

TOOLS = "geocode_city geocode_city get_forecast get_forecast convert_temperature"


def _row(scenario, version, q, latency, server, ok=True, concurrency="", error="", tools=""):
    return {"run_id": "t", "scenario": scenario, "concurrency": concurrency,
            "version": version, "q": q, "question": questions.QUESTIONS[q - 1],
            "session_id": f"{scenario}{version}{q}{concurrency}", "started_at": "2026-11-20T00:00:00+00:00",
            "latency_ms": latency, "ok": ok, "error": error, "server_ms": server,
            "overhead_ms": latency - server, "tools": tools}


def test_report_shows_cold_speedup_and_burst_failures():
    rows = [_row("cold", "V1", q, 9000.0, 4000.0) for q in range(1, 11)]
    rows += [_row("cold", "V2", q, 6000.0, 4000.0) for q in range(1, 11)]
    rows += [_row("warm", v, q, 4100.0, 4000.0) for v in ("V1", "V2") for q in range(1, 11)]
    rows += [_row("concurrent", "V1", 1, 9500.0, 4000.0, concurrency=10)]
    rows += [_row("concurrent", "V2", 1, 0.0, 0.0, ok=False, concurrency=10, error="ThrottlingException: slow down")]
    page = questions.build_html(rows, "t", {"V1": "rt-v1", "V2": "rt-v2"}, missing=0)
    assert "9.00 s → 6.00 s" in page               # cold P50 card
    assert "33% faster on V2" in page
    assert "Concurrent cold starts x10" in page
    assert "ThrottlingException: slow down" in page  # failure listed, not hidden
    assert "1/1 · 0/1" in page                       # burst success counts V1 · V2
    assert questions.html.escape("Rank Tokyo, Sydney and Paris from warmest to coldest") in page


def test_report_shows_tools_per_question_and_all_three_count():
    rows = [_row("cold", "V1", 1, 9000.0, 4000.0, tools=TOOLS),
            _row("cold", "V2", 1, 6000.0, 4000.0, tools="geocode_city get_forecast")]
    page = questions.build_html(rows, "t", {}, missing=0)
    assert "need all three tools" in page
    assert "geocode_city×2, get_forecast×2, convert_temperature" in page
    assert "ran all three tools: V1 1/1 · V2 0/1" in page


def _span(name, start, end, **attrs):
    return {"name": name, "start": start, "end": end, "ms": (end - start) / 1e6, "attrs": attrs}


def test_attach_span_data_splits_one_session_into_calls():
    rows = [_row("warm", "V1", 2, 3000.0, 0.0), _row("warm", "V1", 1, 2000.0, 0.0)]
    rows[0]["started_at"], rows[1]["started_at"] = "2026-11-20T00:00:05+00:00", "2026-11-20T00:00:01+00:00"
    rows[0]["session_id"] = rows[1]["session_id"] = "s"
    spans = [
        _span("POST /invocations", 0, 1_500_000_000),
        _span("execute_tool weather___geocode_city", 100, 200),
        _span("invoke_agent Strands Agents", 50, 1_400_000_000,
              **{"gen_ai.usage.input_tokens": 700, "gen_ai.usage.output_tokens": 80}),
        _span("POST /invocations", 4_000_000_000, 6_000_000_000),
        _span("execute_tool weather___convert_temperature", 4_100_000_000, 4_200_000_000),
        _span("invoke_agent Strands Agents", 4_050_000_000, 5_900_000_000,
              **{"gen_ai.usage.input_tokens": 1500, "gen_ai.usage.output_tokens": 90}),
    ]
    assert questions.attach_span_data(rows, lambda v, s: spans) == 0
    first, second = rows[1], rows[0]
    assert (first["server_ms"], first["overhead_ms"], first["tools"]) == (1500.0, 500.0, "geocode_city")
    assert (second["tools"], second["input_tokens"], second["output_tokens"]) == ("convert_temperature", 1500, 90)


def test_metrics_table_shows_deploy_time_memory_and_cost():
    rows = [dict(_row("cold", v, q, 9000.0 if v == "V1" else 6000.0, 4000.0), input_tokens=2000, output_tokens=300)
            for v in ("V1", "V2") for q in range(1, 11)]
    usage_data = {
        "deploy": {"V1": {"seconds": 13.0, "operation": "update"}, "V2": {"seconds": 225.0, "operation": "update"}},
        "billed": {"V1": {"vcpu_hours": 0.02, "gb_hours": 1.5}, "V2": {"vcpu_hours": 0.03, "gb_hours": 2.0}},
        "partial": False,
    }
    page = questions.metrics_table(rows, usage_data)
    for text in ("13 s (update)", "3 min 45 s (update)", "1.50 GB-h (0.150 per session)", "Cost per task",
                 "9.00 s", "6.00 s", "20,000 in / 3,000 out"):
        assert text in page


def test_rerun_burst_replaces_only_that_burst(monkeypatch):
    old = [_row("cold", "V1", 1, 9000.0, 4000.0),
           _row("concurrent", "V1", 1, 9500.0, 4000.0, concurrency=10),
           _row("concurrent", "V1", 1, 0.0, 0.0, ok=False, concurrency=50, error="EndpointConnectionError")]
    monkeypatch.setattr(questions, "make_caller", lambda run_id, size: "call")
    monkeypatch.setattr(questions, "run_burst",
                        lambda call, v, level, gap: [_row("concurrent", v, 1, 7000.0, 4000.0, concurrency=level)])
    rows = questions.rerun_burst(old, 50, gap=0)
    assert [(r["scenario"], str(r["concurrency"]), r["version"], r["ok"]) for r in rows] == [
        ("cold", "", "V1", True), ("concurrent", "10", "V1", True),
        ("concurrent", "50", "V1", True), ("concurrent", "50", "V2", True)]
