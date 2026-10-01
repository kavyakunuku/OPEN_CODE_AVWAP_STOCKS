"""Contract universe management.

Implements spec §4 (expiry selection), §5 (ATM), §6 (ATM + N ITM per
side, default N=4 - configurable via strategy.itm_strikes_per_side),
§7 (dynamic ATM + open-position universe takes priority), §30
(scanner vs position monitor are SEPARATE).

Each underlying carries a single monthly expiry leg (actual exchange
dates; the 24th-of-month switch is a premium-preservation rule, never
an assumed calendar date).
ATM + ATM±ITM strikes are computed PER EXPIRY LEG from that leg's own chain
strikes (strike grids differ between expiries).

The "monthly expiry" is identified from ACTUAL exchange/Dhan expiry dates
(the furthest expiry date in a calendar month), never assumed to be a fixed
calendar date.
"""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from datetime import date
from typing import Optional

log = logging.getLogger("avwap.market.universe")


# ---------------------------------------------------------------------------
# Expiry selection
# ---------------------------------------------------------------------------
def select_monthly_expiry(
    today: date,
    expiries: list[date],
    switch_day: int = 24,
) -> Optional[date]:
    """Pick the expiry per the strategy rule:

        1st .. (switch_day-1)  -> current month's monthly expiry
        switch_day .. end      -> next month's monthly expiry

    "monthly expiry of month M" = the furthest expiry date that falls in M.
    If the chosen month has no expiries, or the current month's expiry has
    already passed, fall forward to the next available month.
    """
    expiries = sorted({e for e in expiries if e >= today})
    if not expiries:
        return None

    cur_month = (today.year, today.month)
    nxt_month = (today.year + 1, 1) if today.month == 12 else (today.year, today.month + 1)

    cur = [e for e in expiries if (e.year, e.month) == cur_month]
    nxt = [e for e in expiries if (e.year, e.month) == nxt_month]

    if today.day < switch_day and cur:
        return max(cur)
    if nxt:
        return max(nxt)
    if cur:
        return max(cur)
    # no expiry in this or next month: nearest future expiry (warn)
    log.warning(
        "No expiry in current or next month (today=%s, expiries=%s); "
        "falling back to nearest future expiry %s",
        today, [str(e) for e in expiries], expiries[0],
    )
    return expiries[0]


def select_weekly_expiries(today: date, expiries: list[date], count: int = 2) -> list[date]:
    """Pick the next `count` weekly expiries starting from the current one:
    the N nearest upcoming expiry dates (>= today). E.g. on a Tuesday
    between weekly expiries this is [this week's expiry, next week's]."""
    upcoming = sorted({e for e in expiries if e >= today})
    return upcoming[:max(0, int(count))]


# ---------------------------------------------------------------------------
# ATM + strike selection
# ---------------------------------------------------------------------------
def find_atm(spot: float, strikes: list[float]) -> Optional[float]:
    """ATM = the AVAILABLE strike closest to spot. Ties -> lower strike.

    Determined from real chain strikes, never from an assumed step.
    """
    if not strikes or spot <= 0:
        return None
    return min(strikes, key=lambda s: (abs(s - spot), s))


def ce_universe_strikes(atm: float, strikes: list[float], it_count: int = 4) -> list[float]:
    """CALL side: ATM + up to `it_count` ITM strikes (strikes BELOW ATM),
    in descending order, from the available chain strikes only."""
    if atm not in set(strikes):
        return []
    it = sorted([s for s in strikes if s < atm], reverse=True)[:it_count]
    return [atm] + it


def pe_universe_strikes(atm: float, strikes: list[float], it_count: int = 4) -> list[float]:
    """PUT side: ATM + up to `it_count` ITM strikes (strikes ABOVE ATM),
    in ascending order."""
    if atm not in set(strikes):
        return []
    it = sorted([s for s in strikes if s > atm])[:it_count]
    return [atm] + it


# ---------------------------------------------------------------------------
# Per-underlying universe (multi-expiry)
# ---------------------------------------------------------------------------
@dataclass
class ExpiryUniverse:
    """One expiry leg of an underlying (monthly leg, or one weekly leg)."""
    expiry: Optional[date]
    atm: Optional[float] = None
    strikes: list[float] = field(default_factory=list)
    ce_strikes: list[float] = field(default_factory=list)
    pe_strikes: list[float] = field(default_factory=list)
    contracts_by_key: dict = field(default_factory=dict)  # (strike, type) -> OptionContract

    def scanner_contracts(self) -> list:
        out = []
        for s in self.ce_strikes:
            c = self.contracts_by_key.get((s, "CE"))
            if c:
                out.append(c)
        for s in self.pe_strikes:
            c = self.contracts_by_key.get((s, "PE"))
            if c:
                out.append(c)
        return out


@dataclass
class UnderlyingUniverse:
    """ALL expiry legs of one underlying (spot is shared across legs)."""
    underlying: str
    spot: Optional[float] = None
    # iso expiry string -> ExpiryUniverse, insertion-ordered (oldest first)
    expiries: dict = field(default_factory=dict)

    @property
    def primary(self) -> Optional[ExpiryUniverse]:
        """First (earliest) leg - used for info display / spot refresh."""
        if not self.expiries:
            return None
        return self.expiries[sorted(self.expiries)[0]]

    def scanner_contracts(self) -> list:
        out = []
        for e in self.expiries.values():
            out.extend(e.scanner_contracts())
        return out

    def to_info(self) -> list[dict]:
        """One dashboard row per expiry leg."""
        return [
            {
                "underlying": self.underlying,
                "expiry": str(e.expiry) if e.expiry else None,
                "spot": self.spot,
                "atm": e.atm,
                "n_strikes": len(e.strikes),
                "n_scanner_contracts": len(e.scanner_contracts()),
            }
            for e in self.expiries.values()
        ]


