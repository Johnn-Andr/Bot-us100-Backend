"""
Traduction fidèle de l'indicateur Pine Script v5 "Order Blocks" de Mark Middleton
(TradingView : https://www.tradingview.com/v/GecN34Qq/).

Logique :
  - structureLow = plus bas low des `input_range` bougies précédentes (excluant la courante).
  - Bearish BOS : ta.crossunder(close, structureLow)
      → crée un OB baissier sur la dernière bougie haussière :
        top = lastHigh (max courant depuis la dernière réinit), bottom = lastUpLow.
  - Bullish BOS : close > top d'un OB baissier existant
      → supprime cet OB baissier, et si possible crée un OB haussier sur la dernière
        bougie baissière : top = lastDown (high de la bougie baissière), bottom = lastLow.
  - Mitigation OB baissier : mèche traverse le `bottom` (high > bottom et low < bottom).
  - Mitigation OB haussier : mèche traverse le `top` (low <= top et high > top).
  - Invalidation OB haussier : close < bottom.

`lastHigh` / `lastLow` sont des running max/min réinitialisés sur chaque bougie up/down :
  - bougie haussière (close > open) : lastHigh := high
  - bougie baissière (close < open) : lastLow := low
  - puis lastHigh := max(high, lastHigh), lastLow := min(low, lastLow) à chaque barre.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Dict, List, Optional

import MetaTrader5 as mt5
import pytz

UTC = pytz.utc

TF_MAP = {
    "M1": mt5.TIMEFRAME_M1,
    "M5": mt5.TIMEFRAME_M5,
    "M15": mt5.TIMEFRAME_M15,
    "M30": mt5.TIMEFRAME_M30,
    "H1": mt5.TIMEFRAME_H1,
    "H4": mt5.TIMEFRAME_H4,
    "D1": mt5.TIMEFRAME_D1,
}

DEFAULT_INPUT_RANGE = 25
DEFAULT_BARS = 500


def to_heikin_ashi(candles: List[Dict]) -> List[Dict]:
    """
    Convertit une liste de bougies japonaises en bougies Heikin Ashi.

    Formules :
        HA_Close = (Open + High + Low + Close) / 4
        HA_Open  = (HA_Open[-1] + HA_Close[-1]) / 2   (première bougie : (Open + Close) / 2)
        HA_High  = max(High, HA_Open, HA_Close)
        HA_Low   = min(Low,  HA_Open, HA_Close)

    Le champ `time` est préservé.
    """
    if not candles:
        return []

    ha: List[Dict] = []
    for i, c in enumerate(candles):
        o, h, l, cl = c["open"], c["high"], c["low"], c["close"]
        ha_close = (o + h + l + cl) / 4.0
        if i == 0:
            ha_open = (o + cl) / 2.0
        else:
            prev = ha[-1]
            ha_open = (prev["open"] + prev["close"]) / 2.0
        ha_high = max(h, ha_open, ha_close)
        ha_low = min(l, ha_open, ha_close)
        ha.append({
            "time": int(c["time"]),
            "open": ha_open,
            "high": ha_high,
            "low": ha_low,
            "close": ha_close,
        })
    return ha


def compute_order_blocks(
    candles: List[Dict],
    input_range: int = DEFAULT_INPUT_RANGE,
) -> Dict:
    """
    Applique l'algorithme Markmiddleton sur une liste de bougies (oldest first).

    Chaque bougie : {"time": int unix, "open": float, "high": float, "low": float, "close": float}.

    Retourne :
        {
            "bullish_blocks": [...],
            "bearish_blocks": [...],
        }

    Chaque bloc :
        {
            "top": float,
            "bottom": float,
            "left_time": int,       # timestamp de la bougie d'origine
            "left_index": int,      # index dans la liste candles
            "right_time": int,      # dernière bougie où le bloc était actif (= dernière de la série)
            "mitigated": bool,
            "type": "bullish" | "bearish",
        }
    """
    n = len(candles)
    if n < 2:
        return {"bullish_blocks": [], "bearish_blocks": []}

    last_down_index = 0
    last_down = 0.0       # high de la dernière bougie baissière
    last_low = 0.0        # running min depuis la dernière bougie baissière

    last_up_index = 0
    last_up_low = 0.0     # low de la dernière bougie haussière
    last_high = 0.0       # running max depuis la dernière bougie haussière

    last_long_index = 0   # bar_index du dernier OB haussier créé (anti-doublon)

    long_boxes: List[Dict] = []
    long_box_start: List[int] = []
    short_boxes: List[Dict] = []
    short_box_start: List[int] = []

    prev_close: Optional[float] = None
    prev_structure_low: Optional[float] = None

    for bar_index in range(n):
        c = candles[bar_index]
        o, h, l, cl = c["open"], c["high"], c["low"], c["close"]

        # ── structureLow = ta.lowest(low, input_range)[1] ──
        if bar_index == 0:
            structure_low = float("inf")
        else:
            window_start = max(0, bar_index - input_range)
            structure_low = min(b["low"] for b in candles[window_start:bar_index])

        # ── Bearish BOS : ta.crossunder(close, structureLow) ──
        if (
            prev_close is not None
            and prev_structure_low is not None
            and prev_close >= prev_structure_low
            and cl < structure_low
        ):
            if (bar_index - last_up_index) < 1000:
                short_boxes.append({
                    "type": "bearish",
                    "top": last_high,
                    "bottom": last_up_low,
                    "left_index": last_up_index,
                    "left_time": int(candles[last_up_index]["time"]),
                    "right_time": int(c["time"]),
                    "mitigated": False,
                })
                short_box_start.append(bar_index)

        # ── Loop sur les OB baissiers : mitigation + Bullish BOS ──
        i = len(short_boxes) - 1
        while i >= 0:
            sbox = short_boxes[i]
            top = sbox["top"]
            bottom = sbox["bottom"]
            lstart = short_box_start[i]

            # Mitigation
            if h > bottom and l < bottom and bar_index > lstart and not sbox["mitigated"]:
                sbox["mitigated"] = True

            # Bullish BOS : close > top → suppression OB baissier + (peut-être) création OB haussier
            if cl > top:
                short_boxes.pop(i)
                short_box_start.pop(i)
                if (bar_index - last_down_index) < 1000 and bar_index > last_long_index:
                    long_boxes.append({
                        "type": "bullish",
                        "top": last_down,
                        "bottom": last_low,
                        "left_index": last_down_index,
                        "left_time": int(candles[last_down_index]["time"]),
                        "right_time": int(c["time"]),
                        "mitigated": False,
                    })
                    long_box_start.append(bar_index + 1)
                    last_long_index = bar_index
            i -= 1

        # ── Loop sur les OB haussiers : mitigation + invalidation ──
        i = len(long_boxes) - 1
        while i >= 0:
            lbox = long_boxes[i]
            top = lbox["top"]
            bottom = lbox["bottom"]
            lstart = long_box_start[i]

            # Mitigation
            if l <= top and h > top and bar_index > lstart and not lbox["mitigated"]:
                lbox["mitigated"] = True

            # Invalidation : close < bottom
            if cl < bottom:
                long_boxes.pop(i)
                long_box_start.pop(i)
            i -= 1

        # ── Enregistrement de la dernière bougie up/down ──
        if cl < o:
            last_down = h
            last_down_index = bar_index
            last_low = l
        if cl > o:
            last_up_index = bar_index
            last_up_low = l
            last_high = h

        # ── Running max/min (s'applique après les resets up/down) ──
        if h > last_high:
            last_high = h
        if l < last_low:
            last_low = l

        # Étend tous les OB actifs jusqu'à la dernière bougie (équivalent extend.right de Pine)
        for lbox in long_boxes:
            lbox["right_time"] = int(c["time"])
        for sbox in short_boxes:
            sbox["right_time"] = int(c["time"])

        prev_close = cl
        prev_structure_low = structure_low

    return {
        "bullish_blocks": long_boxes,
        "bearish_blocks": short_boxes,
    }


def backtest_strategy(
    candles: List[Dict],
    input_range: int = DEFAULT_INPUT_RANGE,
    tp_rr: Optional[float] = 2.0,
    entry_cutoff_time: Optional[int] = None,
    be_trigger_rr: Optional[float] = None,
    min_excursion_rr: Optional[float] = 1.0,
) -> Dict:
    """
    Lance l'algorithme Markmiddleton + simule la stratégie Maholy en un seul passage.

    Règles d'entrée (sur un OB **non mitigé** existant au début de la barre courante) :
        - BUY  : low <= ob.top    ET close > ob.top      (mèche dans l'OB bullish, clôture au-dessus)
        - SELL : high >= ob.bottom ET close < ob.bottom  (mèche dans l'OB bearish, clôture en-dessous)
    SL  : extrémité opposée de l'OB (bottom pour BUY, top pour SELL).
    TP  : entry ± (entry - sl) * tp_rr, ou None pour exit uniquement sur SL / fin de données.
    Une seule position ouverte à la fois. Sur une barre avec plusieurs candidats,
    l'OB le plus récent (le plus proche en bar_index) est privilégié.

    Lorsqu'une barre déclenche à la fois SL et TP, le SL prime (hypothèse conservatrice).

    `entry_cutoff_time` (unix int, optionnel) : aucune nouvelle entrée n'est ouverte sur les
    bougies dont `time >= entry_cutoff_time`. Les sorties SL/TP des trades déjà ouverts
    restent évaluées normalement. Permet de prolonger les données au-delà de la fenêtre de
    backtest pour clôturer les trades en cours sans introduire de nouveaux signaux.

    `be_trigger_rr` (optionnel) : dès que le prix atteint entry ± be_trigger_rr × risque
    en faveur du trade, le SL est déplacé à l'entrée (break even). Si plus tard le prix
    revient jusqu'à l'entrée, le trade sort en `result="BE"` (PnL ≈ 0). Mettre à None
    ou 0 désactive le break even.

    `min_excursion_rr` (optionnel, default 1.0) : filtre de maturation. L'OB doit avoir
    « parcouru » au moins `min_excursion_rr × (top - bottom)` dans son sens depuis sa
    création avant qu'une entrée soit possible. Pour un OB bullish, on attend que le
    high max depuis création atteigne `top + (top - bottom) × min_excursion_rr`.
    Symétrique pour un OB bearish via le low min. Mettre à None ou 0 désactive le filtre.
    """
    n = len(candles)
    empty = {
        "trades": [],
        "stats": _empty_stats(),
        "equity": [],
        "obs_created": 0,
    }
    if n < 2:
        return empty

    # ── État OB (mêmes variables que compute_order_blocks) ──
    last_down_index = 0
    last_down = 0.0
    last_low = 0.0

    last_up_index = 0
    last_up_low = 0.0
    last_high = 0.0

    last_long_index = 0

    long_boxes: List[Dict] = []
    long_box_start: List[int] = []
    short_boxes: List[Dict] = []
    short_box_start: List[int] = []

    prev_close: Optional[float] = None
    prev_structure_low: Optional[float] = None

    obs_created = 0

    # ── État trade ──
    open_trade: Optional[Dict] = None
    trades: List[Dict] = []

    for bar_index in range(n):
        c = candles[bar_index]
        o, h, l, cl = c["open"], c["high"], c["low"], c["close"]
        t = int(c["time"])

        # ── 1. Vérifier exit du trade ouvert (avant tout) ──
        if open_trade is not None:
            side = open_trade["type"]
            entry = open_trade["entry_price"]
            original_sl = open_trade["original_sl"]
            tp = open_trade["tp"]

            # ── 1a. Déclenchement Break Even : déplace le SL à l'entrée ──
            if (
                be_trigger_rr is not None and be_trigger_rr > 0
                and not open_trade.get("be_activated", False)
            ):
                risk_pts = abs(entry - original_sl)
                if side == "BUY":
                    be_trigger_price = entry + risk_pts * be_trigger_rr
                    if h >= be_trigger_price:
                        open_trade["be_activated"] = True
                        open_trade["sl"] = entry
                        open_trade["be_activated_time"] = t
                else:
                    be_trigger_price = entry - risk_pts * be_trigger_rr
                    if l <= be_trigger_price:
                        open_trade["be_activated"] = True
                        open_trade["sl"] = entry
                        open_trade["be_activated_time"] = t

            sl = open_trade["sl"]  # peut avoir bougé à l'entrée par le BE

            # ── 1b. Check SL / TP avec le SL potentiellement mis à BE ──
            if side == "BUY":
                hit_sl = l <= sl
                hit_tp = tp is not None and h >= tp
                if hit_sl:
                    code = "BE" if open_trade.get("be_activated", False) else "SL"
                    _close_trade(open_trade, t, sl, code, trades)
                    open_trade = None
                elif hit_tp:
                    _close_trade(open_trade, t, tp, "TP", trades)
                    open_trade = None
            else:  # SELL
                hit_sl = h >= sl
                hit_tp = tp is not None and l <= tp
                if hit_sl:
                    code = "BE" if open_trade.get("be_activated", False) else "SL"
                    _close_trade(open_trade, t, sl, code, trades)
                    open_trade = None
                elif hit_tp:
                    _close_trade(open_trade, t, tp, "TP", trades)
                    open_trade = None

        # ── 2. structureLow ──
        if bar_index == 0:
            structure_low = float("inf")
        else:
            window_start = max(0, bar_index - input_range)
            structure_low = min(b["low"] for b in candles[window_start:bar_index])

        # ── 3. Bearish BOS ──
        if (
            prev_close is not None
            and prev_structure_low is not None
            and prev_close >= prev_structure_low
            and cl < structure_low
        ):
            if (bar_index - last_up_index) < 1000:
                short_boxes.append({
                    "type": "bearish",
                    "top": last_high,
                    "bottom": last_up_low,
                    "left_index": last_up_index,
                    "left_time": int(candles[last_up_index]["time"]),
                    "right_time": t,
                    "mitigated": False,
                    "extreme_low": l,
                })
                short_box_start.append(bar_index)
                obs_created += 1

        # ── 4. Loop short boxes : Bullish BOS (mitigation faite plus bas) ──
        i = len(short_boxes) - 1
        while i >= 0:
            sbox = short_boxes[i]
            if cl > sbox["top"]:
                short_boxes.pop(i)
                short_box_start.pop(i)
                if (bar_index - last_down_index) < 1000 and bar_index > last_long_index:
                    long_boxes.append({
                        "type": "bullish",
                        "top": last_down,
                        "bottom": last_low,
                        "left_index": last_down_index,
                        "left_time": int(candles[last_down_index]["time"]),
                        "right_time": t,
                        "mitigated": False,
                        "extreme_high": h,
                    })
                    long_box_start.append(bar_index + 1)
                    last_long_index = bar_index
                    obs_created += 1
            i -= 1

        # ── 5. Invalidation OB haussiers (close < bottom) ──
        i = len(long_boxes) - 1
        while i >= 0:
            lbox = long_boxes[i]
            if cl < lbox["bottom"]:
                long_boxes.pop(i)
                long_box_start.pop(i)
            i -= 1

        # ── 5.5. Mise à jour de l'excursion (high max / low min depuis création) ──
        for lbox in long_boxes:
            if h > lbox["extreme_high"]:
                lbox["extreme_high"] = h
        for sbox in short_boxes:
            if l < sbox["extreme_low"]:
                sbox["extreme_low"] = l

        excursion_filter_active = min_excursion_rr is not None and min_excursion_rr > 0

        # ── 6. Signal d'entrée AVANT la mise à jour des flags mitigated ──
        # On entre uniquement si pas de position ouverte ET OB pas encore mitigé.
        # En cas de match multiple, le plus récent (dernier ajouté) gagne.
        can_enter = (
            open_trade is None
            and (entry_cutoff_time is None or t < entry_cutoff_time)
        )
        if can_enter:
            # BUY : OB bullish, mèche basse dans la zone, corps entièrement au-dessus du top.
            # `o >= top` exclut les bougies qui traversent le niveau par leur corps (engulfing).
            for j in range(len(long_boxes) - 1, -1, -1):
                lbox = long_boxes[j]
                if lbox["mitigated"]:
                    continue
                if bar_index <= long_box_start[j]:
                    continue  # OB tout juste créé, on attend une barre de plus
                if excursion_filter_active:
                    required_high = lbox["top"] + (lbox["top"] - lbox["bottom"]) * min_excursion_rr
                    if lbox["extreme_high"] < required_high:
                        continue
                if l <= lbox["top"] and o >= lbox["top"] and cl > lbox["top"]:
                    sl = lbox["bottom"]
                    risk = cl - sl
                    tp = cl + risk * tp_rr if tp_rr and risk > 0 else None
                    open_trade = {
                        "type": "BUY",
                        "entry_time": t,
                        "entry_price": cl,
                        "sl": sl,
                        "original_sl": sl,
                        "be_activated": False,
                        "be_activated_time": None,
                        "tp": tp,
                        "ob_top": lbox["top"],
                        "ob_bottom": lbox["bottom"],
                        "ob_left_time": lbox["left_time"],
                    }
                    break

            # SELL : OB bearish, mèche haute dans la zone, corps entièrement sous le bottom.
            # `o <= bottom` exclut les bougies qui traversent le niveau par leur corps (engulfing).
            if open_trade is None:
                for j in range(len(short_boxes) - 1, -1, -1):
                    sbox = short_boxes[j]
                    if sbox["mitigated"]:
                        continue
                    if bar_index <= short_box_start[j]:
                        continue
                    if excursion_filter_active:
                        required_low = sbox["bottom"] - (sbox["top"] - sbox["bottom"]) * min_excursion_rr
                        if sbox["extreme_low"] > required_low:
                            continue
                    if h >= sbox["bottom"] and o <= sbox["bottom"] and cl < sbox["bottom"]:
                        sl = sbox["top"]
                        risk = sl - cl
                        tp = cl - risk * tp_rr if tp_rr and risk > 0 else None
                        open_trade = {
                            "type": "SELL",
                            "entry_time": t,
                            "entry_price": cl,
                            "sl": sl,
                            "original_sl": sl,
                            "be_activated": False,
                            "be_activated_time": None,
                            "tp": tp,
                            "ob_top": sbox["top"],
                            "ob_bottom": sbox["bottom"],
                            "ob_left_time": sbox["left_time"],
                        }
                        break

        # ── 7. Mise à jour mitigation (après la décision d'entrée) ──
        for j, lbox in enumerate(long_boxes):
            if not lbox["mitigated"] and bar_index > long_box_start[j] and l <= lbox["top"] and h > lbox["top"]:
                lbox["mitigated"] = True
        for j, sbox in enumerate(short_boxes):
            if not sbox["mitigated"] and bar_index > short_box_start[j] and h > sbox["bottom"] and l < sbox["bottom"]:
                sbox["mitigated"] = True

        # ── 8. lastDown / lastUp + running max/min ──
        if cl < o:
            last_down = h
            last_down_index = bar_index
            last_low = l
        if cl > o:
            last_up_index = bar_index
            last_up_low = l
            last_high = h
        if h > last_high:
            last_high = h
        if l < last_low:
            last_low = l

        for lbox in long_boxes:
            lbox["right_time"] = t
        for sbox in short_boxes:
            sbox["right_time"] = t

        prev_close = cl
        prev_structure_low = structure_low

    # ── Trade encore ouvert à la fin des données : marqué "OPEN", exclu des stats ──
    # PnL = mark-to-market (entry vs last close), purement indicatif.
    open_count = 0
    if open_trade is not None:
        _close_trade(open_trade, int(candles[-1]["time"]), float(candles[-1]["close"]), "OPEN", trades)
        open_count = 1

    return {
        "trades": trades,
        "stats": _compute_stats(trades),
        "equity": _compute_equity(trades),
        "obs_created": obs_created,
        "open_count": open_count,
    }


def _close_trade(trade: Dict, exit_ts: int, exit_price: float, result: str, trades_list: List[Dict]):
    """Ajoute le trade clôturé à la liste avec son pnl et son motif de sortie."""
    side = trade["type"]
    entry = trade["entry_price"]
    pnl = (exit_price - entry) if side == "BUY" else (entry - exit_price)
    trades_list.append({
        **trade,
        "exit_time": exit_ts,
        "exit_price": exit_price,
        "pnl": pnl,
        "result": result,
    })


def _empty_stats() -> Dict:
    return {
        "trades_count": 0, "wins": 0, "losses": 0, "win_rate": 0.0,
        "total_pnl": 0.0, "avg_pnl": 0.0, "profit_factor": None,
        "max_drawdown": 0.0, "best_trade": 0.0, "worst_trade": 0.0,
    }


def _compute_stats(trades: List[Dict]) -> Dict:
    """Stats calculées **uniquement** sur les trades résolus (SL/TP). Les trades 'OPEN' sont exclus."""
    closed = [t for t in trades if t.get("result") != "OPEN"]
    if not closed:
        return _empty_stats()
    wins = [t for t in closed if t["pnl"] > 0]
    losses = [t for t in closed if t["pnl"] <= 0]
    total_pnl = sum(t["pnl"] for t in closed)
    gross_win = sum(t["pnl"] for t in wins)
    gross_loss = abs(sum(t["pnl"] for t in losses))

    peak = 0.0
    max_dd = 0.0
    running = 0.0
    for t in closed:
        running += t["pnl"]
        if running > peak:
            peak = running
        dd = peak - running
        if dd > max_dd:
            max_dd = dd

    return {
        "trades_count": len(closed),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round(len(wins) / len(closed) * 100, 1),
        "total_pnl": round(total_pnl, 2),
        "avg_pnl": round(total_pnl / len(closed), 2),
        "profit_factor": round(gross_win / gross_loss, 2) if gross_loss > 0 else None,
        "max_drawdown": round(max_dd, 2),
        "best_trade": round(max(t["pnl"] for t in closed), 2),
        "worst_trade": round(min(t["pnl"] for t in closed), 2),
    }


def _compute_equity(trades: List[Dict]) -> List[Dict]:
    """Courbe d'equity sur les trades clôturés uniquement."""
    equity = []
    cumul = 0.0
    for t in trades:
        if t.get("result") == "OPEN":
            continue
        cumul += t["pnl"]
        equity.append({"time": t["exit_time"], "value": round(cumul, 2)})
    return equity


def fetch_candles(symbol: str, timeframe: str, bars: int) -> List[Dict]:
    """Récupère les `bars` dernières bougies via MT5 et les formate."""
    if not mt5.symbol_select(symbol, True):
        raise RuntimeError(f"Symbole introuvable : {symbol}")

    tf_const = TF_MAP.get(timeframe.upper())
    if tf_const is None:
        raise ValueError(f"Timeframe invalide : {timeframe}. Valides : {list(TF_MAP)}")

    rates = mt5.copy_rates_from_pos(symbol, tf_const, 0, bars)
    if rates is None or len(rates) == 0:
        raise RuntimeError(f"Aucune donnée MT5 pour {symbol} {timeframe} (bars={bars})")

    return [
        {
            "time": int(b["time"]),
            "open": float(b["open"]),
            "high": float(b["high"]),
            "low": float(b["low"]),
            "close": float(b["close"]),
        }
        for b in rates
    ]


def analyze(
    symbol: str,
    timeframe: str = "M15",
    bars: int = DEFAULT_BARS,
    input_range: int = DEFAULT_INPUT_RANGE,
    candle_type: str = "japanese",
) -> Dict:
    """
    Pipeline complet : récupère les bougies MT5 + applique l'algorithme.

    candle_type :
        - "japanese"     → bougies standards
        - "heikin_ashi"  → conversion HA appliquée avant la détection (et renvoyée
                           dans `candles` pour un affichage cohérent)

    Retourne :
        {
            "symbol": str,
            "timeframe": str,
            "bars_count": int,
            "input_range": int,
            "candle_type": str,
            "candles": [...],          # pour le rendu graphique
            "bullish_blocks": [...],
            "bearish_blocks": [...],
        }
    """
    raw = fetch_candles(symbol, timeframe, bars)

    ctype = candle_type.lower().replace("-", "_")
    if ctype in ("heikin_ashi", "ha", "heikinashi"):
        candles = to_heikin_ashi(raw)
        ctype = "heikin_ashi"
    elif ctype in ("japanese", "jp", "standard", ""):
        candles = raw
        ctype = "japanese"
    else:
        raise ValueError(f"candle_type invalide : {candle_type}. Valides : japanese | heikin_ashi")

    result = compute_order_blocks(candles, input_range=input_range)
    return {
        "symbol": symbol,
        "timeframe": timeframe.upper(),
        "bars_count": len(candles),
        "input_range": input_range,
        "candle_type": ctype,
        "candles": candles,
        "bullish_blocks": result["bullish_blocks"],
        "bearish_blocks": result["bearish_blocks"],
    }
