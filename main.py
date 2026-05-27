from flask import Flask, jsonify, request
from flask_cors import CORS
from flask_socketio import SocketIO, emit
import threading
import time
import mt5_client
import strategy
import backtest as backtest_engine
from crabel import analyze_symbol_full
from crabel.exceptions import (
    InsufficientHistoryForStretch,
    MalformedBarData,
    MT5DataError,
    MT5SessionNotInitialized,
    SymbolNotFound,
)

app = Flask(__name__)
CORS(app)
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading")

state = {
    "running": False,
    "phase": "STOPPED",
    "range_high": None,
    "range_low": None,
    "orders_placed": False,
    "buy_ticket": None,
    "sell_ticket": None,
    "error": None,
}
state_lock = threading.Lock()
bot_thread = None


def build_payload():
    with state_lock:
        s = dict(state)
    s["mt5_connected"] = mt5_client.is_connected()
    s["positions"] = mt5_client.get_open_positions("US100") if s["running"] else []
    s["pending_orders"] = mt5_client.get_pending_orders("US100") if s["running"] else []
    s["ny_time"] = strategy.get_ny_now().strftime("%H:%M:%S")
    s["current_price"] = strategy.get_current_price() if s["mt5_connected"] else None
    # Calcule le range à la demande quand le bot est arrêté et MT5 est connecté
    if s["mt5_connected"] and not s["running"] and s["range_high"] is None:
        rh, rl = strategy.compute_range()
        if rh is not None:
            s["range_high"] = rh
            s["range_low"] = rl
    return s


def broadcast():
    socketio.emit("status", build_payload())


def time_ticker():
    while True:
        price = strategy.get_current_price() if mt5_client.is_connected() else None
        socketio.emit("tick", {
            "ny_time": strategy.get_ny_now().strftime("%H:%M:%S"),
            "current_price": price,
        })
        time.sleep(1)


def bot_loop():
    try:
        mt5_client.connect()
    except RuntimeError as e:
        with state_lock:
            state["error"] = str(e)
            state["running"] = False
            state["phase"] = "ERROR"
        broadcast()
        return

    while True:
        with state_lock:
            if not state["running"]:
                break

        try:
            phase = strategy.get_bot_phase()
            with state_lock:
                state["phase"] = phase
                state["error"] = None

            if phase == "BUILDING_RANGE":
                high, low = strategy.compute_range()
                with state_lock:
                    state["range_high"] = high
                    state["range_low"] = low
                    state["orders_placed"] = False

            elif phase == "ORDERS_READY":
                with state_lock:
                    already_placed = state["orders_placed"]
                    high = state["range_high"]
                    low = state["range_low"]

                if not already_placed:
                    # Calcul rétroactif si le bot a démarré après la fin du range
                    if high is None or low is None:
                        high, low = strategy.compute_range()
                        with state_lock:
                            state["range_high"] = high
                            state["range_low"] = low

                    if high and low:
                        current_price = strategy.get_current_price()
                        if current_price is None or not (low < current_price < high):
                            # Prix déjà sorti du range → signal manqué
                            with state_lock:
                                state["phase"] = "SIGNAL_MISSED"
                                state["orders_placed"] = True
                        else:
                            buy_t, sell_t = strategy.place_orb_orders(high, low)
                            with state_lock:
                                state["buy_ticket"] = buy_t
                                state["sell_ticket"] = sell_t
                                state["orders_placed"] = True

        except RuntimeError as e:
            with state_lock:
                state["error"] = str(e)

        broadcast()
        time.sleep(10)

    mt5_client.disconnect()
    broadcast()


@socketio.on("connect")
def on_connect():
    emit("status", build_payload())


@app.route("/start", methods=["POST"])
def start():
    global bot_thread
    with state_lock:
        if state["running"]:
            return jsonify({"message": "Bot already running"}), 400
        state["running"] = True
        state["orders_placed"] = False
        state["range_high"] = None
        state["range_low"] = None
        state["buy_ticket"] = None
        state["sell_ticket"] = None
        state["error"] = None

    bot_thread = threading.Thread(target=bot_loop, daemon=True)
    bot_thread.start()
    broadcast()
    return jsonify({"message": "Bot started"})


@app.route("/stop", methods=["POST"])
def stop():
    with state_lock:
        if not state["running"]:
            return jsonify({"message": "Bot not running"}), 400
        state["running"] = False
        state["phase"] = "STOPPED"
    broadcast()
    return jsonify({"message": "Bot stopped"})


