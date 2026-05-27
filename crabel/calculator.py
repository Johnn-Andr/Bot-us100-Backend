"""
Layer (b) — Pure Crabel signal calculator.

No MetaTrader5 dependency.  Operates on plain Python lists of bar dicts so
that this layer can be unit-tested without a running terminal.

Bar dict schema (produced by mt5_data.fetch_d1_bars / fetch_h1_bars_for_day):
    timestamp (int), date (str), open (float), high (float),
    low (float), close (float)

Reference: Toby Crabel, "Day Trading with Short Term Price Patterns and
Opening Range Breakout" (1990).

RÈGLE CRABEL — Comparaisons NR/WS en inégalité STRICTE :
Crabel définit les jours NR (Narrow Range) et WS (Wide Strength) par des
inégalités strictes.  Un range strictement égal au jour précédent n'est ni
NR ni WS.  Toute comparaison d'égalité retourne donc False pour ces signaux.
"""

from __future__ import annotations

import math
from typing import Any

from .exceptions import InsufficientHistoryForStretch, MalformedBarData

Bar = dict[str, Any]

# Mapping window-size → nom lisible pour les notes et les erreurs.
_NBAR_NAME: dict[int, str] = {
    2: "two_bar_nr",
    3: "three_bar_nr",
    4: "four_bar_nr",
    8: "eight_bar_nr",
}


# ── Helpers de bas niveau ────────────────────────────────────────────────────


def _range(bar: Bar) -> float:
    """R(bar) = high - low.  Crabel range : PAS close-open."""
    return bar["high"] - bar["low"]


def _is_bar_valid(bar: Bar, fields: tuple[str, ...] = ("open", "high", "low", "close")) -> bool:
    """Retourne False si un champ demandé est absent ou NaN."""
    for f in fields:
        v = bar.get(f)
        if v is None or (isinstance(v, float) and math.isnan(v)):
            return False
    return True


def _span_n(bars: list[Bar], end_idx: int, n: int) -> float | None:
    """
    Span N-barres finissant en bars[end_idx] :
        max(high[end_idx-n+1 .. end_idx]) - min(low[end_idx-n+1 .. end_idx])

    Retourne None si la fenêtre déborde à gauche ou si une barre contient NaN.
    """
    start = end_idx - n + 1
    if start < 0:
        return None
    window = bars[start : end_idx + 1]
    try:
        highs = [b["high"] for b in window]
        lows = [b["low"] for b in window]
        if any(math.isnan(v) for v in highs + lows):
            return None
        return max(highs) - min(lows)
    except (KeyError, TypeError):
        return None


def _safe_and(a: bool | None, b: bool | None) -> bool | None:
    """a AND b ; None si l'un des opérandes est None."""
    if a is None or b is None:
        return None
    return a and b


# ── Signaux individuels ──────────────────────────────────────────────────────


def _nr_x(bars: list[Bar], j: int, n: int, notes: list[str]) -> bool | None:
    """
    NRn : R(J) strictement inférieur à chacun de R(J-1) … R(J-(n-1)).

    Crabel (strict) : égalité ⇒ NR NON validé.
    None si historique insuffisant ou NaN détecté.
    """
    needed = n - 1
    if j < needed:
        notes.append(f"nr{n}: historique insuffisant (besoin {needed} barres avant J, j_idx={j})")
        return None
    r_j = _range(bars[j])
    if math.isnan(r_j):
        notes.append(f"nr{n}: R(J) est NaN")
        return None
    for k in range(1, n):
        prev = bars[j - k]
        if not _is_bar_valid(prev, ("high", "low")):
            notes.append(f"nr{n}: barre J-{k} contient NaN sur high/low")
            return None
        # STRICT — Crabel : égalité ⇒ NR NON validé
        if not (r_j < _range(prev)):
            return False
    return True


def _ws_x(bars: list[Bar], j: int, n: int, notes: list[str]) -> bool | None:
    """
    WSn (Wide Strength) : R(J) strictement supérieur à chacun de R(J-1) … R(J-(n-1)).

    Crabel (strict) : égalité ⇒ WS NON validé.
    None si historique insuffisant ou NaN détecté.
    """
    needed = n - 1
    if j < needed:
        notes.append(f"ws{n}: historique insuffisant (besoin {needed} barres avant J, j_idx={j})")
        return None
    r_j = _range(bars[j])
    if math.isnan(r_j):
        notes.append(f"ws{n}: R(J) est NaN")
        return None
    for k in range(1, n):
        prev = bars[j - k]
        if not _is_bar_valid(prev, ("high", "low")):
            notes.append(f"ws{n}: barre J-{k} contient NaN sur high/low")
            return None
        # STRICT — Crabel : égalité ⇒ WS NON validé
        if not (r_j > _range(prev)):
            return False
    return True


