#!/usr/bin/env python3
"""
Fake PAX Terminal Server — development & testing tool.

Simulates a PAX terminal's POS Link HTTP server so you can test the
pos_payment_pax_terminal Odoo module without real hardware.

Two servers run simultaneously:
  - PAX terminal server (default: port 10009) — handles POS Link binary protocol
  - Web dashboard      (default: port 5000)  — live view of requests, responses, transactions

Amount-based rules (smart mode, T00 transactions):
    amount < 5000 cents   →  DECLINED  (100006: AMOUNT TOO LOW)
    amount > 50000 cents  →  wait 5s, then APPROVED
    5000 <= amount <= 50000 →  APPROVED immediately

Usage:
    python main.py
    python main.py --port 19009 --ui-port 5001
    python main.py --mode manual
    python main.py --delay 2
"""

import argparse
import base64
import collections
import datetime
import http.server
import logging
import os
import re
import random
import sys
import threading
import time
import urllib.parse

from flask import Flask, render_template, jsonify

# ── Paths ─────────────────────────────────────────────────────────────────────

BASE_DIR      = os.path.dirname(os.path.abspath(__file__))
LOG_DIR       = os.path.join(BASE_DIR, "log")
LOG_FILE      = os.path.join(LOG_DIR, "server.log")
TEMPLATES_DIR = os.path.join(BASE_DIR, "templates")

os.makedirs(LOG_DIR, exist_ok=True)

# ── Dedicated logger (file only, no interference with Flask/werkzeug) ─────────

_ANSI_RE = re.compile(r"\033\[[0-9;]*m")

def _strip_ansi(s: str) -> str:
    return _ANSI_RE.sub("", s)

