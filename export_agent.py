"""Export the live agent configuration into agent/ (prompt, tools, settings, tests).

Usage: python3 export_agent.py
Needs ELEVENLABS_API_KEY in the environment. Strips creator and access metadata.
"""
import json
import os
import urllib.request
from pathlib import Path

AGENT_ID = "agent_0101m4y1ss4gekrb34sbfcnf2cm2"
API = "https://api.elevenlabs.io/v1/convai"
KEY = os.environ["ELEVENLABS_API_KEY"]
OUT = Path(__file__).parent / "agent"
TOOL_ORDER = ["validate_rut", "lookup_customer", "lock_account", "block_cards", "create_incident", "notify_fraud_team", "submit_csat"]
TURN_KEEP = ("role", "message", "time_in_call_secs")


def get(path):
    r = urllib.request.Request(f"{API}{path}", headers={"xi-api-key": KEY})
    return json.load(urllib.request.urlopen(r))


def dump(name, data):
    (OUT / name).write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")


def main():
    OUT.mkdir(exist_ok=True)
    agent = get(f"/agents/{AGENT_ID}")
    cc, ps = agent["conversation_config"], agent["platform_settings"]
    prompt = cc["agent"]["prompt"]
    (OUT / "system_prompt.md").write_text(prompt["prompt"] + "\n")

    tool_ids = set(prompt.get("tool_ids") or [])
    all_tools = get("/tools")["tools"]
    tool_names = {t["id"]: t["tool_config"]["name"] for t in all_tools}
    tools = []
    for t in all_tools:
        if t["id"] not in tool_ids:
            continue
        cfg = dict(t["tool_config"])
        if "api_schema" in cfg:
            cfg["api_schema"] = dict(cfg["api_schema"])
            url = cfg["api_schema"]["url"]
            if url.startswith("https://hooks.slack.com/"):
                url = "{SLACK_WEBHOOK_URL}"  # secret: never exported
            else:
                url = "{TUNNEL_URL}" + url.split(".trycloudflare.com", 1)[-1]
            cfg["api_schema"]["url"] = url
        tools.append({k: v for k, v in cfg.items() if v not in (None, [], {})})
    tools.sort(key=lambda c: TOOL_ORDER.index(c["name"]) if c["name"] in TOOL_ORDER else 99)
    dump("tools.json", tools)

    turn, soft = cc["turn"], cc["turn"].get("soft_timeout_config") or {}
    guard = ps.get("guardrails") or {}
    dump("agent_config.json", {
        "agent_id": agent["agent_id"],
        "name": agent["name"],
        "language": cc["agent"]["language"],
        "first_message": cc["agent"]["first_message"],
        "llm": prompt.get("llm"),
        "built_in_tools": [k for k, v in (prompt.get("built_in_tools") or {}).items() if v],
        "knowledge_base": [{"name": k["name"], "usage_mode": k.get("usage_mode")} for k in prompt.get("knowledge_base") or []],
        "dynamic_variables": (cc["agent"].get("dynamic_variables") or {}).get("dynamic_variable_placeholders"),
        "tts": {k: cc["tts"].get(k) for k in ("model_id", "voice_id", "speed", "stability")},
        "language_presets": {
            lang: {"first_message": v["overrides"]["agent"].get("first_message"),
                   "voice_id": (v["overrides"].get("tts") or {}).get("voice_id")}
            for lang, v in (cc.get("language_presets") or {}).items()},
        "turn": {"turn_eagerness": turn.get("turn_eagerness"), "turn_timeout": turn.get("turn_timeout"),
                 "soft_timeout": soft.get("timeout_seconds"),
                 "fillers": [soft.get("message")] + (soft.get("additional_soft_timeout_messages") or [])},
        "background_sound": (cc.get("conversation") or {}).get("background_sound"),
        "guardrails": {
            "focus": guard.get("focus", {}).get("is_enabled"),
            "prompt_injection": guard.get("prompt_injection", {}).get("is_enabled"),
            "custom": [{"name": c["name"], "prompt": c["prompt"]} for c in guard.get("custom", {}).get("config", {}).get("configs", []) if c.get("is_enabled")],
        },
        "data_collection": ps["data_collection"],
        "evaluation_criteria": [{"id": c["id"], "name": c["name"], "goal": c["conversation_goal_prompt"]}
                                for c in ps["evaluation"]["criteria"]],
    })

    tests = []
    for ref in (ps.get("testing") or {}).get("attached_tests") or []:
        t = get(f"/agent-testing/{ref['test_id']}")
        entry = {"name": t["name"], "type": t["type"],
                 "chat_history": [{k: m.get(k) for k in TURN_KEEP} for m in t.get("chat_history") or []]}
        for k in ("success_condition", "success_examples", "failure_examples"):
            if t.get(k):
                entry[k] = t[k]
        if t["type"] == "tool":
            tc = t["tool_call_parameters"]
            ref_tool = tc.get("referenced_tool") or {}
            entry["tool_call"] = {"tool": tool_names.get(ref_tool.get("id")),
                                  "parameters": tc.get("parameters"), "verify_absence": tc.get("verify_absence")}
        tests.append(entry)
    dump("tests.json", tests)
    print(f"Exported prompt, {len(tools)} tools, config and {len(tests)} tests to {OUT}/")


if __name__ == "__main__":
    main()
