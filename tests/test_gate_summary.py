"""FIX 4 (product review panel FAIL): "'a gate that fires often will be disabled' -- presented
as the deciding rationale for cancelling Phase 6, with zero cited firing-rate or disablement
data." This is the instrument: `abb gate-summary` reads the existing audit log and reports the
numbers the panel said were missing, so the next person deciding whether the gate is too noisy
or too quiet has a number instead of an aphorism.

Exercises the CLI command directly via click's CliRunner against a hand-written JSONL audit log
-- no live hub needed, matching this command's own read-only, offline contract.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from click.testing import CliRunner

from amplifier_browser_bridge.cli import main


def _write_audit_log(path: Path, records: list[dict[str, Any]]) -> None:
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")


def test_gate_summary_reports_firing_redemption_and_escalation_counts(tmp_path: Any) -> None:
    audit_path = tmp_path / "abb-audit.jsonl"
    _write_audit_log(
        audit_path,
        [
            {"event": "policy_gated", "category": "permission_change", "escalation_locked": True},
            {"event": "policy_gated", "category": "permission_change", "escalation_locked": True},
            {"event": "policy_gated", "category": "delete", "escalation_locked": False},
            {"event": "policy_confirmed", "category": "delete"},
            {"event": "policy_confirmation_expired", "category": "permission_change"},
            {"event": "policy_confirmation_wrong_channel", "category": "permission_change"},
            {"event": "policy_scope_denied"},
            {"event": "policy_scope_denied"},
            {"event": "policy_unclassified"},
            {"event": "device_connected"},  # irrelevant event -- must be ignored, not error
        ],
    )
    # Malformed line -- must be skipped, not crash. Appended raw (not valid JSON),
    # separate from the typed helper above.
    with open(audit_path, "a", encoding="utf-8") as f:
        f.write("not even json\n")

    runner = CliRunner()
    result = runner.invoke(main, ["gate-summary", "--audit-log", str(audit_path)])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)

    assert payload["gate_fired_total"] == 3
    assert payload["gate_fired_by_category"] == {"permission_change": 2, "delete": 1}
    assert payload["escalation_locked_count"] == 2
    assert payload["redeemed_count"] == 1
    assert payload["expired_unredeemed_count"] == 1
    assert payload["wrong_channel_refused_count"] == 1
    # 3 fired - 1 redeemed - 1 expired - 1 wrong-channel = 0 still outstanding
    assert payload["outstanding_or_abandoned_count"] == 0
    assert payload["scope_denied_count"] == 2
    assert payload["unclassified_count"] == 1


def test_gate_summary_missing_audit_log_fails_loud(tmp_path: Any) -> None:
    runner = CliRunner()
    result = runner.invoke(main, ["gate-summary", "--audit-log", str(tmp_path / "nope.jsonl")])
    assert result.exit_code != 0
    assert "not found" in result.output


def test_gate_summary_empty_log_reports_all_zeros(tmp_path: Any) -> None:
    audit_path = tmp_path / "abb-audit.jsonl"
    audit_path.write_text("", encoding="utf-8")

    runner = CliRunner()
    result = runner.invoke(main, ["gate-summary", "--audit-log", str(audit_path)])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["gate_fired_total"] == 0
    assert payload["gate_fired_by_category"] == {}
    assert payload["escalation_locked_count"] == 0
    assert payload["redeemed_count"] == 0
    assert payload["outstanding_or_abandoned_count"] == 0
