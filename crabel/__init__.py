"""
crabel — Analyse des configurations de prix Toby Crabel via MetaTrader 5.

Usage minimal :
    from crabel import analyze_symbol
    result = analyze_symbol("EURUSD")

Usage étendu (Stretch + ORB levels) :
    from crabel import analyze_symbol_full
    result = analyze_symbol_full("EURUSD")

La session MT5 doit être initialisée par l'application avant tout appel.
"""

from .exceptions import (
    InsufficientHistoryForStretch,
    MalformedBarData,
    MT5DataError,
    MT5SessionNotInitialized,
    SymbolNotFound,
)
from .signals import analyze_symbol, analyze_symbol_full

__all__ = [
    "analyze_symbol",
    "analyze_symbol_full",
    "MT5SessionNotInitialized",
    "SymbolNotFound",
    "MT5DataError",
    "InsufficientHistoryForStretch",
    "MalformedBarData",
]
