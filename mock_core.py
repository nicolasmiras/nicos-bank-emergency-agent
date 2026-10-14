"""Nico's Bank mock core banking API.

Simulates the internal services the ElevenLabs agent calls via webhooks:
customer lookup, account lock, card block and incident registration.
Also serves a live back-office dashboard at /dashboard.

Run: python3 mock_core.py  (listens on :8787, no dependencies)
"""
import json
import random
import threading
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PORT = 8787
ELEVENLABS_API = "https://api.elevenlabs.io"
CHILE = timezone(timedelta(hours=-3))

# Seed customers, keyed by normalized RUT (no dots, uppercase check digit).
CUSTOMERS = {
    "19577077-8": {
        "name": "Nicolás Miras",
        "account": {"number": "****4821", "status": "active"},
        "cards": [
            {"id": "card_debit", "type": "Debit", "last4": "4821", "status": "active"},
            {"id": "card_credit", "type": "Credit", "last4": "9034", "status": "active"},
            {"id": "card_virtual", "type": "Virtual", "last4": "1177", "status": "active"},
        ],
    },
    "21102774-6": {
        "name": "Catalina Rojas",
        "account": {"number": "****7710", "status": "active"},
        "cards": [
            {"id": "card_debit", "type": "Debit", "last4": "7710", "status": "active"},
            {"id": "card_credit", "type": "Credit", "last4": "3358", "status": "active"},
        ],
    },
    "16466696-4": {
        "name": "Martín Fuentes",
        "account": {"number": "****2093", "status": "active"},
        "cards": [
            {"id": "card_debit", "type": "Debit", "last4": "2093", "status": "active"},
        ],
    },
    "17654001-K": {
        "name": "Javiera Soto",
        "account": {"number": "****6645", "status": "active"},
        "cards": [
            {"id": "card_debit", "type": "Debit", "last4": "6645", "status": "active"},
            {"id": "card_virtual", "type": "Virtual", "last4": "0412", "status": "active"},
        ],
    },
}

EVENTS = []  # newest last
CSAT = []
LOCK = threading.Lock()


def now():
    return datetime.now(CHILE)


def normalize_rut(raw):
    clean = "".join(c for c in str(raw or "").upper() if c.isdigit() or c == "K")
    return f"{clean[:-1]}-{clean[-1]}" if len(clean) > 1 else clean


def log_event(service, request, response, status):
    with LOCK:
        EVENTS.append({
            "id": len(EVENTS) + 1,
            "at": now().strftime("%H:%M:%S"),
            "service": service,
            "status": status,
            "request": request,
            "response": response,
        })


def lookup_customer(body):
    rut = normalize_rut(body.get("rut"))
    c = CUSTOMERS.get(rut)
    if not c:
        return 404, {"found": False, "message": "No customer with that RUT."}
    # Only what the agent needs; no personal data is read back to the caller.
    return 200, {"found": True, "customer_id": rut, "first_name": c["name"].split()[0],
                 "active_cards": sum(card["status"] == "active" for card in c["cards"]),
                 "physical_cards": [card["type"].lower() for card in c["cards"] if card["type"] != "Virtual"]}


def lock_account(body):
    rut = normalize_rut(body.get("rut"))
    c = CUSTOMERS.get(rut)
    if not c:
        return 404, {"success": False, "message": "Customer not found."}
    c["account"]["status"] = "locked"
    unlock_at = now() + timedelta(hours=1)
    c["account"]["unlock_eligible_at"] = unlock_at.isoformat(timespec="minutes")
    return 200, {
        "success": True,
        "account": c["account"]["number"],
        "status": "locked",
        "lock_reference": f"LCK-{random.randint(100000, 999999)}",
        "unlock_eligible_at": unlock_at.strftime("%H:%M"),
        "unlock_requirement": "identity_validation",
    }


