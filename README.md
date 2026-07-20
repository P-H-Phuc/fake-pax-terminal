# fake-pax-terminal

A development & testing tool that simulates a PAX terminal's **POS Link HTTP server** so you can test the `pos_payment_pax_terminal` Odoo module without real hardware.

Two servers run side-by-side:
- **PAX terminal** (port `10009`) — speaks the binary POS Link protocol that Odoo expects
- **Web dashboard** (port `5000`) — live view of every request/response and transaction

---

## Quick start

```bash
pip install -r requirements.txt
python main.py
```

Then open **http://localhost:5000** to see the dashboard.

---

## Usage

```
python main.py [OPTIONS]

Options:
  --port PORT        PAX terminal port  (default: 10009)
  --ui-port PORT     Web dashboard port (default: 5000)
  --mode {approve,manual}
                     approve = amount-based rules (default)
                     manual  = prompt y/n in terminal for each payment
  --delay SECONDS    Extra delay added on top of amount rules (default: 0)
```

### Examples

```bash
# Default — amount rules, PAX on 10009, dashboard on 5000
python main.py

# Custom ports
python main.py --port 19009 --ui-port 8080

# Manual approval (type y/n per request)
python main.py --mode manual

# Simulate a slow network (+2s on all responses)
python main.py --delay 2
```

---

## Configure Odoo

In the Odoo POS backend, set the `pax.terminal` configuration to:

| Field | Value |
|-------|-------|
| IP    | `127.0.0.1` |
| Port  | `10009` (or whatever `--port` you used) |

---

## Amount rules (approve mode)

Amounts are in the currency's **smallest unit** (cents, VND, etc.).

| Amount            | Result                        |
|-------------------|-------------------------------|
| < 5 000           | **DECLINED** (code `100006`: AMOUNT TOO LOW) |
| 5 000 – 50 000    | **APPROVED** instantly        |
| > 50 000          | **APPROVED** after a 5-second delay (slow terminal simulation) |

---

## Web dashboard

| URL | Description |
|-----|-------------|
| `http://localhost:5000/` | Live dashboard — stats + last 20 transactions (auto-refresh every 4s) |
| `http://localhost:5000/transactions` | Full transaction history with status filter |
| `http://localhost:5000/log` | Raw server log (last 300 lines, auto-refresh every 10s) |
| `http://localhost:5000/api/transactions` | JSON API — all transactions |
| `http://localhost:5000/api/stats` | JSON API — summary stats |

---

## Log file

Requests and responses are always logged to `log/server.log` (plain text, no ANSI colours).

```
2024-01-20 10:30:01 INFO  REQUEST  cmd=T00  type=Sale  amount=12345  order=POS/001/0001
2024-01-20 10:30:01 INFO  RESPONSE APPROVED
```

---

## Supported commands

| Command | Description |
|---------|-------------|
| `A08`   | Initialize — always returns `FAKE PAX A920` |
| `T00`   | Transaction — Sale, Return, Auth, etc. (amount rules apply) |
| others  | Returns `000000 OK` (pass-through) |
