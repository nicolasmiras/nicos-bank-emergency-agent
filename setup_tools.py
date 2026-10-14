"""Create or update the agent's webhook tools so they point at BASE_URL.

Usage: python3 setup_tools.py https://xxxx.trycloudflare.com
Needs ELEVENLABS_API_KEY in the environment.
"""
import json
import os
import sys
import urllib.request

AGENT_ID = "agent_0101m4y1ss4gekrb34sbfcnf2cm2"
API = "https://api.elevenlabs.io/v1/convai"
KEY = os.environ["ELEVENLABS_API_KEY"]

RUT = {"type": "string", "description": "Customer RUT, normalized as returned by validate_rut, e.g. '12.345.678-5'."}
CONV_ID = {"type": "string", "dynamic_variable": "system__conversation_id"}


def tool(name, path, description, properties, required):
    return {
        "type": "webhook",
        "name": name,
        "description": description,
        "response_timeout_secs": 15,
        "api_schema": {
            "url": f"{BASE}{path}",
            "method": "POST",
            "request_body_schema": {
                "type": "object",
                "description": f"Payload for {name}",
                "properties": {**properties, "conversation_id": CONV_ID},
                "required": required,
            },
        },
    }


def tools():
    return [
        tool("lookup_customer", "/api/customers/lookup",
             "Checks that the RUT belongs to a Nico's Bank customer. Call after validate_rut returns valid.",
             {"rut": RUT}, ["rut"]),
        tool("lock_account", "/api/webhooks/account-lock",
             "Locks the customer's whole Nico's Bank account. Call only after collecting all required details and getting explicit consent.",
             {"rut": RUT,
              "incident_type": {"type": "string", "enum": ["theft", "robbery", "loss", "fraud"], "description": "Incident type confirmed by the caller."}},
             ["rut", "incident_type"]),
        tool("block_cards", "/api/webhooks/cards-block",
             "Blocks the customer's cards. Use scope 'all' (every card, including virtual) whenever the phone was lost or stolen, "
             "or for fraud, right after lock_account succeeds. Use scope 'physical' only when the caller lost just a physical card "
             "and still has their phone; then lock_account is NOT called.",
             {"rut": RUT,
              "scope": {"type": "string", "enum": ["all", "physical"], "description": "'all' or 'physical'."},
              "card_types": {"type": "array", "description": "Only for scope 'physical': which physical cards to block, e.g. ['debit']. Empty array blocks all physical cards.",
                             "items": {"type": "string", "enum": ["debit", "credit"], "description": "Physical card type."}}},
             ["rut", "scope"]),
        tool("create_incident", "/api/webhooks/incidents",
             "Registers the incident for the Fraud & Security team and returns a ticket id. Call after the account and cards are locked.",
             {"rut": RUT,
              "incident_type": {"type": "string", "enum": ["theft", "robbery", "loss", "fraud"], "description": "Incident type."},
              "incident_date": {"type": "string", "description": "Date of the incident, YYYY-MM-DD."},
              "incident_time": {"type": "string", "description": "Approximate time of the incident, HH:MM 24h."},
              "description": {"type": "string", "description": "One or two sentence summary of what happened, as confirmed with the caller."}},
             ["rut", "incident_type", "incident_date", "incident_time", "description"]),
        tool("submit_csat", "/api/webhooks/csat",
             "Records the caller's satisfaction survey answer at the end of the call. Call once, after the caller gives a 1-5 score.",
             {"score": {"type": "integer", "description": "Satisfaction score from 1 (very dissatisfied) to 5 (very satisfied)."},
              "comment": {"type": "string", "description": "Optional short comment from the caller, empty string if none."},
              "rut": {"type": "string", "description": "Customer RUT if known, empty string otherwise."}},
             ["score"]),
    ]


def req(method, url, body=None):
    r = urllib.request.Request(url, method=method, data=json.dumps(body).encode() if body else None,
                               headers={"xi-api-key": KEY, "Content-Type": "application/json"})
    try:
        return json.load(urllib.request.urlopen(r))
    except urllib.error.HTTPError as e:
        sys.exit(f"{method} {url} -> {e.code}: {e.read().decode()[:500]}")


if __name__ == "__main__":
    BASE = sys.argv[1].rstrip("/")
    existing = {t["tool_config"]["name"]: t["id"] for t in req("GET", f"{API}/tools")["tools"]}
    ids = []
    for cfg in tools():
        if cfg["name"] in existing:
            tid = existing[cfg["name"]]
            req("PATCH", f"{API}/tools/{tid}", {"tool_config": cfg})
            print(f"updated {cfg['name']} -> {cfg['api_schema']['url']}")
        else:
            tid = req("POST", f"{API}/tools", {"tool_config": cfg})["id"]
            print(f"created {cfg['name']} -> {cfg['api_schema']['url']}")
        ids.append(tid)

    agent = req("GET", f"{API}/agents/{AGENT_ID}")
    current = agent["conversation_config"]["agent"]["prompt"].get("tool_ids") or []
    req("PATCH", f"{API}/agents/{AGENT_ID}",
        {"conversation_config": {"agent": {"prompt": {"tool_ids": sorted(set(current + ids))}}}})
    print("agent tools:", len(set(current + ids)))