def block_cards(body):
    rut = normalize_rut(body.get("rut"))
    c = CUSTOMERS.get(rut)
    if not c:
        return 404, {"success": False, "message": "Customer not found."}
    scope = body.get("scope") or "all"
    if scope not in ("all", "physical"):
        return 400, {"success": False, "message": "scope must be 'all' or 'physical'."}
    wanted = {t.lower() for t in body.get("card_types") or []}
    blocked = []
    for card in c["cards"]:
        if scope == "physical":
            if card["type"] == "Virtual" or (wanted and card["type"].lower() not in wanted):
                continue
        card["status"] = "blocked"
        blocked.append(f'{card["type"]} ending {card["last4"]}')
    if not blocked:
        return 404, {"success": False, "message": "No matching cards to block."}
    still_active = [f'{k["type"]} ending {k["last4"]}' for k in c["cards"] if k["status"] == "active"]
    return 200, {"success": True, "scope": scope, "cards_blocked": len(blocked), "cards": blocked,
                 "cards_still_active": still_active}


def create_incident(body):
    rut = normalize_rut(body.get("rut"))
    ticket = f"NB-{now():%y%m%d}-{random.randint(1000, 9999)}"
    if rut in CUSTOMERS:
        CUSTOMERS[rut].setdefault("incidents", []).append({**body, "ticket_id": ticket})
    return 201, {"success": True, "ticket_id": ticket,
                 "spoken_ticket_id": " ".join(ticket.split("-")[-1]),
                 "assigned_team": "Fraud & Security"}


def submit_csat(body):
    try:
        score = int(body.get("score"))
    except (TypeError, ValueError):
        return 400, {"success": False, "message": "score must be an integer from 1 to 5."}
    if not 1 <= score <= 5:
        return 400, {"success": False, "message": "score must be an integer from 1 to 5."}
    comment = body.get("comment") or ""
    with LOCK:
        CSAT.append({"score": score, "comment": comment, "at": now().strftime("%H:%M"),
                     "rut": normalize_rut(body.get("rut")) if body.get("rut") else None})
    # Mirror the score into ElevenLabs' native CSAT for this conversation.
    synced = forward_rating_to_elevenlabs(body.get("conversation_id"), score, comment)
    return 201, {"success": True, "message": "Thanks, feedback recorded.", "elevenlabs_csat_synced": synced}


def forward_rating_to_elevenlabs(conversation_id, score, comment):
    if not conversation_id:
        return False
    req = urllib.request.Request(
        f"{ELEVENLABS_API}/v1/convai/conversations/{conversation_id}/feedback",
        data=json.dumps({"rating": score, "comment": comment}).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return 200 <= r.status < 300
    except (urllib.error.URLError, TimeoutError) as e:
        print(f"ElevenLabs CSAT sync failed: {e}")
        return False


ROUTES = {
    "/api/customers/lookup": ("Customer lookup", lookup_customer),
    "/api/webhooks/account-lock": ("Account lock service", lock_account),
    "/api/webhooks/cards-block": ("Card block service", block_cards),
    "/api/webhooks/incidents": ("Incident registry", create_incident),
    "/api/webhooks/csat": ("CSAT survey", submit_csat),
}


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, payload, ctype="application/json"):
        data = payload if isinstance(payload, bytes) else json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.end_headers()

    def do_GET(self):
        if self.path in ("/", "/dashboard"):
            html = (Path(__file__).parent / "dashboard.html").read_bytes()
            return self._send(200, html, "text/html; charset=utf-8")
        if self.path == "/api/state":
            with LOCK:
                return self._send(200, {"customers": CUSTOMERS, "events": EVENTS[-50:], "csat": CSAT[-20:]})
        self._send(404, {"error": "not found"})

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            return self._send(400, {"error": "invalid JSON"})
        if self.path == "/api/reset":
            reset()
            return self._send(200, {"ok": True})
        route = ROUTES.get(self.path)
        if not route:
            return self._send(404, {"error": "not found"})
        service, fn = route
        code, response = fn(body)
        log_event(service, body, response, code)
        self._send(code, response)

    def log_message(self, fmt, *args):
        print(f"[{now():%H:%M:%S}] {self.command} {self.path} -> {args[1] if len(args) > 1 else ''}")


SEED = json.dumps(CUSTOMERS)


def reset():
    with LOCK:
        CUSTOMERS.clear()
        CUSTOMERS.update(json.loads(SEED))
        EVENTS.clear()
        CSAT.clear()


if __name__ == "__main__":
    print(f"Nico's Bank mock core on http://localhost:{PORT}  (dashboard: /dashboard)")
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
