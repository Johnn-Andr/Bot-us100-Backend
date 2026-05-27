"""Custom exceptions for the Crabel signal module."""


class MT5SessionNotInitialized(RuntimeError):
    """
    Raised when no active MT5 session is found.

    The application is responsible for calling mt5.initialize() and (if required)
    mt5.login() before using this module.  This module will never do so itself.
    """


class SymbolNotFound(LookupError):
    """Raised when the requested symbol is absent from MT5 or cannot be made visible."""


class MT5DataError(RuntimeError):
    """Raised when an MT5 data function returns None or an empty array unexpectedly."""


class InsufficientHistoryForStretch(ValueError):
    """
    Raised when the D1 bar history before J is too short to compute the Crabel Stretch.

    Crabel defines the Stretch as an average over a fixed window (default: 10 days).
    A partial average would not be the Crabel Stretch — hence the hard error.
    Increase `history_days` in the call to analyze_symbol_full() to fix this.
    """


class MalformedBarData(ValueError):
    """
    Raised when a bar contains structurally invalid OHLC data for Stretch computation.

    Specifically: open outside [low, high].  On clean broker data this should never
    happen; it indicates a feed or normalization problem upstream.
    """
