"""
Layer (c) — Orchestration.

Points d'entrée publics :
    analyze_symbol()      — signaux Crabel (inchangé).
    analyze_symbol_full() — signaux + Stretch + ORB levels + SL options.

Les deux fonctions partagent le même contexte MT5 via _fetch_context().

HYPOTHÈSE FUSEAU:
Les timestamps MT5 (champ `time`) sont en heure serveur sous forme d'entiers
Unix.  Ce module ne réalise aucune conversion vers l'heure locale.  Les dates
"YYYY-MM-DD" sont construites via datetime.utcfromtimestamp(), ce qui suppose
que le serveur MT5 tourne en UTC.  Cf. commentaires dans mt5_data.py.

Règle « bougie D1 clôturée » :
Une bougie D1 d'heure d'ouverture T est considérée clôturée si
server_time >= T + 86400 (fenêtre de 24 h).  Cette règle est valide pour
les marchés forex et les indices synthétiques.  Pour les instruments boursiers
avec session définie, la bougie peut se clôturer avant T + 86400 ; la règle
est alors conservative (cf. AMBIGUÏTÉ dans calculator.py).
is_closed_bar est toujours True avec le comportement par défaut (target=None).
"""

from __future__ import annotations

from . import mt5_data
from .calculator import (
    compute_atr,
    compute_signals,
    compute_stretch,
    compute_tradeability_hint,
)
from .exceptions import (  # noqa: F401
    InsufficientHistoryForStretch,
    MalformedBarData,
    MT5DataError,
    MT5SessionNotInitialized,
    SymbolNotFound,
)


# ── Helpers internes partagés ────────────────────────────────────────────────


def _resolve_j_idx(
    bars: list[dict],
    target: str | int | None,
    server_now: int,
    symbol: str,
) -> tuple[int, bool]:
    """
    Résout l'indice j_idx et is_closed_bar à partir du paramètre target.

    Retourne (j_idx, is_closed_bar).
    """
    last_idx = len(bars) - 1

    if bars[last_idx]["timestamp"] + 86400 <= server_now:
        last_closed_idx = last_idx
    else:
        if last_idx < 1:
            raise MT5DataError(
                f"Une seule bougie D1 disponible pour '{symbol}' "
                "et elle n'est pas encore clôturée."
            )
        last_closed_idx = last_idx - 1

    is_closed_bar = True

    if target is None:
        j_idx = last_closed_idx

    elif isinstance(target, int):
        if target >= 0:
            raise ValueError(
                f"target doit être None, un entier négatif ou une chaîne 'YYYY-MM-DD' ; "
                f"reçu {target!r}"
            )
        j_idx = last_closed_idx + target
        if j_idx < 0:
            raise ValueError(
                f"Décalage target={target} dépasse l'historique disponible "
                f"(last_closed_idx={last_closed_idx})."
            )

    elif isinstance(target, str):
        matches = [i for i, b in enumerate(bars) if b["date"] == target]
        if not matches:
            raise ValueError(
                f"La date '{target}' est absente de l'historique D1 récupéré pour '{symbol}'. "
                f"L'historique couvre {bars[0]['date']} – {bars[-1]['date']}."
            )
        j_idx = matches[0]
        is_closed_bar = bars[j_idx]["timestamp"] + 86400 <= server_now

    else:
        raise ValueError(
            f"target doit être None, un entier négatif ou une chaîne 'YYYY-MM-DD' ; "
            f"reçu {target!r}"
        )

    return j_idx, is_closed_bar


def _fetch_context(
    symbol: str,
    target: str | int | None,
    history_days: int,
) -> tuple[list[dict], int, dict, bool, list[dict] | None]:
    """
    Facteur commun de analyze_symbol() et analyze_symbol_full().

    Effectue la vérification de session, la récupération D1 + H1, et la
    résolution du jour J.

    Retourne (bars, j_idx, j_bar, is_closed_bar, h1_bars).
    """
    mt5_data.check_session()
    bars = mt5_data.fetch_d1_bars(symbol, history_days)
    server_now = mt5_data.get_current_server_time(symbol)
    j_idx, is_closed_bar = _resolve_j_idx(bars, target, server_now, symbol)
    j_bar = bars[j_idx]

    try:
        h1_bars: list[dict] | None = mt5_data.fetch_h1_bars_for_day(
            symbol, j_bar["timestamp"]
        )
    except MT5DataError:
        h1_bars = None

    return bars, j_idx, j_bar, is_closed_bar, h1_bars


