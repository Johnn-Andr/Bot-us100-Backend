"""
Layer (a) — MT5 data access.

All MetaTrader5 API calls are isolated here.  The rest of the package
(calculator.py, signals.py) never imports MetaTrader5 directly so the
pure logic layer is testable without a running terminal.

RÈGLE STRICTE : ce module ne doit JAMAIS appeler mt5.initialize(),
mt5.login() ou mt5.shutdown().  Ces appels appartiennent à l'application.
"""

from __future__ import annotations

from datetime import datetime, timezone

import MetaTrader5 as mt5

from .exceptions import MT5DataError, MT5SessionNotInitialized, SymbolNotFound


def _utc_dt(ts: int) -> datetime:
    """Convertit un timestamp Unix en datetime naïf traité comme UTC, sans tz locale."""
    return datetime.fromtimestamp(ts, tz=timezone.utc).replace(tzinfo=None)

# HYPOTHÈSE FUSEAU:
# Les timestamps renvoyés par MT5 (champ `time`) représentent l'heure du
# serveur MT5 exprimée en secondes Unix.  Ce module les traite comme des
# entiers bruts et construit les dates affichées avec _utc_dt(),
# ce qui est exact uniquement si le serveur MT5 tourne en UTC.
# Pour un serveur en EET (UTC+2 / UTC+3), les dates peuvent différer d'une
# unité au voisinage de minuit serveur.  Aucune conversion implicite n'est
# effectuée ; l'appelant est prévenu ici.


def check_session() -> None:
    """
    Vérifier qu'une session MT5 active existe.

    Lève MT5SessionNotInitialized si mt5.terminal_info() retourne None,
    signe que mt5.initialize() n'a pas été appelé (ou a échoué).

    Ce module ne tente PAS de corriger la situation — c'est une anomalie
    de configuration de l'application.
    """
    if mt5.terminal_info() is None:
        raise MT5SessionNotInitialized(
            "Aucune session MT5 active détectée (mt5.terminal_info() is None). "
            "L'application doit appeler mt5.initialize() — et mt5.login() si "
            "nécessaire — avant d'utiliser ce module.  Ce module ne le fera jamais."
        )


def _ensure_symbol_visible(symbol: str) -> None:
    """Rendre le symbole visible dans le Market Watch, ou lever SymbolNotFound."""
    info = mt5.symbol_info(symbol)
    if info is None:
        raise SymbolNotFound(
            f"Symbole '{symbol}' introuvable dans MT5.  "
            f"Vérifiez l'orthographe et que le symbole est disponible sur ce broker."
        )
    if not info.visible:
        if not mt5.symbol_select(symbol, True):
            raise SymbolNotFound(
                f"Symbole '{symbol}' trouvé mais impossible de le sélectionner "
                f"(visible=False, symbol_select a échoué) : {mt5.last_error()}"
            )


def fetch_d1_bars(symbol: str, count: int) -> list[dict]:
    """
    Récupère les `count` dernières bougies D1 pour `symbol`, triées par date
    croissante (la plus ancienne en tête, la plus récente en queue).

    Retourne une liste de dicts avec les clés :
        timestamp (int)   — heure serveur Unix du début de la bougie
        date      (str)   — "YYYY-MM-DD" construit depuis timestamp (cf. HYPOTHÈSE FUSEAU)
        open, high, low, close (float)

    Lève MT5DataError si MT5 renvoie None ou un tableau vide.
    """
    _ensure_symbol_visible(symbol)
    rates = mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_D1, 0, count)
    if rates is None or len(rates) == 0:
        raise MT5DataError(
            f"copy_rates_from_pos(D1, count={count}) a retourné vide pour "
            f"'{symbol}' : {mt5.last_error()}"
        )
    # MT5 retourne les barres triées par temps croissant (la plus ancienne d'abord).
    return [
        {
            "timestamp": int(r["time"]),
            "date": _utc_dt(int(r["time"])).strftime("%Y-%m-%d"),
            "open": float(r["open"]),
            "high": float(r["high"]),
            "low": float(r["low"]),
            "close": float(r["close"]),
        }
        for r in rates
    ]


def fetch_h1_bars_for_day(symbol: str, day_timestamp: int) -> list[dict]:
    """
    Récupère les bougies H1 dont l'heure d'ouverture se situe dans la fenêtre
    [day_timestamp, day_timestamp + 86400).

    Utilise copy_rates_range avec des datetime naïfs construits depuis les
    timestamps bruts (cf. HYPOTHÈSE FUSEAU) pour éviter toute conversion de
    fuseau implicite.

    Retourne une liste vide si aucune donnée H1 n'existe pour ce jour
    (ex. week-end, férié) — ce n'est pas une erreur.
    Lève MT5DataError si MT5 renvoie None (vraie erreur API).
    """
    _ensure_symbol_visible(symbol)
    # Bornes construites depuis les timestamps serveur bruts, sans conversion tz.
    dt_start = _utc_dt(day_timestamp)
    dt_end = _utc_dt(day_timestamp + 86400)
    rates = mt5.copy_rates_range(symbol, mt5.TIMEFRAME_H1, dt_start, dt_end)
    if rates is None:
        raise MT5DataError(
            f"copy_rates_range(H1) a retourné None pour '{symbol}' "
            f"(jour {_utc_dt(day_timestamp).date()}) : "
            f"{mt5.last_error()}"
        )
    if len(rates) == 0:
        return []
    bars = [
        {
            "timestamp": int(r["time"]),
            "date": _utc_dt(int(r["time"])).strftime("%Y-%m-%d"),
            "open": float(r["open"]),
            "high": float(r["high"]),
            "low": float(r["low"]),
            "close": float(r["close"]),
        }
        for r in rates
        # Filtre de sécurité : s'assure que les barres appartiennent bien au jour J.
        if day_timestamp <= int(r["time"]) < day_timestamp + 86400
    ]
    return sorted(bars, key=lambda b: b["timestamp"])


def get_current_server_time(symbol: str) -> int:
    """
    Retourne l'horodatage serveur courant approximatif via le dernier tick du symbole.

    Lève MT5DataError si aucun tick n'est disponible.
    """
    tick = mt5.symbol_info_tick(symbol)
    if tick is None:
        raise MT5DataError(
            f"symbol_info_tick('{symbol}') a retourné None : {mt5.last_error()}"
        )
    return int(tick.time)
