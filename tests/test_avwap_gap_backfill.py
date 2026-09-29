"""AVWAP correctness deep-dive (user-reported chart mismatch, 2026-09-18).

Three layers of verification:

  1. EXACT MATH - hand-computed cumulative typical-price VWAP values,
     bit-checked against AvwapState.update() (no tolerance games: the
     reference uses Fractions).
  2. INDEPENDENT REFERENCE - a separately-written accumulation loop over
     hundreds of random candles must agree with the production state.
  3. MISSED-DAY RESTART - the app is stopped after day 1 and restarted on
     day 2; day 2's candles MUST still be folded into the cumulative
     AVWAP. (Regression guard: a multi-day downtime used to silently drop
     every missed candle for contracts that already had AVWAP state.)
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, time
from fractions import Fraction

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from common.models import Candle  # noqa: E402
from common.utils import IST, epoch  # noqa: E402
from strategy.avwap import AvwapState  # noqa: E402


def _candle(ts, o, h, l, c, v):
    return Candle(security_id="T", ts=ts, open=o, high=h, low=l, close=c,
                  volume=v)


# ---------------------------------------------------------------- exact math
def test_avwap_exact_math_hand_computed():
    # Candles (ts, open, high, low, close, volume) - chosen so the
    # typical prices are non-trivial but hand-verifiable:
    #   TP1 = (H+L+C)/3 = (10 + 8 + 9) / 3 = 9
    #   TP2 = (12 + 10 + 11) / 3 = 11
    #   TP3 = (7 + 5 + 5) / 3 = 17/3
    c1 = _candle(100, 10.0, 10.0, 8.0, 9.0, 100)
    c2 = _candle(1900, 11.0, 12.0, 10.0, 11.0, 300)
    c3 = _candle(20000, 6.0, 7.0, 5.0, 5.0, 50)

    st = AvwapState(security_id="T")
    assert st.update(c1) == 9.0                       # anchor = first candle
    assert st.anchor_ts == 100
    # after c2: (9*100 + 11*300) / 400 = 4200/400 = 10.5
    assert st.update(c2) == Fraction(21, 2)
    # after c3: (4200 + (17/3)*50) / 450 = (13450/3) / 450 = 269/27
    # (a real data mismatch shows up at ~1e-3 or worse; 1e-12 relative is
    # pure float-rounding territory)
    assert abs(st.update(c3) - 269 / 27) <= 1e-12 * (269 / 27)
    assert st.cumulative_volume == 450
    assert st.last_close == 5.0

    # idempotency: re-feeding the same or an older candle changes nothing
    before = (st.cumulative_price_volume, st.cumulative_volume, st.last_avwap)
    assert st.update(c2) == st.last_avwap
    assert st.update(c1) == st.last_avwap
    assert (st.cumulative_price_volume, st.cumulative_volume, st.last_avwap) == before


def test_avwap_zero_volume_candles_do_not_break():
    st = AvwapState(security_id="T")
    assert st.update(_candle(100, 1.0, 1.0, 1.0, 1.0, 0)) is None  # no volume
    assert st.update(_candle(200, 2.0, 2.0, 2.0, 2.0, 100)) == 2.0
    assert st.cumulative_volume == 100


def test_avwap_merge_never_double_counts():
    """initialize_from_candles semantics: feeding a list that overlaps the
    existing state must only add the newer candles."""
    import storage.database as dbmod

    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        db = dbmod.Database(os.path.join(tmp, "t.db"))
        from strategy.avwap import AvwapStore

        store = AvwapStore(db)
        # day 1
        d1 = [_candle(1000 + i * 900, 10.0, 10.5, 9.5, 10.0 + i * 0.1, 100)
              for i in range(4)]
        st = store.initialize_from_candles("T", d1)
        assert st.anchor_ts == 1000
        # "restart": re-feed the SAME history plus 2 more candles
        d1_plus = d1 + [_candle(1000 + 4 * 900, 10.0, 10.5, 9.5, 10.5, 100),
                        _candle(1000 + 5 * 900, 10.0, 10.5, 9.5, 10.6, 100)]
        st2 = store.initialize_from_candles("T", d1_plus)
        assert st2.cumulative_volume == st.cumulative_volume + 200
        assert st2.anchor_ts == 1000
        # and a third feed of the identical list changes nothing
        st3 = store.initialize_from_candles("T", d1_plus)
        assert (st3.cumulative_price_volume, st3.cumulative_volume,
                st3.last_candle_ts) == \
               (st2.cumulative_price_volume, st2.cumulative_volume,
                st2.last_candle_ts)


# ------------------------------------------------- independent reference (1)
def test_avwap_matches_independent_reference_random():
    """A from-scratch re-implementation (no shared code) over 400 random
    candles must agree with the production state to ~1e-12."""
    import random

    rng = random.Random(20260918)
    base_ts = 1_700_000_000

    def reference(candles):
        num = den = 0.0
        for c in candles:
            tp = (c.high + c.low + c.close) / 3.0
            num += tp * c.volume
            den += c.volume
        return num / den if den else None

    st = AvwapState(security_id="T")
    ts = base_ts
    all_c = []
    for i in range(400):
        lo = 1.0 + rng.random() * 40.0
        hi = lo + rng.random() * 3.0
        o = lo + rng.random() * (hi - lo)
        c = lo + rng.random() * (hi - lo)
        v = int(rng.random() * 5000)
        cand = _candle(ts, o, hi, lo, c, v)
        got = st.update(cand)
        all_c.append(cand)
        ts += 900
        if i % 10 == 0:  # spot-check at several points, not just the end
            ref = reference(all_c)
            assert got is None or abs(got - ref) <= 1e-12 * max(1.0, abs(ref)), \
                f"mismatch at candle {i}: {got} vs {ref}"
    assert st.last_avwap is not None


# ------------------------------------------------------------- missed-day gap
def test_restart_after_missed_trading_day_backfills_avwap(tmp_path):
    """Stop the app after day 1, restart it on day 2. Every missed candle
    must end up in the cumulative AVWAP - a chart anchored at the contract's
    first candle includes them, so missing them is exactly the mismatch a
    user sees when validating scanner values on a chart."""
    from tests.test_v2_dashboard import make_app

    d1 = datetime(2026, 9, 15, 11, 0, tzinfo=IST)   # Tuesday, mid-session
    app, _ = make_app(tmp_path, d1)
    app.bootstrap()

    contracts = app.universe.all_scanner_contracts()
    assert contracts
    sec = contracts[0].security_id
    st1 = app.db.get_avwap_state(sec)
    assert st1 is not None and st1["last_candle_ts"], "day-1 state expected"
    assert from_day(st1["last_candle_ts"]) == d1.date()

    # --- user stops the app; next start is mid-session the following day ---
    d2 = datetime(2026, 9, 16, 11, 0, tzinfo=IST)   # Wednesday, mid-session
    app2, _ = make_app(tmp_path, d2)                # same SQLite file
    app2.bootstrap()

    st2 = app2.db.get_avwap_state(sec)
    assert from_day(st2["last_candle_ts"]) == d2.date(), (
        "AVWAP state was NOT advanced over the missed day - day-2 candles "
        f"are lost (last candle still {from_day(st2['last_candle_ts'])})"
    )
    assert st2["anchor_ts"] == st1["anchor_ts"], "backfill must not move the anchor"

    # The reference must be built from the DATA THE APP ACTUALLY CONSUMED:
    # day-1 candles came from the first feed instance; the missed portion
    # (day-1 afternoon + day-2) from the second. (The mock generates a fresh
    # random walk per instance, so the two instances are NOT guaranteed to
    # agree on overlapping candles - what we audit is the accumulation:
    # nothing missed, nothing double-counted, correct weighting.)
    day1_c = [c for c in app.feed._history[sec] if c.ts < epoch(d1)]
    missed_c = [c for c in app2.feed._history[sec] if epoch(d1) <= c.ts < epoch(d2)]
    allc = day1_c + missed_c
    ts_seq = [c.ts for c in allc]
    assert ts_seq == sorted(set(ts_seq)), "candle overlap between the two feeds"
    assert missed_c, "second feed should contain the missed candles"

    # volume: exactly the sum of what was consumed - proves no double count
    assert st2["cumulative_volume"] == sum(c.volume for c in allc)

    # value: independent accumulation over the consumed set
    num = sum(((c.high + c.low + c.close) / 3.0) * c.volume for c in allc)
    den = sum(c.volume for c in allc)
    ref = num / den
    assert abs(st2["last_avwap"] - ref) < 1e-9 * max(1.0, ref), (
        f"AVWAP {st2['last_avwap']:.6f} != reference {ref:.6f}"
    )


def test_restart_pre_open_next_day_does_not_change_avwap(tmp_path):
    """Normal nightly stop/start: nothing must change (and no history
    re-fetch storm - the state's last candle IS the previous session's last)."""
    from tests.test_v2_dashboard import make_app

    d1 = datetime(2026, 9, 15, 15, 0, tzinfo=IST)   # Tuesday, after close
    app, _ = make_app(tmp_path, d1)
    app.bootstrap()
    contracts = app.universe.all_scanner_contracts()
    sec = contracts[0].security_id
    st1 = app.db.get_avwap_state(sec)

    d2 = datetime(2026, 9, 16, 9, 5, tzinfo=IST)    # Wednesday, pre-open
    app2, _ = make_app(tmp_path, d2)
    app2.bootstrap()
    st2 = app2.db.get_avwap_state(sec)
    assert st2["last_avwap"] == st1["last_avwap"]
    assert st2["last_candle_ts"] == st1["last_candle_ts"]


def from_day(ts):
    from common.utils import from_epoch

    return from_epoch(int(ts)).date()
