"""Append-only JSONL audit log.

Every command sent, every result received, and every device connect/disconnect is
recorded here. This is the human's after-the-fact visibility into everything the
agent did (design doc §6.3 co-working etiquette: "Full audit log; the human can see
everything the agent did, after the fact.").

Deliberately synchronous. Command volume in this system is bounded by human/agent
interaction speed, not high-frequency event streams -- a blocking append-and-flush
per record is simpler and more obviously correct than an async writer, and there is
no measured need for the latter (ruthless simplicity: don't build for a load that
doesn't exist).

## Policy events (see policy.py, hub.py)

This is the single log both dispatch events and policy decisions are recorded to
-- there is no separate policy log. `event` names to know when reviewing:

    policy_denied                a target matched the denylist; command refused
    policy_tab_hidden            a `tabs` result entry was filtered out (response-
                                  path invisibility -- see policy.py)
    policy_tab_shown_despite_match  an `auth`-category match was NOT hidden because
                                  the tab is discarded (no live renderer -- see
                                  policy.py's `_tab_discarded` docstring, Bug 2 case
                                  study: background tabs frozen mid-way through a
                                  first-party app's silent OAuth session-refresh
                                  redirect through an identity-provider host)
    policy_allowed_despite_match  request-path symmetric case of the above -- a
                                  command targeting a discarded `auth`-category tab
                                  was allowed to reach the device (which still
                                  refuses to act on it unless `wake=true`)
    policy_gated                 an irreversible/world-visible action was detected;
                                  a confirmation token was issued instead of dispatching
    policy_confirmed             a confirmation token was redeemed; the original
                                  command was re-submitted for dispatch
    policy_confirmation_expired  a confirmation token expired (unused) and was purged
    kill_switch_engaged          hub-level stop-all activated
    kill_switch_rejected         one queued (not-yet-dispatched) command rejected
                                  as part of a kill-switch engagement
    kill_switch_disengaged       stop-all lifted

This is the compensating control for broad-by-default access (design doc §6.2):
since most reads/navigations run unprompted, the audit log -- not a approval
dialog -- is what lets the human review, after the fact, everything the agent did
and every policy decision the hub made on its behalf.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .protocol import now_iso


class AuditLog:
    def __init__(self, path: str | Path) -> None:
        self._path = Path(path).expanduser()
        self._path.parent.mkdir(parents=True, exist_ok=True)

    @property
    def path(self) -> Path:
        return self._path

    def record(self, event: str, **fields: Any) -> None:
        line = json.dumps({"ts": now_iso(), "event": event, **fields}, default=str)
        with open(self._path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
            f.flush()
