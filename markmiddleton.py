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
