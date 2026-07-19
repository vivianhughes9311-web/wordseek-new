import os

from flask import Flask, jsonify, render_template, request

from solver import solve_board

app = Flask(__name__)


@app.get("/")
def home():
    return render_template("index.html")


@app.get("/health")
def health():
    return jsonify({"status": "ok"})


@app.post("/solve")
def solve():
    data = request.get_json(silent=True) or {}
    board = str(data.get("board", "")).strip()

    try:
        mode = int(data.get("mode", 5))
    except (TypeError, ValueError):
        return jsonify({"error": "Mode must be 4 or 5."}), 400

    if mode not in (4, 5):
        return jsonify({"error": "Mode must be 4 or 5."}), 400
    if not board:
        return jsonify({"error": "Paste a WordSeek board first."}), 400
    if len(board) > 25_000:
        return jsonify({"error": "Board input is too large."}), 413

    try:
        result = solve_board(board, mode)
    except Exception:
        app.logger.exception("Solver failed")
        return jsonify({"error": "Solver failed. Check the board format."}), 500

    return jsonify({
        "text": result["message"],
        "answer": result["answer"],
        "guesses": result["guesses"],
        "count": result["count"],
        "mode": mode,
    })


@app.after_request
def no_cache(response):
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "51332"))
    app.run(host="0.0.0.0", port=port)