def _inside_day(bars: list[Bar], j: int, strict: bool, notes: list[str]) -> bool | None:
    """
    Inside day :
        strict=True  → high(J) <  high(J-1) ET low(J) >  low(J-1)
        strict=False → high(J) <= high(J-1) ET low(J) >= low(J-1)
    """
    if j < 1:
        notes.append("inside_day: pas de barre J-1 disponible")
        return None
    cur, prev = bars[j], bars[j - 1]
    if not _is_bar_valid(cur, ("high", "low")) or not _is_bar_valid(prev, ("high", "low")):
        notes.append("inside_day: NaN sur high/low de J ou J-1")
        return None
    if strict:
        return cur["high"] < prev["high"] and cur["low"] > prev["low"]
    return cur["high"] <= prev["high"] and cur["low"] >= prev["low"]


def _doji(bar: Bar, r_j: float, threshold: float) -> bool | None:
    """
    Doji : |close(J) - open(J)| <= threshold * R(J).

    Cas limite R(J)==0 : vrai uniquement si open==close (corps nul sur range nul).
    Aucune division par zéro.
    """
    if not _is_bar_valid(bar):
        return None
    body = abs(bar["close"] - bar["open"])
    if r_j == 0.0:
        return body == 0.0
    return body <= threshold * r_j


def _n_bar_nr(
    bars: list[Bar],
    j: int,
    window: int,
    lookback: int,
    notes: list[str],
) -> bool | None:
    """
    N-bar Narrow Range :
        span_n(J) strictement < span_n(k) pour chaque k de J-1 à J-lookback.

        span_n(d) = max(high[d-n+1..d]) - min(low[d-n+1..d])

    Crabel (strict) : égalité ⇒ N-bar NR NON validé.

    Retourne None si l'historique est insuffisant pour J ou pour la comparaison
    la plus lointaine (besoin ~lookback jours avant J).
    """
    name = _NBAR_NAME.get(window, f"{window}_bar_nr")
    # Indice minimum requis : la fenêtre la plus lointaine ends at j-lookback,
    # et démarre à j - lookback - (window-1).  Il faut donc j >= lookback+window-1.
    min_j = lookback + window - 1
    if j < min_j:
        notes.append(
            f"{name}: historique insuffisant (besoin ~{lookback} jours avant J, j_idx={j})"
        )
        return None
    span_j = _span_n(bars, j, window)
    if span_j is None:
        notes.append(f"{name}: NaN dans la fenêtre courante de J")
        return None
    for k in range(1, lookback + 1):
        span_k = _span_n(bars, j - k, window)
        if span_k is None:
            notes.append(f"{name}: NaN dans la fenêtre de comparaison à J-{k}")
            return None
        # STRICT — Crabel : égalité ⇒ N-bar NR NON validé
        if not (span_j < span_k):
            return False
    return True


def _trend_day(h1_bars: list[Bar] | None, r_j: float, notes: list[str]) -> bool | None:
    """
    Trend day : range de la 1re heure de J < 0.10 * R(J).

    Retourne None uniquement si les données H1 sont indisponibles (MT5 error ou
    liste vide) — jamais False par défaut.
    """
    if h1_bars is None:
        notes.append("trend_day: données H1 indisponibles (erreur MT5 lors de la récupération)")
        return None
    if len(h1_bars) == 0:
        notes.append("trend_day: aucune barre H1 trouvée pour le jour J (marché fermé ou données absentes)")
        return None
    first = h1_bars[0]
    if not _is_bar_valid(first, ("high", "low")):
        notes.append("trend_day: NaN sur high/low de la première barre H1")
        return None
    first_range = _range(first)
    if r_j == 0.0:
        return first_range == 0.0
    return first_range < 0.10 * r_j


# ── Stretch de Crabel ────────────────────────────────────────────────────────


