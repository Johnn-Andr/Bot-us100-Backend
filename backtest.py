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
    "M1": mt5.TIMEFRAME_M1,
    "M5": mt5.TIMEFRAME_M5,
    "M15": mt5.TIMEFRAME_M15,
    "M30": mt5.TIMEFRAME_M30,
    "H1": mt5.TIMEFRAME_H1,
    "H4": mt5.TIMEFRAME_H4,
    "D1": mt5.TIMEFRAME_D1,
}

# Timeframes autorisés pour le replay détaillé (zoom intra-day)
TF_MAP_REPLAY = {
    "M1": mt5.TIMEFRAME_M1,
    "M5": mt5.TIMEFRAME_M5,
    "M15": mt5.TIMEFRAME_M15,
    "M30": mt5.TIMEFRAME_M30,
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


def _fetch_bars(symbol: str, start_ny: datetime, end_ny: datetime, tf_const):
    """Comme _fetch_m1 mais avec un timeframe paramétrable."""
    bars = mt5.copy_rates_range(
        symbol,
        tf_const,
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
            for f in day_bars:
                if int(f["time"]) <= ts:
                    continue
                if float(f["low"]) <= sl:
                    return _make_trade(
                        side="BUY", entry_ts=ts, entry=entry, sl=sl,
                        exit_ts=int(f["time"]), exit_price=sl,
                        pnl=sl - entry, result="SL",
                    )
            last = day_bars[-1]
            return _make_trade(
                side="BUY", entry_ts=ts, entry=entry, sl=sl,
                exit_ts=int(last["time"]), exit_price=float(last["close"]),
                pnl=float(last["close"]) - entry, result="CLOSE",
            )

        # SELL breakout
        if low <= range_low:
            entry = range_low
            sl = range_high
            for f in day_bars:
                if int(f["time"]) <= ts:
                    continue
                if float(f["high"]) >= sl:
                    return _make_trade(
                        side="SELL", entry_ts=ts, entry=entry, sl=sl,
                        exit_ts=int(f["time"]), exit_price=sl,
                        pnl=entry - sl, result="SL",
                    )
            last = day_bars[-1]
            return _make_trade(
                side="SELL", entry_ts=ts, entry=entry, sl=sl,
                exit_ts=int(last["time"]), exit_price=float(last["close"]),
                pnl=entry - float(last["close"]), result="CLOSE",
            )

    return None


def _make_trade(side, entry_ts, entry, sl, exit_ts, exit_price, pnl, result):
    return {
        "type": side,
        "entry_time": entry_ts,
        "entry_price": entry,
        "exit_time": exit_ts,
        "exit_price": exit_price,
        "sl": sl,
        "pnl": pnl,
        "result": result,
    }


def _build_orb_steps(day_ny: datetime, trade: dict) -> list:
    """
    Construit la timeline générique d'événements pour la stratégie ORB.
    Le frontend les consomme sans rien connaître de la stratégie.

    Types : range_start | range_end | entry | exit
    """
    range_start = day_ny.replace(hour=RANGE_START.hour, minute=RANGE_START.minute)
    range_end = day_ny.replace(hour=RANGE_END.hour, minute=RANGE_END.minute)
    rh = trade["range_high"]
    rl = trade["range_low"]
    side = trade["type"]
    entry = trade["entry_price"]
    exit_label = "SL touché" if trade["result"] == "SL" else "Close de session"

    return [
        {
            "time": int(range_start.astimezone(UTC).timestamp()),
            "type": "range_start",
            "label": "Construction du range NY (8h00–9h00)",
        },
        {
            "time": int(range_end.astimezone(UTC).timestamp()),
            "type": "range_end",
            "label": f"Range défini · Haut {rh:.2f} / Bas {rl:.2f}",
        },
        {
            "time": trade["entry_time"],
            "type": "entry",
            "label": f"Breakout {side} à {entry:.2f} · SL {trade['sl']:.2f}",
        },
        {
            "time": trade["exit_time"],
            "type": "exit",
            "label": f"Sortie ({exit_label}) à {trade['exit_price']:.2f} · "
                     f"{'+' if trade['pnl'] >= 0 else ''}{trade['pnl']:.2f} pts",
        },
    ]


def get_trade_detail_orb(symbol: str, date_str: str, tf: str = "M1") -> dict:
    """
    Recharge les bougies d'une journée + recalcule range et steps,
    pour alimenter le replay détaillé d'un trade ORB.

    Le calcul du range et du trade reste TOUJOURS en M1 pour la précision.
    Seul l'affichage des bougies utilise le timeframe `tf` (M1, M5, M15, M30).
    """
    _ensure_symbol(symbol)
    day_ny = _parse_date(date_str)

    day_start = day_ny.replace(hour=RANGE_START.hour, minute=RANGE_START.minute)
    range_end = day_ny.replace(hour=RANGE_END.hour, minute=RANGE_END.minute)
    day_end = day_ny.replace(hour=SESSION_END.hour, minute=SESSION_END.minute)

    # 1) Calcul du range et du trade en M1 (précision)
    range_bars_m1 = _fetch_m1(symbol, day_start, range_end)
    trade_bars_m1 = _fetch_m1(symbol, range_end, day_end)

    if len(range_bars_m1) == 0 and len(trade_bars_m1) == 0:
        return {
            "symbol": symbol,
            "date": date_str,
            "tf": tf.upper(),
            "candles": [],
            "range_high": None,
            "range_low": None,
            "trade": None,
            "steps": [],
        }

    rh = max((float(b["high"]) for b in range_bars_m1), default=None)
    rl = min((float(b["low"]) for b in range_bars_m1), default=None)

    trade = None
    if rh is not None and rl is not None and len(trade_bars_m1) > 0:
        trade = _simulate_day(trade_bars_m1, rh, rl)
        if trade is not None:
            trade["range_high"] = rh
            trade["range_low"] = rl
            trade["date"] = date_str

    # 2) Bougies pour l'AFFICHAGE au timeframe demandé
    tf_const = TF_MAP_REPLAY.get(tf.upper(), mt5.TIMEFRAME_M1)
    if tf_const == mt5.TIMEFRAME_M1:
        raw_bars = list(range_bars_m1) + list(trade_bars_m1)
    else:
        raw_bars = list(_fetch_bars(symbol, day_start, day_end, tf_const))

    # Dédoublonne par timestamp (les fetchs range/session peuvent partager une bougie frontière)
    seen_ts = set()
    candles = []
    for b in raw_bars:
        ts = int(b["time"])
        if ts in seen_ts:
            continue
        seen_ts.add(ts)
        candles.append({
            "time": ts,
            "open": float(b["open"]),
            "high": float(b["high"]),
            "low": float(b["low"]),
            "close": float(b["close"]),
        })
    candles.sort(key=lambda c: c["time"])

    steps = _build_orb_steps(day_ny, trade) if trade is not None else []

    return {
        "symbol": symbol,
        "date": date_str,
        "tf": tf.upper(),
        "candles": candles,
        "range_high": rh,
        "range_low": rl,
        "trade": trade,
        "steps": steps,
    }


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
                        trade["steps"] = _build_orb_steps(cursor, trade)
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