_log_handler = logging.FileHandler(LOG_FILE, encoding="utf-8")
_log_handler.setFormatter(
    logging.Formatter("%(asctime)s %(levelname)-5s %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
)
_logger = logging.getLogger("fake_pax")
_logger.setLevel(logging.INFO)
_logger.addHandler(_log_handler)

# suppress Flask/werkzeug access noise in the console
logging.getLogger("werkzeug").setLevel(logging.ERROR)

# ── POS Link constants ────────────────────────────────────────────────────────

STX = 0x02
ETX = 0x03
FS  = 0x1C

GREEN  = "\033[92m"
RED    = "\033[91m"
YELLOW = "\033[93m"
CYAN   = "\033[96m"
BLUE   = "\033[94m"
GREY   = "\033[90m"
RESET  = "\033[0m"
BOLD   = "\033[1m"

AMOUNT_ERROR_BELOW = 5000
AMOUNT_SLOW_ABOVE  = 50000
SLOW_DELAY_SECONDS = 5

TYPE_LABELS = {
    "01": "Sale", "02": "Return", "04": "Void",
    "05": "Auth", "06": "Post-Auth",
}

# ── Thread-safe transaction store ─────────────────────────────────────────────

class TransactionStore:
    """Keeps the last `maxlen` transactions in memory, newest first."""

    def __init__(self, maxlen: int = 500):
        self._lock  = threading.Lock()
        self._items: collections.deque[dict] = collections.deque(maxlen=maxlen)
        self._seq   = 0

    def add(self, entry: dict) -> dict:
        with self._lock:
            self._seq += 1
            entry["seq"] = self._seq
            self._items.appendleft(entry)
            return entry

    def all(self) -> list:
        with self._lock:
            return list(self._items)

    def stats(self) -> dict:
        items = self.all()
        approved = sum(1 for x in items if x.get("status") == "APPROVED")
        declined = sum(1 for x in items if x.get("status") == "DECLINED")
        cancelled = sum(1 for x in items if x.get("status") == "CANCELLED")
        return {
            "total": len(items),
            "approved": approved,
            "declined": declined,
            "cancelled": cancelled,
            "other": len(items) - approved - declined - cancelled,
        }


store = TransactionStore()

# runtime config (populated by main())
_SERVER_CONFIG: dict = {}

# ── Protocol helpers ──────────────────────────────────────────────────────────

def _lrc(data: bytes) -> int:
    result = 0
    for b in data:
        result ^= b
    return result


def _build_frame(*fields: str) -> bytes:
    parts = [f.encode("ascii") for f in fields]
    body  = bytes([FS]).join(parts)
    inner = f"{len(body) + 1:04d}".encode() + body + bytes([ETX])
    return bytes([STX]) + inner + bytes([_lrc(inner)])


def _parse_frame(raw: bytes) -> dict:
    if not raw or raw[0] != STX:
        return {}
    try:
        etx_pos = raw.index(ETX, 1)
    except ValueError:
        return {}
    payload = raw[1:etx_pos][4:]
    fields  = payload.split(bytes([FS]))

    def f(i):
        return fields[i].decode("ascii", errors="replace") if len(fields) > i else ""

    return {
        "command":          f(0),
        "version":          f(1),
        "transaction_type": f(2),
        "amount":           f(3),
        "order_id":         f(14),
        "currency":         f(17) if len(fields) > 17 else "",
        "_raw_fields":      [x.decode("ascii", errors="replace") for x in fields],
    }


# ── Response builders ─────────────────────────────────────────────────────────

def _make_auth_code() -> str:
    return "".join(random.choices("ABCDEFGHJKLMNPQRSTUVWXYZ0123456789", k=6))

def _make_txn_id() -> str:
    return f"TXN{random.randint(100000, 999999)}"

def _make_ref() -> str:
    return f"REF{random.randint(10000, 99999)}"


def response_approve(parsed: dict) -> tuple[bytes, dict]:
    amount = parsed.get("amount", "0")
    auth   = _make_auth_code()
    txn_id = _make_txn_id()
    ref    = parsed.get("order_id") or _make_ref()
    cmd    = parsed.get("command", "T00")
    frame  = _build_frame(
        cmd, "1.28", "000000", "OK",
        "00", "APPROVAL",
        auth, ref, txn_id,
        "", "", "", "",
        amount, "",
        "JOHN DOE", "1225", "411111", "1111",
    )
    return frame, {"auth_code": auth, "txn_id": txn_id, "ref": ref}


def response_decline(parsed: dict, code: str = "000100", msg: str = "DECLINED") -> tuple[bytes, dict]:
    cmd   = parsed.get("command", "T00")
    frame = _build_frame(cmd, "1.28", code, msg, "05", msg, "", "", "")
    return frame, {"error_code": code, "error_msg": msg}


def response_initialize() -> tuple[bytes, dict]:
    frame = _build_frame(
        "A08", "1.28", "000000", "OK",
        "FAKE PAX A920", "FW:1.28.0", "SN:FAKE000001",
    )
    return frame, {"terminal": "FAKE PAX A920", "sn": "FAKE000001"}


# ── Amount-based decision logic ───────────────────────────────────────────────

def _amount_int(parsed: dict) -> int:
    try:
        return int(parsed.get("amount", "0") or "0")
    except ValueError:
        return 0


def _resolve_t00(parsed: dict, mode: str, extra_delay: float) -> tuple[bytes, dict, str, float]:
    """Returns (frame, extra_info, console_status_label, sleep_seconds)."""
    amount = _amount_int(parsed)

    if mode == "manual":
        answer = ""
        while answer not in ("y", "n"):
            try:
                answer = input(f"  {BOLD}Approve transaction? [y/n]: {RESET}").strip().lower()
            except EOFError:
                answer = "n"
        if answer == "y":
            frame, info = response_approve(parsed)
            return frame, info, f"{GREEN}APPROVED (manual){RESET}", extra_delay
        frame, info = response_decline(parsed)
        return frame, info, f"{RED}DECLINED (manual){RESET}", extra_delay

    if amount < AMOUNT_ERROR_BELOW:
        label = (
            f"{RED}DECLINED{RESET} "
            f"{GREY}(amount {amount} < {AMOUNT_ERROR_BELOW} cents){RESET}"
        )
        frame, info = response_decline(parsed, code="100006", msg="AMOUNT TOO LOW")
        return frame, info, label, extra_delay

    if amount > AMOUNT_SLOW_ABOVE:
        label = (
            f"{BLUE}SLOW → APPROVED{RESET} "
            f"{GREY}(amount {amount} > {AMOUNT_SLOW_ABOVE} cents, "
            f"waiting {SLOW_DELAY_SECONDS}s){RESET}"
        )
        frame, info = response_approve(parsed)
        return frame, info, label, extra_delay + SLOW_DELAY_SECONDS

    frame, info = response_approve(parsed)
    return frame, info, f"{GREEN}APPROVED{RESET}", extra_delay


# ── HTTP handler (PAX terminal port) ─────────────────────────────────────────

class PaxHandler(http.server.BaseHTTPRequestHandler):

    MODE  = "approve"
    DELAY = 0.0

    def log_message(self, format: str, *args) -> None:
        _ = format, args

    def do_GET(self):
        ts_dt  = datetime.datetime.now()
        ts     = ts_dt.strftime("%H:%M:%S")
        ts_iso = ts_dt.isoformat(timespec="seconds")

        raw_query = urllib.parse.urlparse(self.path).query
        try:
            frame_bytes = base64.b64decode(raw_query + "==")
            parsed      = _parse_frame(frame_bytes)
        except Exception as exc:
            print(f"{RED}[{ts}] Bad frame: {exc}{RESET}")
            _logger.error("Bad frame: %s", exc)
            self.send_error(400, "Bad POS Link frame")
            return

        command    = parsed.get("command", "?")
        amount     = parsed.get("amount", "")
        order      = parsed.get("order_id", "")
        t_type     = parsed.get("transaction_type", "")
        type_label = TYPE_LABELS.get(t_type, t_type or "—")

        print(
            f"\n{BOLD}{CYAN}[{ts}] ◄ REQUEST{RESET}  "
            f"cmd={BOLD}{command}{RESET}  type={type_label}  "
            f"amount={YELLOW}{amount}{RESET} cents  "
            f"order={order}"
        )
        _logger.info("REQUEST  cmd=%s  type=%s  amount=%s  order=%s",
                     command, type_label, amount, order)

        extra_info   = {}
        status_label = ""

        if command == "A08":
            response_frame, extra_info = response_initialize()
            status_label = f"{GREEN}INIT OK{RESET}"
            sleep_s      = self.DELAY

        elif command == "T00":
            response_frame, extra_info, status_label, sleep_s = _resolve_t00(
                parsed, self.MODE, self.DELAY
            )

        else:
            response_frame = _build_frame(command, "1.28", "000000", "OK")
            status_label   = f"{GREY}UNKNOWN CMD → OK{RESET}"
            sleep_s        = self.DELAY

        status_plain = _strip_ansi(status_label)
        sp = status_plain.upper()
        if "APPROVED" in sp:
            clean_status = "APPROVED"
        elif "INIT OK" in sp:
            clean_status = "OK"
        elif "DECLINED" in sp:
            clean_status = "DECLINED"
        elif "CANCEL" in sp:
            clean_status = "CANCELLED"
        else:
            clean_status = "OK"

        if sleep_s > 0:
            print(f"{GREY}[{ts}]   waiting {sleep_s:.1f}s …{RESET}", flush=True)
            time.sleep(sleep_s)

        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(len(response_frame)))
        self.end_headers()
        self.wfile.write(response_frame)

        ts2 = datetime.datetime.now().strftime("%H:%M:%S")
        print(f"{BOLD}{CYAN}[{ts2}] ► RESPONSE{RESET} {status_label}")
        _logger.info("RESPONSE %s", status_plain)

        store.add({
            "ts":       ts_iso,
            "command":  command,
            "type":     type_label,
            "amount":   amount,
            "order_id": order,
            "status":   clean_status,
            "delay_s":  round(sleep_s, 1),
            **extra_info,
        })