def compute_stretch(
    bars: list[Bar],
    j_idx: int,
    window: int = 10,
    offset: int = 1,
) -> dict[str, Any]:
    """
    Stretch de Crabel.

    Crabel (1990) : « STRETCH is determined by looking at the previous ten days
    and averaging the sum of the differences between the open and the closest
    extreme to the open for each day. »

    Algorithme :
        Pour chaque jour d dans la fenêtre [j_idx-offset-window+1 .. j_idx-offset] :
            distance_to_low  = open(d) - low(d)   ≥ 0
            distance_to_high = high(d) - open(d)  ≥ 0
            distance(d)      = min(distance_to_low, distance_to_high)
        stretch = moyenne arithmétique des `window` distances.

    Paramètres
    ----------
    bars   : liste de barres D1 triées par date croissante.
    j_idx  : index du jour analysé J dans bars.
    window : nombre de jours dans la fenêtre (défaut 10 — Crabel strict).
    offset : décalage depuis J ; 1 = fenêtre se termine en J-1 (J exclu).

    Lève
    ----
    InsufficientHistoryForStretch : si bars ne contient pas assez de jours avant J.
    MalformedBarData              : si open est hors de [low, high] sur un jour de
                                    la fenêtre (anomalie de données, ne devrait pas
                                    arriver sur des données broker propres).
    """
    end_idx   = j_idx - offset              # index inclusif de la fin de fenêtre
    start_idx = end_idx - window + 1        # index inclusif du début de fenêtre

    if start_idx < 0:
        available = max(0, end_idx + 1)
        raise InsufficientHistoryForStretch(
            f"Historique insuffisant pour Stretch({window}) : "
            f"{available} jour(s) disponible(s) avant J (offset={offset}), "
            f"{window} requis. "
            "Augmentez history_days dans l'appel à analyze_symbol_full()."
        )

    distances: list[float] = []
    window_dates: list[str] = []

    for i in range(start_idx, end_idx + 1):
        b = bars[i]

        if not _is_bar_valid(b, ("open", "high", "low")):
            raise MalformedBarData(
                f"Barre du {b.get('date', f'index {i}')} contient NaN "
                "sur open/high/low — impossible de calculer le Stretch."
            )

        # Vérification de cohérence OHLC : open doit être dans [low, high].
        # Une valeur hors range indique une corruption de données en amont.
        if b["open"] < b["low"] or b["open"] > b["high"]:
            raise MalformedBarData(
                f"Barre du {b['date']} : open={b['open']} hors de "
                f"[low={b['low']}, high={b['high']}]. "
                "Donnée corrompue — vérifiez le feed de données."
            )

        dist_low  = b["open"] - b["low"]    # ≥ 0 garanti par le check ci-dessus
        dist_high = b["high"] - b["open"]   # ≥ 0 garanti par le check ci-dessus
        dist = min(dist_low, dist_high)

        # Garde-fou redondant (ne devrait jamais s'activer après les checks).
        if dist < 0:
            raise MalformedBarData(
                f"Barre du {b['date']} : distance négative ({dist}) — "
                "anomalie inattendue après validation OHLC."
            )

        distances.append(round(dist, 8))
        window_dates.append(b["date"])

    stretch_value = sum(distances) / window

    return {
        "value":            round(stretch_value, 8),
        "window_days":      window,
        "window_dates":     window_dates,
        "per_day_distances": distances,
        "method": (
            f"average of min(open-low, high-open) "
            f"over previous {window} days (J-{offset+window-1} to J-{offset})"
        ),
    }


# ── ATR (14) ─────────────────────────────────────────────────────────────────


def compute_atr(
    bars: list[Bar],
    j_idx: int,
    period: int = 14,
) -> float | None:
    """
    ATR classique (Wilder) sur `period` jours jusqu'à bars[j_idx] inclus.

        TR(d) = max(
            high(d) - low(d),
            |high(d) - close(d-1)|,
            |low(d)  - close(d-1)|,
        )
        ATR = moyenne arithmétique des `period` derniers TR.

    Retourne None si l'historique est insuffisant (besoin : j_idx >= period,
    soit period+1 barres incluant bars[j_idx-period] pour close(d-1) du
    premier TR).  Pas d'erreur — l'ATR est un SL optionnel.

    Retourne également None si une barre de la fenêtre contient NaN.
    """
    # Besoin de period TRs ; le premier TR (à j_idx-period+1) utilise
    # bars[j_idx-period].close comme close(d-1).
    if j_idx < period:
        return None

    trs: list[float] = []
    for i in range(j_idx - period + 1, j_idx + 1):
        b  = bars[i]
        bp = bars[i - 1]
        if not _is_bar_valid(b, ("high", "low")) or not _is_bar_valid(bp, ("close",)):
            return None     # NaN dans la fenêtre ATR → pas d'ATR
        tr = max(
            b["high"] - b["low"],
            abs(b["high"] - bp["close"]),
            abs(b["low"]  - bp["close"]),
        )
        trs.append(tr)

    return round(sum(trs) / period, 8)


# ── Tradeability hint ────────────────────────────────────────────────────────


