# BTC Race Bot — "Ride the cross, trail the exit"

A long-only trading bot for cbBTC/USDC 1-minute bars, written for the
make.fun `btc-race-24h-20261117-0603` competition. It speaks the
`agent-fun/bars-replay/v1` protocol (newline JSON on stdin/stdout) using
the vendored Python SDK shim in `vendor/makefun_bars.py`; `main.py` is
Python 3.13 standard library only and fully deterministic.

## Strategy

The bot buys one thing only: the moment an uptrend actually starts.

- **Entry trigger** — an upward cross of the fast EMA (36 bars) over
  the slow EMA (144 bars) of the close. One shot per cross: while the
  fast average stays on top there is no adding and no re-entering, and
  a 20-bar cooldown after every exit adds a second lock, so a ranging
  market cannot grind the account down one round trip at a time.
- **Volatility band** — Wilder ATR(14) as a fraction of price must be
  between 6 and 60 bps at the moment of the cross: too dead can't pay
  the ~26 bps round-trip cost, too wild makes exits meaningless.
- **Exits** — a trailing stop 2.2 ATR under the highest close since
  entry; a regime exit when the fast EMA falls back under the slow one;
  and a stale-trade exit that frees capital from a position that has
  gone nowhere for 240 bars.
- **Sizing** — 65% of cash per entry, one position at a time, sized
  against the pool so price impact stays a small part of the cost.

## Layout

- `main.py` — the bot and its strategy.
- `vendor/makefun_bars.py` — the competition SDK shim, vendored
  byte-identical from the kit.
- `LICENSE` — MIT.
