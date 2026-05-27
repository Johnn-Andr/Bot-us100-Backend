"""
Moteur de backtest pour les stratégies du bot.

Étape 1 : stratégie ORB (Opening Range Breakout) sur la session NY 8h-9h.
  - Range : high/low des bougies M1 entre 8h00 et 9h00 NY.
  - Entry : BUY au franchissement du high, SELL au franchissement du low.
  - Exit : SL touché (côté opposé du range) OU close de session (23h59 NY).
  - Une seule entry possible par jour (le premier signal gagne).
"""

from __future__ import annotations

from datetime import datetime, time, timedelta
from typing import Optional

import MetaTrader5 as mt5
import pytz

NY_TZ = pytz.timezone("America/New_York")
UTC = pytz.utc

RANGE_START = time(8, 0)
RANGE_END = time(9, 0)
SESSION_END = time(23, 59)

TF_MAP_CHART = {
    "M5": mt5.TIMEFRAME_M5,
    "M15": mt5.TIMEFRAME_M15,
    "M30": mt5.TIMEFRAME_M30,
    "H1": mt5.TIMEFRAME_H1,
    "H4": mt5.TIMEFRAME_H4,
    "D1": mt5.TIMEFRAME_D1,
}


def _parse_date(s: str) -> datetime:
    """YYYY-MM-DD → datetime NY-aware à minuit."""
    d = datetime.strptime(s, "%Y-%m-%d")
    return NY_TZ.localize(datetime(d.year, d.month, d.day))


def _ensure_symbol(symbol: str):
    if not mt5.symbol_select(symbol, True):
        raise RuntimeError(f"Symbole introuvable : {symbol}")


def _fetch_m1(symbol: str, start_ny: datetime, end_ny: datetime):
    """Retourne les bougies M1 entre start et end (NY-aware) ou [] si rien."""
    bars = mt5.copy_rates_range(
        symbol,
        mt5.TIMEFRAME_M1,
        start_ny.astimezone(UTC),
        end_ny.astimezone(UTC),
    )
    return bars if bars is not None else []


def _fetch_chart(symbol: str, start_ny: datetime, end_ny: datetime, tf: str):
    tf_const = TF_MAP_CHART.get(tf.upper(), mt5.TIMEFRAME_H1)
    bars = mt5.copy_rates_range(
        symbol,
        tf_const,
        start_ny.astimezone(UTC),
        end_ny.astimezone(UTC),
    )
    if bars is None:
        return []
    return [
        {
            "time": int(b["time"]),
            "open": float(b["open"]),
            "high": float(b["high"]),
            "low": float(b["low"]),
            "close": float(b["close"]),
        }
        for b in bars
    ]


def _simulate_day(day_bars, range_high, range_low):
    """
    Simule l'ORB sur les bougies M1 d'une seule journée (après 9h NY).
    Retourne (trade_dict | None).
    """
    for b in day_bars:
        high = float(b["high"])
        low = float(b["low"])
        ts = int(b["time"])

        # BUY breakout
        if high >= range_high:
            entry = range_high
            sl = range_low
            # Cherche l'exit dans les bougies suivantes
            for f in day_bars:
                if int(f["time"]) <= ts:
                    continue
                if float(f["low"]) <= sl:
                    return {
                        "type": "BUY",
                        "entry_time": ts,
                        "entry_price": entry,
                        "exit_time": int(f["time"]),
                        "exit_price": sl,
                        "sl": sl,
                        "pnl": sl - entry,
                        "result": "SL",
                    }
            # Pas de SL touché → close fin de session
            last = day_bars[-1]
            return {
                "type": "BUY",
                "entry_time": ts,
                "entry_price": entry,
                "exit_time": int(last["time"]),
                "exit_price": float(last["close"]),
                "sl": sl,
                "pnl": float(last["close"]) - entry,
                "result": "CLOSE",
            }

        # SELL breakout
        if low <= range_low:
            entry = range_low
            sl = range_high
            for f in day_bars:
                if int(f["time"]) <= ts:
                    continue
                if float(f["high"]) >= sl:
                    return {
                        "type": "SELL",
                        "entry_time": ts,
                        "entry_price": entry,
                        "exit_time": int(f["time"]),
                        "exit_price": sl,
                        "sl": sl,
                        "pnl": entry - sl,
                        "result": "SL",
                    }
            last = day_bars[-1]
            return {
                "type": "SELL",
                "entry_time": ts,
                "entry_price": entry,
                "exit_time": int(last["time"]),
                "exit_price": float(last["close"]),
                "sl": sl,
                "pnl": entry - float(last["close"]),
                "result": "CLOSE",
            }

    return None