# ── Flask web dashboard ───────────────────────────────────────────────────────

flask_app = Flask(__name__, template_folder=TEMPLATES_DIR)
flask_app.config["TEMPLATES_AUTO_RELOAD"] = True


@flask_app.route("/")
def ui_index():
    return render_template(
        "index.html",
        transactions=store.all()[:20],
        stats=store.stats(),
        config=_SERVER_CONFIG,
    )


@flask_app.route("/transactions")
def ui_transactions():
    return render_template(
        "transactions.html",
        transactions=store.all(),
        stats=store.stats(),
    )


@flask_app.route("/log")
def ui_log():
    lines: list[str] = []
    try:
        with open(LOG_FILE, encoding="utf-8") as fh:
            lines = fh.readlines()[-300:]
    except FileNotFoundError:
        pass
    return render_template("log.html", lines=lines, log_file=LOG_FILE)


@flask_app.route("/api/transactions")
def api_transactions():
    return jsonify(store.all())


@flask_app.route("/api/stats")
def api_stats():
    return jsonify(store.stats())


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Fake PAX terminal server for Odoo POS development",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Modes:
  approve (default)  use amount-based rules (see below)
  manual             prompt y/n in the terminal for each payment

Amount rules (approve mode):
  < 5000 cents    →  DECLINED  (100006: AMOUNT TOO LOW)
  > 50000 cents   →  5s delay, then APPROVED
  5000–50000      →  instant APPROVED
        """,
    )
    parser.add_argument("--port",    type=int, default=10009,
                        help="PAX terminal port (default: 10009)")
    parser.add_argument("--ui-port", type=int, default=5000,
                        help="Web dashboard port (default: 5000)")
    parser.add_argument("--mode", choices=["approve", "manual"], default="approve")
    parser.add_argument("--delay", type=float, default=0.0,
                        help="Extra delay in seconds on top of amount rules (default: 0)")
    args = parser.parse_args()

    PaxHandler.MODE  = args.mode
    PaxHandler.DELAY = args.delay

    _SERVER_CONFIG.update({
        "pax_port": args.port,
        "ui_port":  args.ui_port,
        "mode":     args.mode,
        "delay":    args.delay,
        "log_file": LOG_FILE,
        "started":  datetime.datetime.now().isoformat(timespec="seconds"),
    })

    # ── Start PAX server in a background thread ───────────────────────────────
    pax_server = http.server.HTTPServer(("0.0.0.0", args.port), PaxHandler)
    pax_thread = threading.Thread(target=pax_server.serve_forever, daemon=True)
    pax_thread.name = "pax-server"
    pax_thread.start()

    if args.mode == "approve":
        rule_summary = (
            f"  {GREY}Amount rules:{RESET}\n"
            f"    < {AMOUNT_ERROR_BELOW} cents  →  {RED}DECLINED{RESET}\n"
            f"    > {AMOUNT_SLOW_ABOVE} cents  →  {BLUE}wait {SLOW_DELAY_SECONDS}s → APPROVED{RESET}\n"
            f"    {AMOUNT_ERROR_BELOW}–{AMOUNT_SLOW_ABOVE} cents  →  {GREEN}instant APPROVED{RESET}\n"
        )
    else:
        rule_summary = f"  {YELLOW}Manual mode — will prompt y/n for each payment{RESET}\n"

    print(f"""
{BOLD}{'─' * 62}{RESET}
{BOLD}  Fake PAX Terminal Server{RESET}
{'─' * 62}
  PAX terminal  : {BOLD}http://127.0.0.1:{args.port}{RESET}
  Web dashboard : {BOLD}http://127.0.0.1:{args.ui_port}{RESET}
  Mode          : {BOLD}{YELLOW}{args.mode.upper()}{RESET}
  Extra delay   : {args.delay}s
  Log file      : {LOG_FILE}

{rule_summary}
  {GREY}Configure pax.terminal in Odoo:{RESET}
    IP   → 127.0.0.1
    Port → {args.port}

  {GREY}Press Ctrl+C to stop{RESET}
{'─' * 62}
""")
    _logger.info(
        "Started — pax_port=%d  ui_port=%d  mode=%s  delay=%.1f",
        args.port, args.ui_port, args.mode, args.delay,
    )

    try:
        flask_app.run(host="0.0.0.0", port=args.ui_port, debug=False, use_reloader=False)
    except KeyboardInterrupt:
        pass
    finally:
        pax_server.shutdown()
        _logger.info("Stopped.")
        print(f"\n{YELLOW}Stopped.{RESET}")
        sys.exit(0)


if __name__ == "__main__":
    main()