@app.route("/detail", methods=["GET"])
def detail():
    if not mt5_client.is_connected():
        return jsonify({"error": "MT5 non connecté"}), 503
    current_price = strategy.get_current_price()
    range_high, range_low = strategy.compute_range()
    return jsonify({
        "current_price": current_price,
        "range_high": range_high,
        "range_low": range_low,
    })


@app.route("/cancel_orders", methods=["POST"])
def cancel_orders():
    mt5_client.cancel_pending_orders("US100")
    with state_lock:
        state["orders_placed"] = False
        state["buy_ticket"] = None
        state["sell_ticket"] = None
    broadcast()
    return jsonify({"message": "Pending orders cancelled"})


@app.route("/signals/<symbol>", methods=["GET"])
def signals(symbol: str):
    """
    GET /signals/<symbol>[?target=YYYY-MM-DD|int&doji_threshold=0.10&inside_strict=true&history_days=60]

    Retourne les configurations de prix Crabel pour le symbole donné.
    La session MT5 doit être active (bot démarré ou terminal connecté).
    """
    target_raw = request.args.get("target", None)
    target = None
    if target_raw is not None:
        stripped = target_raw.lstrip("-")
        if stripped.isdigit() and target_raw.startswith("-"):
            target = int(target_raw)
        else:
            target = target_raw  # chaîne "YYYY-MM-DD" ou invalide (ValueError levée)

    try:
        result = analyze_symbol_full(
            symbol,
            target=target,
            doji_threshold=float(request.args.get("doji_threshold", 0.10)),
            inside_strict=request.args.get("inside_strict", "true").lower() != "false",
            history_days=int(request.args.get("history_days", 60)),
            stretch_window=int(request.args.get("stretch_window", 10)),
            atr_period=int(request.args.get("atr_period", 14)),
            sl_atr_k=float(request.args.get("sl_atr_k", 1.0)),
        )
        return jsonify(result)
    except MT5SessionNotInitialized as exc:
        return jsonify({"error": "mt5_session", "detail": str(exc)}), 503
    except SymbolNotFound as exc:
        return jsonify({"error": "symbol_not_found", "detail": str(exc)}), 404
    except MT5DataError as exc:
        return jsonify({"error": "mt5_data", "detail": str(exc)}), 502
    except InsufficientHistoryForStretch as exc:
        return jsonify({"error": "insufficient_history", "detail": str(exc)}), 422
    except MalformedBarData as exc:
        return jsonify({"error": "malformed_data", "detail": str(exc)}), 422
    except ValueError as exc:
        return jsonify({"error": "invalid_param", "detail": str(exc)}), 400


@app.route("/backtest/<symbol>", methods=["GET"])
def backtest(symbol: str):
    """
    GET /backtest/<symbol>?date_from=YYYY-MM-DD&date_to=YYYY-MM-DD&strategy=orb&chart_tf=H1

    Lance un backtest historique pour la stratégie demandée.
    La session MT5 doit être active.
    """
    if not mt5_client.is_connected():
        try:
            mt5_client.connect()
        except RuntimeError as exc:
            return jsonify({"error": "mt5_session", "detail": str(exc)}), 503

    strat = request.args.get("strategy", "orb").lower()
    date_from = request.args.get("date_from")
    date_to = request.args.get("date_to")
    chart_tf = request.args.get("chart_tf", "H1")

    if not date_from or not date_to:
        return jsonify({"error": "invalid_param", "detail": "date_from et date_to sont requis (YYYY-MM-DD)"}), 400

    try:
        if strat == "orb":
            result = backtest_engine.run_orb_backtest(symbol, date_from, date_to, chart_tf)
        else:
            return jsonify({"error": "unknown_strategy", "detail": f"Stratégie inconnue : {strat}"}), 400
        return jsonify(result)
    except ValueError as exc:
        return jsonify({"error": "invalid_param", "detail": str(exc)}), 400
    except RuntimeError as exc:
        return jsonify({"error": "mt5_data", "detail": str(exc)}), 502


if __name__ == "__main__":
    ticker = threading.Thread(target=time_ticker, daemon=True)
    ticker.start()
    print("ORB Bot backend running on http://localhost:5000")
    socketio.run(app, host="0.0.0.0", port=5000, debug=False)
