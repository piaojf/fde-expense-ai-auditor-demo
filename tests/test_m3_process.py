"""Hard gate uses actual terminated/restarted uvicorn processes, not just TestClient."""

from scripts.smoke_m3 import run_gates


def test_real_fastapi_process_restart_and_idempotency_hard_gates():
    cases = run_gates()
    assert all(case["status"] == "PASS" for case in cases.values())
    assert cases["TIMEOUT_ALWAYS"]["process_restart"] == "PASS"
    assert cases["LOST_RESPONSE_ALWAYS"]["after"]["actual_write_count"] == 1