def run_orb_backtest(
    symbol: str,
    date_from: str,
    date_to: str,
    chart_tf: str = "H1",
) -> dict:
    """Lance un backtest ORB et retourne bougies + trades + equity + stats."""
    _ensure_symbol(symbol)

    start_ny = _parse_date(date_from)
    end_ny = _parse_date(date_to) + timedelta(days=1)

    if end_ny <= start_ny:
        raise ValueError("date_to doit être >= date_from")

    chart = _fetch_chart(symbol, start_ny, end_ny, chart_tf)

    trades = []
    cursor = start_ny
    while cursor < end_ny:
        # Skip weekends (samedi=5, dimanche=6)
        if cursor.weekday() < 5:
            day_start = cursor.replace(hour=RANGE_START.hour, minute=RANGE_START.minute)
            range_end = cursor.replace(hour=RANGE_END.hour, minute=RANGE_END.minute)
            day_end = cursor.replace(hour=SESSION_END.hour, minute=SESSION_END.minute)

            range_bars = _fetch_m1(symbol, day_start, range_end)
            if len(range_bars) > 0:
                rh = max(float(b["high"]) for b in range_bars)
                rl = min(float(b["low"]) for b in range_bars)

                trade_bars = _fetch_m1(symbol, range_end, day_end)
                if len(trade_bars) > 0:
                    trade = _simulate_day(trade_bars, rh, rl)
                    if trade is not None:
                        trade["range_high"] = rh
                        trade["range_low"] = rl
                        trade["date"] = cursor.strftime("%Y-%m-%d")
                        trades.append(trade)

        cursor += timedelta(days=1)

    # Equity curve (points = cumul du pnl à chaque exit)
    equity = []
    cumul = 0.0
    for t in trades:
        cumul += t["pnl"]
        equity.append({"time": t["exit_time"], "value": round(cumul, 2)})

    # Stats
    wins = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] <= 0]
    total_pnl = sum(t["pnl"] for t in trades)
    gross_win = sum(t["pnl"] for t in wins)
    gross_loss = abs(sum(t["pnl"] for t in losses))

    # Max drawdown sur l'equity curve
    peak = 0.0
    max_dd = 0.0
    running = 0.0
    for t in trades:
        running += t["pnl"]
        if running > peak:
            peak = running
        dd = peak - running
        if dd > max_dd:
            max_dd = dd

    stats = {
        "trades_count": len(trades),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round(len(wins) / len(trades) * 100, 1) if trades else 0.0,
        "total_pnl": round(total_pnl, 2),
        "avg_pnl": round(total_pnl / len(trades), 2) if trades else 0.0,
        "profit_factor": round(gross_win / gross_loss, 2) if gross_loss > 0 else None,
        "max_drawdown": round(max_dd, 2),
        "best_trade": round(max((t["pnl"] for t in trades), default=0.0), 2),
        "worst_trade": round(min((t["pnl"] for t in trades), default=0.0), 2),
    }

    return {
        "symbol": symbol,
        "strategy": "ORB",
        "date_from": date_from,
        "date_to": date_to,
        "chart_tf": chart_tf.upper(),
        "candles": chart,
        "trades": trades,
        "equity": equity,
        "stats": stats,
    }