def _build_details(
    bars: list[dict],
    j_idx: int,
    j_bar: dict,
    h1_bars: list[dict] | None,
    r_j: float,
    doji_threshold: float,
) -> dict:
    """Construit la section 'details' (données brutes pour affichage UI)."""
    lookback = min(7, j_idx)
    detail_bars = [
        {
            "offset": k,
            "date":  bars[j_idx - k]["date"],
            "open":  bars[j_idx - k]["open"],
            "high":  bars[j_idx - k]["high"],
            "low":   bars[j_idx - k]["low"],
            "close": bars[j_idx - k]["close"],
            "range": round(bars[j_idx - k]["high"] - bars[j_idx - k]["low"], 8),
        }
        for k in range(lookback + 1)   # k=0 → J, k=1 → J-1, …
    ]

    trend_detail = None
    if h1_bars and len(h1_bars) > 0:
        fh = h1_bars[0]
        trend_detail = {
            "first_h1_range": round(fh["high"] - fh["low"], 8),
            "daily_10pct":    round(0.10 * r_j, 8),
        }

    return {
        "bars": detail_bars,
        "doji": {
            "open":            j_bar["open"],
            "close":           j_bar["close"],
            "body":            round(abs(j_bar["close"] - j_bar["open"]), 8),
            "range":           round(r_j, 8),
            "threshold_value": round(doji_threshold * r_j, 8),
        },
        "trend_day": trend_detail,
    }


# ── API publique ─────────────────────────────────────────────────────────────


def analyze_symbol(
    symbol: str,
    target: str | int | None = None,
    *,
    doji_threshold: float = 0.10,
    inside_strict: bool = True,
    history_days: int = 60,
) -> dict:
    """
    Analyse les signaux de prix Toby Crabel pour *symbol* sur une journée D1.

    Paramètres
    ----------
    symbol        : Symbole MT5 (ex. "EURUSD", "US100").
    target        : Jour à analyser.
                    None      → dernière D1 clôturée (défaut).
                    int < 0   → décalage depuis la dernière clôturée (-1 = avant-dernière).
                    "YYYY-MM-DD" → jour précis ; ValueError si absent de l'historique.
    doji_threshold: Seuil corps/range pour le Doji (défaut 0.10).
    inside_strict : True = inégalités strictes pour l'inside day.
    history_days  : Nombre de bougies D1 à récupérer (≥ 48 requis pour 8BNR).

    Retour
    ------
    dict avec les clés :
        symbol, analyzed_date, is_closed_bar, range, signals, combined,
        contraction_rank, expansion_flag, data_meta, notes, details

    Exceptions
    ----------
    MT5SessionNotInitialized : aucune session MT5 active.
    SymbolNotFound           : symbole absent de MT5.
    MT5DataError             : MT5 a retourné None ou vide.
    ValueError               : target invalide ou hors plage.
    """
    bars, j_idx, j_bar, is_closed_bar, h1_bars = _fetch_context(
        symbol, target, history_days
    )
    r_j = j_bar["high"] - j_bar["low"]

    result = compute_signals(
        bars, h1_bars, j_idx,
        doji_threshold=doji_threshold,
        inside_strict=inside_strict,
    )

    return {
        "symbol":           symbol,
        "analyzed_date":    j_bar["date"],
        "is_closed_bar":    is_closed_bar,
        "range":            round(r_j, 8),
        "signals":          result["signals"],
        "combined":         result["combined"],
        "contraction_rank": result["contraction_rank"],
        "expansion_flag":   result["expansion_flag"],
        "data_meta": {
            "d1_bars": len(bars),
            "h1_bars": len(h1_bars) if h1_bars is not None else 0,
            "tz": "server",
        },
        "notes":   result["notes"],
        "details": _build_details(bars, j_idx, j_bar, h1_bars, r_j, doji_threshold),
    }


