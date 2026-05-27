import MetaTrader5 as mt5
import pytz
from datetime import datetime, timedelta
import mt5_client

SYMBOL = "US100"
VOLUME = 0.01
NY_TZ = pytz.timezone("America/New_York")

# NY session open = 8:00 AM, range ends at 9:00 AM
RANGE_START_HOUR = 8
RANGE_START_MIN = 0
RANGE_DURATION_MINUTES = 60


def get_ny_now():
    return datetime.now(NY_TZ)


def get_range_window():
    """Returns (range_start, range_end) as timezone-aware datetimes for today."""
    ny_now = get_ny_now()
    start = ny_now.replace(
        hour=RANGE_START_HOUR,
        minute=RANGE_START_MIN,
        second=0,
        microsecond=0,
    )
    end = start + timedelta(minutes=RANGE_DURATION_MINUTES)
    return start, end


def compute_range():
    """Returns (high, low) of the 1H NY opening range using M1 bars."""
    _ensure_symbol()
    start, end = get_range_window()

    # Convert to UTC for MT5
    start_utc = start.astimezone(pytz.utc)
    end_utc = end.astimezone(pytz.utc)

    bars = mt5.copy_rates_range(SYMBOL, mt5.TIMEFRAME_M1, start_utc, end_utc)
    if bars is None or len(bars) == 0:
        return None, None

    high = max(b["high"] for b in bars)
    low = min(b["low"] for b in bars)
    return round(high, 2), round(low, 2)


def place_orb_orders(high, low):
    """Places Buy Stop at high and Sell Stop at low."""
    symbol_info = mt5_client.get_symbol_info(SYMBOL)
    point = symbol_info.point

    buy_ticket = mt5_client.place_order(
        symbol=SYMBOL,
        order_type=mt5.ORDER_TYPE_BUY_STOP,
        price=high,
        sl=low,
        volume=VOLUME,
        comment="ORB_BUY",
    )

    sell_ticket = mt5_client.place_order(
        symbol=SYMBOL,
        order_type=mt5.ORDER_TYPE_SELL_STOP,
        price=low,
        sl=high,
        volume=VOLUME,
        comment="ORB_SELL",
    )

    return buy_ticket, sell_ticket


def _ensure_symbol():
    mt5.symbol_select(SYMBOL, True)


def get_current_price():
    _ensure_symbol()
    tick = mt5.symbol_info_tick(SYMBOL)
    if tick is None:
        return None
    return tick.bid


def get_bot_phase():
    """Returns the current phase of the strategy."""
    _, end = get_range_window()
    ny_now = get_ny_now()
    start, _ = get_range_window()

    if ny_now < start:
        return "WAITING_NY_OPEN"
    elif ny_now < end:
        return "BUILDING_RANGE"
    else:
        return "ORDERS_READY"
