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
