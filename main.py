from flask import Flask, jsonify
from flask_cors import CORS
import threading
import time
import mt5_client
import strategy

app = Flask(__name__)
CORS(app)

# Shared bot state
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


def bot_loop():
    try:
        mt5_client.connect()
    except RuntimeError as e:
        with state_lock:
            state["error"] = str(e)
            state["running"] = False
            state["phase"] = "ERROR"
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

                if not already_placed and high and low:
                    buy_t, sell_t = strategy.place_orb_orders(high, low)
                    with state_lock:
                        state["buy_ticket"] = buy_t
                        state["sell_ticket"] = sell_t
                        state["orders_placed"] = True

        except RuntimeError as e:
            with state_lock:
                state["error"] = str(e)

        time.sleep(10)

    mt5_client.disconnect()


@app.route("/status")
def status():
    with state_lock:
        s = dict(state)
    s["mt5_connected"] = mt5_client.is_connected()
    s["positions"] = mt5_client.get_open_positions("US100") if s["running"] else []
    s["pending_orders"] = mt5_client.get_pending_orders("US100") if s["running"] else []
    s["ny_time"] = strategy.get_ny_now().strftime("%H:%M:%S")
    return jsonify(s)


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
    return jsonify({"message": "Bot started"})


@app.route("/stop", methods=["POST"])
def stop():
    with state_lock:
        if not state["running"]:
            return jsonify({"message": "Bot not running"}), 400
        state["running"] = False
        state["phase"] = "STOPPED"
    return jsonify({"message": "Bot stopped"})


@app.route("/cancel_orders", methods=["POST"])
def cancel_orders():
    mt5_client.cancel_pending_orders("US100")
    with state_lock:
        state["orders_placed"] = False
        state["buy_ticket"] = None
        state["sell_ticket"] = None
    return jsonify({"message": "Pending orders cancelled"})


if __name__ == "__main__":
    print("ORB Bot backend running on http://localhost:5000")
    app.run(host="0.0.0.0", port=5000, debug=False)
