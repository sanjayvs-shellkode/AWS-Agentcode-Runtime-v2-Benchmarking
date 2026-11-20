import questions


def _row(scenario, version, q, latency, server, ok=True, concurrency="", error=""):
    return {"run_id": "t", "scenario": scenario, "concurrency": concurrency, "version": version, "q": q,
            "question": questions.QUESTIONS[q - 1], "session_id": f"{scenario}{version}{q}{concurrency}",
            "started_at": "2026-11-20T00:00:00+00:00", "latency_ms": latency, "ok": ok, "error": error,
            "server_ms": server, "overhead_ms": latency - server}


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
    assert questions.html.escape("Is it a good time for a walk in Sydney") in page