class UniverseManager:
    """Holds the ENTRY SCANNER UNIVERSE and is aware of the OPEN POSITION
    universe. Monitored set = scanner ∪ open positions (spec §7, §30).

    Each underlying carries a single monthly expiry leg; ATM / ATM±ITM are tracked per leg.
    """

    def __init__(self, it_count: int = 4):
        self.it_count = it_count
        self.by_underlying: dict[str, UnderlyingUniverse] = {}
        self.position_security_ids: set[str] = set()
        # None means legacy ATM/ITM candidates; a concrete set (including an
        # empty set) is the opportunity scanner's new-entry allow-list.
        self._eligible_scanner_ids: Optional[set[str]] = None
        # the bootstrap/main thread mutates the universe while the dashboard
        # thread reads it - serialize structural access
        self._lock = threading.RLock()

    # -------------------------------------------------------------- update
    def update(
        self,
        underlying: str,
        expiry: Optional[date],
        spot: Optional[float],
        strikes: list[float],
        contracts_by_key: dict,
    ) -> Optional[UnderlyingUniverse]:
        """Register/refresh ONE expiry leg for the underlying (calling this
        again with a different expiry adds a second leg)."""
        with self._lock:
            u = self.by_underlying.get(underlying) or UnderlyingUniverse(underlying=underlying)
            u.spot = spot
            key = str(expiry)
            e = u.expiries.get(key) or ExpiryUniverse(expiry=expiry)
            e.strikes = sorted(set(strikes))
            e.contracts_by_key = contracts_by_key
            e.atm = find_atm(spot, e.strikes) if spot else None
            e.ce_strikes = ce_universe_strikes(e.atm, e.strikes, self.it_count) if e.atm else []
            e.pe_strikes = pe_universe_strikes(e.atm, e.strikes, self.it_count) if e.atm else []
            u.expiries[key] = e
            self.by_underlying[underlying] = u
            return u

    def refresh_atm(self, underlying: str, spot: float) -> Optional[UnderlyingUniverse]:
        """DYNAMIC ATM: recompute ATM + scanner strikes for EVERY expiry leg
        as spot moves. This only changes NEW-ENTRY candidates. Open
        positions keep being monitored via position_security_ids (spec §7)."""
        with self._lock:
            u = self.by_underlying.get(underlying)
            if u is None:
                return None
            u.spot = spot
            for e in u.expiries.values():
                e.atm = find_atm(spot, e.strikes)
                e.ce_strikes = ce_universe_strikes(e.atm, e.strikes, self.it_count)
                e.pe_strikes = pe_universe_strikes(e.atm, e.strikes, self.it_count)
            return u

    def get(self, underlying: str) -> Optional[UnderlyingUniverse]:
        with self._lock:
            return self.by_underlying.get(underlying)

    def drop_expiries(self, underlying: str, keep: set[str]) -> list[str]:
        """Remove expiry legs NOT in `keep` (iso strings) - called after a
        refresh when the selected leg set changed (expiry passed, weekly roll,
        24th switch). Open positions on dropped legs keep being monitored
        via position_security_ids (spec §7). Returns removed iso expiries."""
        with self._lock:
            u = self.by_underlying.get(underlying)
            if u is None:
                return []
            removed = [k for k in u.expiries if k not in keep]
            for k in removed:
                del u.expiries[k]
            return removed

    # ----------------------------------------------------------- monitoring
    def add_position_id(self, security_id: str) -> None:
        with self._lock:
            self.position_security_ids.add(security_id)

    def remove_position_id(self, security_id: str) -> None:
        with self._lock:
            self.position_security_ids.discard(security_id)

    def set_eligible_scanner_ids(self, security_ids: Optional[set[str] | list[str] | tuple[str, ...]]) -> None:
        """Replace the new-entry allow-list without changing position monitoring.

        ``None`` disables the opportunity filter and restores the legacy
        ATM/ITM candidate set. An empty collection is an active fail-closed
        filter: no new contract is eligible, while open positions stay
        monitored through :meth:`monitored_ids`.
        """
        with self._lock:
            self._eligible_scanner_ids = (
                None if security_ids is None
                else {str(security_id) for security_id in security_ids if str(security_id)}
            )

    def scanner_ids(self) -> set[str]:
        with self._lock:
            if self._eligible_scanner_ids is not None:
                return set(self._eligible_scanner_ids)
            ids = set()
            for u in self.by_underlying.values():
                for c in u.scanner_contracts():
                    ids.add(c.security_id)
            return ids

    def monitored_ids(self) -> set[str]:
        """ENTRY SCANNER UNIVERSE + OPEN POSITION UNIVERSE (union).
        The position universe always takes priority for exit monitoring."""
        with self._lock:
            return self.scanner_ids() | set(self.position_security_ids)

    def all_scanner_contracts(self) -> list:
        with self._lock:
            out = []
            for u in self.by_underlying.values():
                out.extend(u.scanner_contracts())
            return out

    def info(self) -> list[dict]:
        with self._lock:
            rows: list[dict] = []
            for u in sorted(self.by_underlying.values(), key=lambda x: x.underlying):
                rows.extend(u.to_info())
            return rows
