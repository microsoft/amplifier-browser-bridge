# blfj — Hermetic doctor stopped-service test

## Result

`test_doctor_service_status_fails_and_skips_downstream_when_locally_stopped`
now patches the `HubClient.list_devices()` reachability probe to raise
`HubError("connection refused")`, as well as patching the local
`describe_service()` record. The test therefore exercises only the intended
"locally stopped and unreachable" branch; it no longer depends on whether a
real hub happens to listen on port 8900.

The separate `test_doctor_reachability_overrides_a_stale_service_stopped_record`
continues to cover the deliberate production behavior: successful reachability
overrides a stale local stopped-service record.

## Evidence

- Host with a real hub listener on `0.0.0.0:8900`: focused test passed
  (`1 passed in 1.71s`).
- Isolated `docker run --network none` container, which verified no listener
  on port 8900: focused test passed (`1 passed in 0.09s`).
- `uv run ruff format --check .`: passed (`155 files already formatted`).
- `uv run ruff check .`: passed.
- `uv run pyright --venvpath . src tests`: passed (`0 errors`).
- `uv run pytest tests/ -v`: passed (`884 passed`).
- `uv run pytest modules/tool-browser-bridge/tests/ -v`: passed (`40 passed`).

## Authority

$0 spent. This is a test-only isolation change; no API measurement was run.

## Landing stage

This change is submitted as a **draft PR**. It is not merged; merging is the
manager's next stage.