def compute_tradeability_hint(
    signals: dict[str, Any],
    combined: dict[str, Any],
    contraction_rank: int,
    expansion_flag: bool,
) -> dict[str, Any]:
    """
    Synthèse pratique non normative combinant les signaux pour orienter la décision.

    AVERTISSEMENT : Ce champ est une aide à la décision indicative UNIQUEMENT.
    Il ne remplace ni le jugement du trader ni un backtest rigoureux sur données
    historiques.  Les heuristiques implémentées ici ne constituent PAS une
    stratégie de trading validée.

    Règles (ordre de priorité) :
    1. expansion_flag = True → confidence="low" (risque de faux breakout).
    2. contraction_rank >= 7 (inside+NR4/NR7) → confidence="high".
    3. contraction_rank >= 4 (NR7, 2BNR, 3BNR) → confidence="medium".
    4. contraction_rank >= 1 → confidence="low".
    5. Sinon → confidence="low", pas de configuration.

    La direction préférée est toujours null : avec Crabel ORB, c'est le
    premier breakout (buy_stop ou sell_stop) qui détermine la direction.
    """
    if expansion_flag:
        return {
            "preferred_direction": None,
            "confidence": "low",
            "reason": (
                "expansion la veille (WS4/WS7/trend_day) — "
                "risque de faux breakout ORB, envisager fade plutôt qu'ORB"
            ),
        }

    if contraction_rank >= 7:
        return {
            "preferred_direction": None,
            "confidence": "high",
            "reason": (
                "contraction double (inside+NR) — "
                "configuration forte pour ORB ; "
                "direction déterminée par le premier breakout"
            ),
        }

    if contraction_rank >= 4:
        return {
            "preferred_direction": None,
            "confidence": "medium",
            "reason": "contraction marquée — conditions favorables à un ORB",
        }

    if contraction_rank >= 1:
        return {
            "preferred_direction": None,
            "confidence": "low",
            "reason": (
                "contraction modérée — "
                "exiger Early Entry ou confirmation supplémentaire avant d'entrer"
            ),
        }

    return {
        "preferred_direction": None,
        "confidence": "low",
        "reason": (
            "pas de configuration de contraction claire — "
            "éviter l'ORB ou attendre un signal de setup"
        ),
    }


# ── Point d'entrée public de la couche (signaux) ────────────────────────────


def compute_signals(
    bars: list[Bar],
    h1_bars: list[Bar] | None,
    j_idx: int,
    *,
    doji_threshold: float = 0.10,
    inside_strict: bool = True,
) -> dict[str, Any]:
    """
    Calcule tous les signaux Crabel pour la barre d'index j_idx dans bars.

    Paramètres
    ----------
    bars          : Bougies D1 triées par date croissante.
    h1_bars       : Bougies H1 du jour J (croissant). None = indisponible.
    j_idx         : Index du jour J dans bars.
    doji_threshold: Seuil corps/range pour le Doji (défaut 0.10).
    inside_strict : True = inégalités strictes pour l'inside day.

    Retourne
    --------
    dict avec les clés : signals, combined, contraction_rank, expansion_flag, notes.
    """
    notes: list[str] = []
    j = bars[j_idx]

    if not _is_bar_valid(j):
        notes.append("La barre J contient des valeurs NaN/manquantes — tous les signaux sont None")
        return _null_result(notes)

    r_j = _range(j)

    # ── Contraction ──────────────────────────────────────────────────────────

    nr4 = _nr_x(bars, j_idx, 4, notes)
    nr7 = _nr_x(bars, j_idx, 7, notes)
    inside = _inside_day(bars, j_idx, inside_strict, notes)
    doji = _doji(j, r_j, doji_threshold)

    two_bar_nr = _n_bar_nr(bars, j_idx, 2, 20, notes)
    three_bar_nr = _n_bar_nr(bars, j_idx, 3, 20, notes)
    four_bar_nr = _n_bar_nr(bars, j_idx, 4, 30, notes)
    eight_bar_nr = _n_bar_nr(bars, j_idx, 8, 40, notes)

    # ── Expansion ────────────────────────────────────────────────────────────

    ws4 = _ws_x(bars, j_idx, 4, notes)
    ws7 = _ws_x(bars, j_idx, 7, notes)
    trend = _trend_day(h1_bars, r_j, notes)

    # ── Combinés ─────────────────────────────────────────────────────────────

    inside_and_nr4 = _safe_and(inside, nr4)
    inside_and_nr7 = _safe_and(inside, nr7)

    # expansion_flag = True si ws4 OU ws7 est activement True.
    # None est traité comme "inconnu, pas True" — si ws4=True et ws7=None,
    # on sait déjà qu'il y a expansion.
    expansion_flag = (ws4 is True) or (ws7 is True)

    # ── Contraction rank ─────────────────────────────────────────────────────
    # Table de priorité Crabel (rang le plus élevé = signal le plus significatif).
    # four_bar_nr et eight_bar_nr ne figurent pas dans la table de rang définie
    # par la spécification ; ils sont présents dans signals mais pas dans le rang.
    _rank_table = [
        (8, inside_and_nr7),
        (7, inside_and_nr4),
        (6, three_bar_nr),
        (5, two_bar_nr),
        (4, nr7),
        (3, doji),
        (2, nr4),
        (1, inside),
    ]
    contraction_rank = 0
    for rank, signal in _rank_table:
        if signal is True:
            contraction_rank = rank
            break

    return {
        "signals": {
            "nr4": nr4,
            "nr7": nr7,
            "inside_day": inside,
            "doji": doji,
            "two_bar_nr": two_bar_nr,
            "three_bar_nr": three_bar_nr,
            "four_bar_nr": four_bar_nr,
            "eight_bar_nr": eight_bar_nr,
            "ws4": ws4,
            "ws7": ws7,
            "trend_day": trend,
        },
        "combined": {
            "inside_and_nr4": inside_and_nr4,
            "inside_and_nr7": inside_and_nr7,
        },
        "contraction_rank": contraction_rank,
        "expansion_flag": expansion_flag,
        "notes": notes,
    }


