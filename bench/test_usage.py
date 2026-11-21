from datetime import datetime, timedelta, timezone

import usage

T0 = datetime(2026, 11, 21, 10, 0, tzinfo=timezone.utc)


def _ev(seconds, status):
    return {"Timestamp": T0 + timedelta(seconds=seconds), "ResourceStatus": status}


def test_last_operation_takes_newest_update_from_first_in_progress():
    events = [  # newest first, as CloudFormation returns them
        _ev(214, "UPDATE_COMPLETE"), _ev(2, "UPDATE_IN_PROGRESS"), _ev(0, "UPDATE_IN_PROGRESS"),
        _ev(-900, "CREATE_COMPLETE"), _ev(-1200, "CREATE_IN_PROGRESS"),
    ]
    op = usage.last_operation(events)
    assert (op["seconds"], op["operation"]) == (214.0, "update")


def test_last_operation_none_without_a_completed_operation():
    assert usage.last_operation([_ev(0, "UPDATE_IN_PROGRESS")]) is None


def test_cost_summary_divides_runtime_and_model_cost_by_successful_calls():
    rows = [
        {"version": v, "session_id": f"{v}-{i}", "ok": ok, "input_tokens": tin, "output_tokens": tout}
        for v in ("V1", "V2")
        for i, (ok, tin, tout) in enumerate([(True, 2000, 300), (True, 2000, 300), (False, "", "")])
    ]
    billed = {"V1": {"vcpu_hours": 0.01, "gb_hours": 1.0}, "V2": {"vcpu_hours": 0.01, "gb_hours": 1.0}}
    c = usage.cost_summary(rows, billed)["V1"]
    runtime = 0.01 * 0.0895 + 1.0 * 0.00945
    model = (4000 * 1.00 + 600 * 5.00) / 1e6
    assert (c["tasks"], c["sessions"], c["tokens_missing"]) == (2, 3, 0)
    assert abs(c["per_task_usd"] - (runtime + model) / 2) < 1e-12
    assert abs(c["gb_hours_per_session"] - 1 / 3) < 1e-12
