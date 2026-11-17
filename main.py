"""Ride the cross, trail the exit: a long-only cbBTC 1-minute bot.

Strategy (my own design):
  * Entry trigger: an upward cross of two exponential moving averages
    of the close (fast 36 over slow 144). One shot per cross -- while
    the fast average stays on top we do not add or re-enter, so a
    ranging market cannot grind the account down one round trip at a
    time; a 20-bar cooldown after every exit adds a second lock.
  * Volatility sanity: the average true range (Wilder, 14 bars) as a
    fraction of price must sit inside a band at the moment of the
    cross. Dead tape cannot pay the ~26 bps round-trip cost, and wild
    tape makes the trailing exit meaningless, so both extremes keep
    us in cash.
  * Exit: a trailing stop anchored to the highest close since entry,
    2.2 ATR below it, plus a regime exit (fast EMA crossing back under
    the slow EMA) and a stale-trade exit (a position that has gone
    nowhere after 240 bars is sold so the capital can work elsewhere).
  * Sizing: a fixed 65% of current cash per entry, one position at a
    time (the contest cap), sized against the pool so price impact
    stays a small part of the round-trip cost.

Everything is a pure function of the bars seen so far: no clock, no
network, no files, no randomness -- two runs over the same bars emit
byte-identical orders.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "vendor"))

import makefun_bars

SYMBOL = "CBBTC"

# --- parameters (tuned on the kit's frozen dev window) -------------------
EMA_FAST = 36
EMA_SLOW = 144
BREAKOUT_LOOKBACK = 72
ATR_PERIOD = 14
ATR_MIN_PCT = 0.0006   # 6 bps of price: below this the tape is too dead
ATR_MAX_PCT = 0.0060   # 60 bps of price: above this the tape is too wild
TRAIL_ATR_MULT = 2.2
STALE_BARS = 240
COOLDOWN_BARS = 20
CASH_FRACTION = 0.65
MAX_SLIPPAGE_BPS = 200
ENTRY_MODE = "cross"      # final build races the EMA-cross trigger
REGIME_EXIT = True        # sell when the fast EMA falls back under the slow one
MOM_LOOKBACK = 90
MOM_MIN = 0.003           # 30 bps of trailing momentum required
NEAR_HIGH = 0.0015        # close within 15 bps of the lookback high

# --- running state --------------------------------------------------------
_closes = []           # close of every bar seen, oldest first
_highs = []
_lows = []
_ema_fast = None
_ema_slow = None
_tr = []               # true ranges, for Wilder ATR
_atr = None
_entry_bar = -1        # index into _closes where the open position began
_peak_close = 0        # highest close since entry
_last_exit_bar = -10 ** 9
_prev_ema_diff = None  # (fast - slow) EMA as of the previous bar


def _update_indicators(close, high, low):
    global _ema_fast, _ema_slow, _atr
    _closes.append(close)
    _highs.append(high)
    _lows.append(low)
    if _ema_fast is None:
        _ema_fast = float(close)
        _ema_slow = float(close)
    else:
        kf = 2.0 / (EMA_FAST + 1)
        ks = 2.0 / (EMA_SLOW + 1)
        _ema_fast += kf * (close - _ema_fast)
        _ema_slow += ks * (close - _ema_slow)
    if len(_closes) >= 2:
        prev = _closes[-2]
        tr = max(high - low, abs(high - prev), abs(low - prev))
        _tr.append(tr)
        if _atr is None:
            if len(_tr) >= ATR_PERIOD:
                _atr = sum(_tr[-ATR_PERIOD:]) / ATR_PERIOD
        else:
            _atr += (tr - _atr) / ATR_PERIOD


def on_batch(state, batch):
    global _entry_bar, _peak_close, _last_exit_bar, _prev_ema_diff
    bar = None
    for b in batch["bars"]:
        if b["symbol"] == SYMBOL:
            bar = b
    if bar is None:
        return None
    close = int(bar["c"])
    _prev_ema_diff = None if _ema_fast is None else _ema_fast - _ema_slow
    _update_indicators(close, int(bar["h"]), int(bar["l"]))
    n = len(_closes)
    if state.warming_up:
        return None

    held = state.positions.get(SYMBOL)
    holding = held is not None and held.qty > 0

    if holding:
        if close > _peak_close:
            _peak_close = close
        exit_now = False
        # Trailing stop: peak close minus a few ATR.
        if _atr is not None and _peak_close - close > TRAIL_ATR_MULT * _atr:
            exit_now = True
        # Regime exit: the uptrend that let us in is over.
        if REGIME_EXIT and _ema_fast is not None and _ema_fast < _ema_slow:
            exit_now = True
        # Stale exit: capital parked in a position going nowhere.
        if _entry_bar >= 0 and n - 1 - _entry_bar >= STALE_BARS:
            entry_ref = _closes[_entry_bar]
            if close <= entry_ref:
                exit_now = True
        if exit_now:
            _last_exit_bar = n - 1
            _entry_bar = -1
            _peak_close = 0
            return [{
                "side": "sell",
                "symbol": SYMBOL,
                "qty": held.qty,
                "max_slippage_bps": MAX_SLIPPAGE_BPS,
            }]
        return None

    # Flat: look for a gated breakout entry.
    if n <= max(EMA_SLOW, BREAKOUT_LOOKBACK) + 1:
        return None
    if n - 1 - _last_exit_bar < COOLDOWN_BARS:
        return None
    if _atr is None or close <= 0:
        return None
    atr_pct = _atr / close
    if atr_pct < ATR_MIN_PCT or atr_pct > ATR_MAX_PCT:
        return None
    if not (_ema_fast > _ema_slow):
        return None
    if ENTRY_MODE == "breakout":
        prior_high = max(_closes[-BREAKOUT_LOOKBACK - 1:-1])
        if close <= prior_high:
            return None
    elif ENTRY_MODE == "momentum":
        if n <= MOM_LOOKBACK + 1:
            return None
        if close / _closes[-MOM_LOOKBACK - 1] - 1.0 < MOM_MIN:
            return None
        if close < max(_closes[-MOM_LOOKBACK - 1:-1]) * (1.0 - NEAR_HIGH):
            return None
    elif ENTRY_MODE == "cross":
        # Only the bar of the upward cross itself (previous bar not above).
        prev_diff = _prev_ema_diff
        if prev_diff is None or prev_diff > 0:
            return None
    elif ENTRY_MODE == "crossride":
        # The upward cross itself, or -- while the uptrend regime lasts --
        # a fresh lookback high (riding the same trend after a stop-out).
        fresh_cross = _prev_ema_diff is not None and _prev_ema_diff <= 0
        if not fresh_cross:
            prior_high = max(_closes[-BREAKOUT_LOOKBACK - 1:-1])
            if close <= prior_high:
                return None
    spend = int(state.cash * CASH_FRACTION)
    room = state.weight_room(SYMBOL)
    if room > 0:
        spend = min(spend, room)
    if spend < 1_000_000:  # below the 1 USDC minimum notional
        return None
    _entry_bar = n - 1
    _peak_close = close
    return [{
        "side": "buy",
        "symbol": SYMBOL,
        "notional_quote_units": spend,
        "max_slippage_bps": MAX_SLIPPAGE_BPS,
    }]


if __name__ == "__main__":
    makefun_bars.run(on_batch)