def _null_result(notes: list[str]) -> dict[str, Any]:
    """Résultat vide quand la barre J elle-même est invalide."""
    return {
        "signals": {k: None for k in (
            "nr4", "nr7", "inside_day", "doji",
            "two_bar_nr", "three_bar_nr", "four_bar_nr", "eight_bar_nr",
            "ws4", "ws7", "trend_day",
        )},
        "combined": {"inside_and_nr4": None, "inside_and_nr7": None},
        "contraction_rank": 0,
        "expansion_flag": False,
        "notes": notes,
    }


# AMBIGUÏTÉ:
#
# 1. Détection « bougie clôturée » (voir aussi signals.py) :
#    La règle utilisée (D1 clôturée si timestamp_barre + 86400 ≤ server_time)
#    suppose une journée de 24 h exactes.  Pour les instruments boursiers avec
#    une session de marché définie (ex. indices), la bougie D1 peut se fermer
#    avant minuit serveur + 24 h.  La règle est donc conservative (peut désigner
#    une barre ouverte comme clôturée en fin de session) mais constitue la
#    meilleure approximation sans API exposant l'heure de clôture de session.
#
# 2. Frontière de journée du H1 :
#    datetime.utcfromtimestamp(day_timestamp) est utilisé comme borne de
#    copy_rates_range.  Si le serveur MT5 n'est pas en UTC, les barres H1
#    récupérées peuvent ne pas coïncider exactement avec la journée serveur
#    définie par le timestamp D1.  Le filtre secondaire
#    `day_timestamp <= ts < day_timestamp + 86400` compense partiellement,
#    mais si la session ouvre à une heure != minuit UTC et que le serveur est
#    en UTC+2, le premier H1 renvoyé par copy_rates_range peut correspondre à
#    l'avant-dernière heure de la veille.  Solution robuste : utiliser
#    copy_rates_from_pos avec un compte suffisant et filtrer par timestamp.
#
# 3. Stretch et jours fériés / jours à range nul (ex. Nasdaq fermé) :
#    La spécification précise que les jours à range nul sont inclus avec
#    distance = 0 (valide).  Pour le Nasdaq (US100), les jours fériés
#    américains (Thanksgiving, Christmas…) peuvent apparaître comme des
#    bougies D1 avec range quasi nul selon le broker.  Inclure ces jours
#    réduit mécaniquement le Stretch.  Il n'existe pas de règle Crabel
#    explicite sur ce cas ; la décision prise ici est de les inclure, ce
#    qui est cohérent avec la définition textuelle.
#
# 4. Stretch et barres de week-end :
#    MT5 ne retourne généralement pas de bougies D1 pour les week-ends sur
#    les instruments forex/indices ; la fenêtre de 10 jours correspond donc
#    à 10 jours de trading effectifs.  Si un broker inclut des bougies
#    week-end synthétiques (ex. certains CFD crypto), le Stretch serait
#    calculé sur un mix jours ouvrés/non ouvrés.  Ce cas n'est pas géré
#    car il n'est pas documenté par Crabel.
