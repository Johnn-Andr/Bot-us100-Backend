import MetaTrader5 as mt5

def connect():
    if not mt5.initialize():
        raise RuntimeError(f"MT5 initialize failed: {mt5.last_error()}")
    info = mt5.account_info()
    if info is None:
        raise RuntimeError("No MT5 account connected. Open MT5 and log in first.")
    return info

def disconnect():
    mt5.shutdown()

def get_symbol_info(symbol):
    info = mt5.symbol_info(symbol)
    if info is None:
        raise RuntimeError(f"Symbol {symbol} not found. Check Market Watch in MT5.")
    return info

def place_order(symbol, order_type, price, sl, volume, comment="ORB"):
    request = {
        "action": mt5.TRADE_ACTION_PENDING,
        "symbol": symbol,
        "volume": volume,
        "type": order_type,
        "price": price,
        "sl": sl,
        "tp": 0.0,
        "comment": comment,
        "type_time": mt5.ORDER_TIME_DAY,
        "type_filling": mt5.ORDER_FILLING_RETURN,
    }
    result = mt5.order_send(request)
    if result.retcode != mt5.TRADE_RETCODE_DONE:
        raise RuntimeError(f"Order failed: {result.comment} (code {result.retcode})")
    return result.order

def cancel_pending_orders(symbol):
    orders = mt5.orders_get(symbol=symbol)
    if not orders:
        return
    for order in orders:
        mt5.order_send({
            "action": mt5.TRADE_ACTION_REMOVE,
            "order": order.ticket,
        })

def get_open_positions(symbol):
    positions = mt5.positions_get(symbol=symbol)
    if not positions:
        return []
    return [
        {
            "ticket": p.ticket,
            "type": "BUY" if p.type == 0 else "SELL",
            "volume": p.volume,
            "open_price": p.price_open,
            "sl": p.sl,
            "profit": p.profit,
        }
        for p in positions
    ]

def get_pending_orders(symbol):
    orders = mt5.orders_get(symbol=symbol)
    if not orders:
        return []
    type_map = {
        mt5.ORDER_TYPE_BUY_STOP: "BUY_STOP",
        mt5.ORDER_TYPE_SELL_STOP: "SELL_STOP",
    }
    return [
        {
            "ticket": o.ticket,
            "type": type_map.get(o.type, str(o.type)),
            "price": o.price_open,
            "sl": o.sl,
            "volume": o.volume_current,
        }
        for o in orders
    ]
