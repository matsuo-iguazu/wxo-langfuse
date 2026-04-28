import os
import csv
import requests
from collections import defaultdict
from dotenv import load_dotenv

load_dotenv()

SECRET_KEY = os.getenv("LANGFUSE_SECRET_KEY")
PUBLIC_KEY = os.getenv("LANGFUSE_PUBLIC_KEY")
HOST = os.getenv("LANGFUSE_HOST", "https://cloud.langfuse.com")

def fetch_all_traces():
    traces = []
    page = 1
    while True:
        response = requests.get(
            f"{HOST}/api/public/traces",
            auth=(PUBLIC_KEY, SECRET_KEY),
            params={"page": page, "limit": 50}
        )
        response.raise_for_status()
        data = response.json()
        items = data.get("data", [])
        if not items:
            break
        traces.extend(items)
        print(f"  ページ {page}: {len(items)}件取得")
        if len(items) < 50:
            break
        page += 1
    return traces

def get_user_message(trace):
    try:
        messages = trace.get("input", {}).get("messages", [])
        for m in messages:
            if m.get("role") == "user":
                return m.get("content", "")[:200]
    except Exception:
        pass
    return ""

def get_assistant_message(trace):
    try:
        messages = trace.get("output", {}).get("messages", [])
        for m in messages:
            if m.get("role") == "assistant":
                return m.get("content", "")[:200]
    except Exception:
        pass
    return ""

def get_agent_info(trace):
    try:
        inp = trace.get("input", {})
        agent_id = inp.get("current_agent_id", "")
        agent_name = inp.get("agent_display_name", "")
        return agent_id, agent_name
    except Exception:
        pass
    return "", ""

def export_traces_csv(traces, output_file="traces.csv"):
    fieldnames = [
        "id", "timestamp", "sessionId", "userId",
        "agent_id", "agent_display_name",
        "latency", "user_message", "assistant_message"
    ]
    with open(output_file, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for t in traces:
            agent_id, agent_name = get_agent_info(t)
            writer.writerow({
                "id": t.get("id", ""),
                "timestamp": t.get("timestamp", ""),
                "sessionId": t.get("sessionId", ""),
                "userId": t.get("userId", ""),
                "agent_id": agent_id,
                "agent_display_name": agent_name,
                "latency": t.get("latency", ""),
                "user_message": get_user_message(t),
                "assistant_message": get_assistant_message(t),
            })
    print(f"✅ トレース {len(traces)}件 → {output_file}")

def export_sessions_csv(traces, output_file="sessions.csv"):
    sessions = defaultdict(lambda: {
        "start_time": None,
        "turn_count": 0,
        "total_latency": 0.0,
        "userId": "",
        "agent_id": "",
        "agent_display_name": "",
        "first_user_message": "",
        "last_assistant_message": "",
    })

    for t in sorted(traces, key=lambda x: x.get("timestamp", "")):
        sid = t.get("sessionId", "unknown")
        ts = t.get("timestamp", "")
        s = sessions[sid]

        if s["start_time"] is None or ts < s["start_time"]:
            s["start_time"] = ts

        s["turn_count"] += 1
        s["total_latency"] += t.get("latency") or 0.0
        s["userId"] = s["userId"] or t.get("userId", "")

        agent_id, agent_name = get_agent_info(t)
        s["agent_id"] = s["agent_id"] or agent_id
        s["agent_display_name"] = s["agent_display_name"] or agent_name

        user_msg = get_user_message(t)
        if user_msg and not s["first_user_message"]:
            s["first_user_message"] = user_msg

        asst_msg = get_assistant_message(t)
        if asst_msg:
            s["last_assistant_message"] = asst_msg

    fieldnames = [
        "sessionId", "start_time", "turn_count", "total_latency",
        "userId", "agent_id", "agent_display_name",
        "first_user_message", "last_assistant_message"
    ]
    rows = sorted(sessions.items(), key=lambda x: x[1]["start_time"] or "", reverse=True)

    with open(output_file, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for sid, s in rows:
            writer.writerow({
                "sessionId": sid,
                "start_time": s["start_time"],
                "turn_count": s["turn_count"],
                "total_latency": round(s["total_latency"], 3),
                "userId": s["userId"],
                "agent_id": s["agent_id"],
                "agent_display_name": s["agent_display_name"],
                "first_user_message": s["first_user_message"],
                "last_assistant_message": s["last_assistant_message"],
            })
    print(f"✅ セッション {len(sessions)}件 → {output_file}")

if __name__ == "__main__":
    print("Langfuse からトレースを取得中...")
    traces = fetch_all_traces()
    print(f"合計 {len(traces)} 件取得\n")
    export_traces_csv(traces)
    export_sessions_csv(traces)
