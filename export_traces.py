import os
import csv
import time
import requests
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv

load_dotenv()

SECRET_KEY = os.getenv("LANGFUSE_SECRET_KEY")
PUBLIC_KEY = os.getenv("LANGFUSE_PUBLIC_KEY")
HOST = os.getenv("LANGFUSE_HOST", "https://cloud.langfuse.com")

JST = timezone(timedelta(hours=9))
SERVICE_START_UTC = datetime(2026, 4, 30, 15, 0, 0, tzinfo=timezone.utc)


def _fetch_page(endpoint, params):
    for attempt in range(4):
        resp = requests.get(
            f"{HOST}/api/public/{endpoint}",
            auth=(PUBLIC_KEY, SECRET_KEY),
            params=params,
            timeout=30,
        )
        if resp.status_code == 429:
            time.sleep(0.5 * (2 ** attempt))
            continue
        resp.raise_for_status()
        return resp.json().get("data", [])
    return []


def _fetch_all_pages(endpoint, base_params):
    p1 = requests.get(
        f"{HOST}/api/public/{endpoint}",
        auth=(PUBLIC_KEY, SECRET_KEY),
        params={**base_params, "page": 1},
        timeout=30,
    )
    p1.raise_for_status()
    body = p1.json()
    items = body.get("data", [])
    meta = body.get("meta", {})
    total_pages = meta.get("totalPages") or max(
        1, (meta.get("totalItems", 0) + base_params["limit"] - 1) // base_params["limit"]
    )

    if total_pages <= 1:
        return items

    results = list(items)
    with ThreadPoolExecutor(max_workers=5) as ex:
        futs = {
            ex.submit(_fetch_page, endpoint, {**base_params, "page": p}): p
            for p in range(2, total_pages + 1)
        }
        for fut in as_completed(futs):
            results.extend(fut.result())
    return results


def fetch_all_traces(from_timestamp=None):
    if from_timestamp is None:
        from_timestamp = SERVICE_START_UTC.isoformat()
    print(f"  fromTimestamp: {from_timestamp}")
    return _fetch_all_pages("traces", {"limit": 50, "fromTimestamp": from_timestamp})


def fetch_all_observations(from_timestamp=None):
    """LangGraph observations を一括取得して {traceId: [obs, ...]} で返す。"""
    if from_timestamp is None:
        from_timestamp = SERVICE_START_UTC.isoformat()
    items = _fetch_all_pages(
        "observations",
        {"limit": 50, "fromTimestamp": from_timestamp, "name": "LangGraph"},
    )
    obs_by_trace = defaultdict(list)
    for obs in items:
        tid = obs.get("traceId", "")
        if tid:
            obs_by_trace[tid].append(obs)
    return obs_by_trace


def _to_jst(ts_str):
    if not ts_str:
        return ""
    try:
        dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        return dt.astimezone(JST).strftime("%Y/%m/%d %H:%M")
    except Exception:
        return ts_str


def get_user_message(trace, obs_list=None):
    try:
        inp = trace.get("input") or {}
        for m in inp.get("messages", []):
            if m.get("role") == "user":
                content = m.get("content", "")
                return (content if isinstance(content, str) else str(content))[:200]
    except Exception:
        pass
    if obs_list:
        for obs in obs_list:
            try:
                inp = obs.get("input") or {}
                if isinstance(inp, dict):
                    for m in inp.get("messages", []):
                        if m.get("role") == "user":
                            content = m.get("content", "")
                            return (content if isinstance(content, str) else str(content))[:200]
            except Exception:
                pass
    return ""


def get_assistant_message(trace, obs_list=None):
    try:
        out = trace.get("output") or {}
        for m in out.get("messages", []):
            if m.get("role") == "assistant":
                content = m.get("content", "")
                return (content if isinstance(content, str) else str(content))[:200]
    except Exception:
        pass
    if obs_list:
        for obs in reversed(obs_list or []):
            try:
                out = obs.get("output") or {}
                if isinstance(out, dict):
                    for m in out.get("messages", []):
                        if m.get("role") == "assistant":
                            content = m.get("content", "")
                            return (content if isinstance(content, str) else str(content))[:200]
            except Exception:
                pass
    return ""


def get_agent_info(trace, obs_list=None):
    try:
        inp = trace.get("input") or {}
        agent_id = inp.get("current_agent_id", "")
        agent_name = inp.get("agent_display_name", "")
        if agent_id or agent_name:
            return agent_id, agent_name
    except Exception:
        pass
    if obs_list:
        for obs in obs_list:
            try:
                inp = obs.get("input") or {}
                if isinstance(inp, dict):
                    agent_id = inp.get("current_agent_id", "")
                    agent_name = inp.get("agent_display_name", "")
                    if agent_id or agent_name:
                        return agent_id, agent_name
            except Exception:
                pass
    return "", ""


def export_traces_csv(traces, obs_by_trace, output_file="traces.csv"):
    fieldnames = [
        "id", "timestamp", "sessionId", "userId",
        "agent_id", "agent_display_name",
        "latency", "user_message", "assistant_message"
    ]
    with open(output_file, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for t in traces:
            obs_list = obs_by_trace.get(t.get("id", ""), [])
            agent_id, agent_name = get_agent_info(t, obs_list)
            writer.writerow({
                "id": t.get("id", ""),
                "timestamp": _to_jst(t.get("timestamp", "")),
                "sessionId": t.get("sessionId", ""),
                "userId": t.get("userId", ""),
                "agent_id": agent_id,
                "agent_display_name": agent_name,
                "latency": t.get("latency", ""),
                "user_message": get_user_message(t, obs_list),
                "assistant_message": get_assistant_message(t, obs_list),
            })
    print(f"✅ トレース {len(traces)}件 → {output_file}")


def export_sessions_csv(traces, obs_by_trace, output_file="sessions.csv"):
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
        obs_list = obs_by_trace.get(t.get("id", ""), [])

        if s["start_time"] is None or ts < s["start_time"]:
            s["start_time"] = ts

        s["turn_count"] += 1
        s["total_latency"] += t.get("latency") or 0.0
        s["userId"] = s["userId"] or t.get("userId", "")

        agent_id, agent_name = get_agent_info(t, obs_list)
        s["agent_id"] = s["agent_id"] or agent_id
        s["agent_display_name"] = s["agent_display_name"] or agent_name

        user_msg = get_user_message(t, obs_list)
        if user_msg and not s["first_user_message"]:
            s["first_user_message"] = user_msg

        asst_msg = get_assistant_message(t, obs_list)
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
                "start_time": _to_jst(s["start_time"]),
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

    print("LangGraph observations を取得中...")
    obs_by_trace = fetch_all_observations()
    print(f"対象トレース {len(obs_by_trace)} 件分の observations 取得\n")

    export_traces_csv(traces, obs_by_trace)
    export_sessions_csv(traces, obs_by_trace)