def analyze_symbol_full(
    symbol: str,
    target: str | int | None = None,
    *,
    doji_threshold: float = 0.10,
    inside_strict: bool = True,
    history_days: int = 60,
    stretch_window: int = 10,
    stretch_window_offset: int = 1,
    atr_period: int = 14,
    sl_atr_k: float = 1.0,
) -> dict:
    """
    Analyse complète : signaux Crabel + Stretch + niveaux ORB + options de SL.

    Étend analyze_symbol() sans modifier son comportement.  La session MT5 est
    vérifiée et les données D1/H1 sont récupérées une seule fois.

    Paramètres supplémentaires
    --------------------------
    stretch_window        : Nombre de jours de la fenêtre Stretch (défaut 10).
    stretch_window_offset : Offset depuis J ; 1 = fenêtre termine en J-1.
    atr_period            : Période ATR pour l'option de SL (défaut 14).
    sl_atr_k              : Multiplicateur ATR pour la distance de SL (défaut 1.0).

    Sections supplémentaires dans le retour
    ----------------------------------------
    stretch               : valeur, dates, distances par jour.
    orb_levels_estimated  : niveaux estimés basés sur close(J).
    orb_levels_next_day   : formules paramétrées par open(J+1).
    sl_options            : 4 approches de stop-loss.
    tradeability_hint     : synthèse pratique non normative.

    Exceptions (en plus de analyze_symbol)
    ----------------------------------------
    InsufficientHistoryForStretch : historique trop court pour le Stretch.
    MalformedBarData              : open hors [low, high] dans la fenêtre Stretch.
    """
    # ── 1. Récupération MT5 + résolution J ───────────────────────────────────
    bars, j_idx, j_bar, is_closed_bar, h1_bars = _fetch_context(
        symbol, target, history_days
    )
    r_j = j_bar["high"] - j_bar["low"]

    # ── 2. Signaux Crabel ────────────────────────────────────────────────────
    signals_result = compute_signals(
        bars, h1_bars, j_idx,
        doji_threshold=doji_threshold,
        inside_strict=inside_strict,
    )
    notes = signals_result["notes"]

    # ── 3. Stretch (lève une exception si historique insuffisant) ────────────
    stretch_result = compute_stretch(
        bars, j_idx,
        window=stretch_window,
        offset=stretch_window_offset,
    )
    stretch_value = stretch_result["value"]

    # ── 4. ATR — optionnel, None si données insuffisantes ───────────────────
    atr_val = compute_atr(bars, j_idx, atr_period)
    if atr_val is None:
        notes = notes + [
            f"atr_based: historique insuffisant pour ATR({atr_period}) "
            f"(besoin {atr_period + 1} barres incluant J)"
        ]

    # ── 5. Niveaux ORB estimés (basés sur close(J)) ──────────────────────────
    close_j = j_bar["close"]
    orb_levels_estimated = {
        "based_on":            "close of J",
        "close_of_j":          round(close_j, 8),
        "buy_stop_estimated":  round(close_j + stretch_value, 8),
        "sell_stop_estimated": round(close_j - stretch_value, 8),
        "note": (
            "estimation basée sur close(J) ; "
            "les vrais niveaux dépendent de open(J+1)"
        ),
    }

    # ── 6. Formules ORB pour J+1 ─────────────────────────────────────────────
    orb_levels_next_day = {
        "formula_buy_stop":  f"open_next + {stretch_value}",
        "formula_sell_stop": f"open_next - {stretch_value}",
        "to_apply_at":       "ouverture de la prochaine session J+1",
    }

    # ── 7. Options de stop-loss ──────────────────────────────────────────────
    j_high = round(j_bar["high"], 8)
    j_low  = round(j_bar["low"],  8)

    atr_based: dict
    if atr_val is not None:
        atr_based = {
            "description": f"SL = {sl_atr_k} × ATR({atr_period})",
            "atr_14":      atr_val,
            "k":           sl_atr_k,
            "distance":    round(sl_atr_k * atr_val, 8),
        }
    else:
        atr_based = {
            "description": f"SL = {sl_atr_k} × ATR({atr_period})",
            "atr_14":      None,
            "k":           sl_atr_k,
            "distance":    None,
            "note": (
                f"ATR non calculable : historique insuffisant "
                f"(besoin {atr_period + 1} barres incluant J)"
            ),
        }

    sl_options = {
        "stretch_opposite": {
            "description": "SL au breakout opposé (≈ 2× Stretch)",
            "distance":    round(2 * stretch_value, 8),
            "buy_sl":      f"open_next - {stretch_value}",
            "sell_sl":     f"open_next + {stretch_value}",
        },
        "opposite_or_extreme": {
            "description": (
                "SL à l'extrême opposé de l'opening range — "
                "connu uniquement après la formation de l'OR"
            ),
            "distance": None,
            "note": "à calculer après la formation de l'opening range J+1",
        },
        "previous_day_extreme": {
            "description": "SL aux extrêmes du jour J (la barre de contraction)",
            "buy_sl":                  j_low,
            "sell_sl":                 j_high,
            "distance_long_estimate":  f"open_next - {j_low}",
            "distance_short_estimate": f"{j_high} - open_next",
        },
        "atr_based": atr_based,
    }

    # ── 8. Tradeability hint ──────────────────────────────────────────────────
    hint = compute_tradeability_hint(
        signals_result["signals"],
        signals_result["combined"],
        signals_result["contraction_rank"],
        signals_result["expansion_flag"],
    )

    return {
        # ── existant (même structure que analyze_symbol) ─────────────────────
        "symbol":           symbol,
        "analyzed_date":    j_bar["date"],
        "is_closed_bar":    is_closed_bar,
        "range":            round(r_j, 8),
        "signals":          signals_result["signals"],
        "combined":         signals_result["combined"],
        "contraction_rank": signals_result["contraction_rank"],
        "expansion_flag":   signals_result["expansion_flag"],
        "data_meta": {
            "d1_bars": len(bars),
            "h1_bars": len(h1_bars) if h1_bars is not None else 0,
            "tz": "server",
        },
        "notes":   notes,
        "details": _build_details(bars, j_idx, j_bar, h1_bars, r_j, doji_threshold),
        # ── nouveau ──────────────────────────────────────────────────────────
        "stretch":               stretch_result,
        "orb_levels_estimated":  orb_levels_estimated,
        "orb_levels_next_day":   orb_levels_next_day,
        "sl_options":            sl_options,
        "tradeability_hint":     hint,
    }
