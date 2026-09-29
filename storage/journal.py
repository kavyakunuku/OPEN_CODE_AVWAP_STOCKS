"""Trade journal: permanent, append-only audit log of every significant event."""
from __future__ import annotations

import json
import logging
from typing import Optional

log = logging.getLogger("avwap.journal")


class Journal:
    def __init__(self, db):
        self.db = db

    def write(
        self,
        event: str,
        *,
        ts: int,
        position_id: Optional[str] = None,
        security_id: Optional[str] = None,
        symbol: Optional[str] = None,
        detail: Optional[dict] = None,
        level: int = logging.INFO,
    ) -> None:
        try:
            self.db._exec(
                "INSERT INTO journal (ts, position_id, security_id, symbol, event, detail) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (ts, position_id, security_id, symbol, event,
                 json.dumps(detail or {}, default=str)),
            )
        except Exception:  # journal must never take the system down
            log.exception("journal write failed for event %s", event)

    def tail(self, limit: int = 200, since_ts: int = 0):
        rows = self.db._query(
            "SELECT * FROM journal WHERE ts>=? ORDER BY id DESC LIMIT ?",
            (since_ts, limit),
        )
        out = []
        for r in rows:
            d = dict(r)
            try:
                d["detail"] = json.loads(d.get("detail") or "{}")
            except (TypeError, ValueError):
                d["detail"] = {}
            out.append(d)
        return out
