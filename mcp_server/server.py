import os
import json
import time
import requests
from collections import defaultdict, Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta

from fastmcp import FastMCP
from dotenv import load_dotenv

load_dotenv()

PUBLIC_KEY = os.getenv("LANGFUSE_PUBLIC_KEY")
SECRET_KEY = os.getenv("LANGFUSE_SECRET_KEY")
HOST = os.getenv("LANGFUSE_BASE_URL", "https://cloud.langfuse.com")

JST = timezone(timedelta(hours=9))

# サービス本番開始日時: JST 2026/05/01 00:00 = UTC 2026/04/30 15:00
SERVICE_START_UTC = datetime(2026, 4, 30, 15, 0, 0, tzinfo=timezone.utc)

mcp = FastMCP("langfuse-sessions")


@mcp.tool()
def get_session_counts(
    days: int | None = None,
    from_date: str | None = None,
    to_date: str | None = None,
) -> str:
    """日付ごとのセッション件数を返す。

    件数・推移・アクセス状況を知りたいときに使う。
    セッションの内容（質問・回答）を知りたいときは get_session_list を使うこと。

    Args:
        days: 今日から遡る日数。from_date/to_date が指定された場合は無視される。
        from_date: 取得開始日（JST）。例: "2026/05/07"。省略するとサービス開始日(2026/05/01)以降。
        to_date: 取得終了日（JST、当日を含む）。例: "2026/05/08"。省略すると現在まで。
    """
    from_ts, to_ts = _resolve_time_range(days, from_date, to_date)
    traces = _fetch_traces(from_ts, to_ts)
    sessions = _aggregate_sessions_basic(traces)
    sessions = [s for s in sessions if s["userId"].startswith("wxo-chat-anonymous")]

    counts = Counter(s["start_time"][:10] for s in sessions)
    result = [{"date": d, "count": c} for d, c in sorted(counts.items())]
    return json.dumps(result, ensure_ascii=False)


@mcp.tool()
def get_session_list(
    days: int | None = None,
    from_date: str | None = None,
    to_date: str | None = None,
    limit: int | None = 20,
) -> str:
    """セッション一覧を返す。各セッションには最初のユーザー発言と最後のエージェント応答が含まれる。

    セッションの内容（どんな質問がされたか）を知りたいときに使う。
    件数や推移を知りたいときは get_session_counts を使うこと。

    Args:
        days: 今日から遡る日数。from_date/to_date が指定された場合は無視される。
        from_date: 取得開始日（JST）。例: "2026/05/07"。省略するとサービス開始日(2026/05/01)以降。
        to_date: 取得終了日（JST、当日を含む）。例: "2026/05/08"。省略すると現在まで。
        limit: 返すセッション数の上限（デフォルト: 20）。全件取得は None を指定。
    """
    from_ts, to_ts = _resolve_time_range(days, from_date, to_date)
    traces = _fetch_traces(from_ts, to_ts)
    sessions = _aggregate_sessions_basic(traces)
    sessions = [s for s in sessions if s["userId"].startswith("wxo-chat-anonymous")]

    if limit is not None:
        sessions = sessions[:limit]

    needed_tids = set()
    for s in sessions:
        if not s["first_user_message"] and s.get("_first_trace_id"):
            needed_tids.add(s["_first_trace_id"])
        if not s["last_assistant_message"] and s.get("_last_trace_id"):
            needed_tids.add(s["_last_trace_id"])

    obs_by_trace = _fetch_all_observations(from_ts, to_ts) if needed_tids else {}
    sessions = _fill_messages(sessions, obs_by_trace)

    return json.dumps(sessions, ensure_ascii=False)


# ---------- 時刻解決 ----------

def _resolve_time_range(
    days: int | None,
    from_date: str | None,
    to_date: str | None,
) -> tuple:
    """from_date/to_date（JST 日付文字列）または days から UTC タイムスタンプを返す。"""
    if from_date:
        dt = datetime.strptime(from_date, "%Y/%m/%d").replace(tzinfo=JST)
        from_ts = dt.astimezone(timezone.utc).isoformat()
    elif days is not None:
        from_ts = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    else:
        from_ts = SERVICE_START_UTC.isoformat()

    if to_date:
        # 当日末まで含むため翌日 00:00 JST を終端にする
        dt = (datetime.strptime(to_date, "%Y/%m/%d").replace(tzinfo=JST) + timedelta(days=1))
        to_ts = dt.astimezone(timezone.utc).isoformat()
    else:
        to_ts = None

    return from_ts, to_ts


# ---------- Langfuse API ----------

def _fetch_page(endpoint: str, params: dict) -> list:
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


def _fetch_all_pages(endpoint: str, base_params: dict) -> list:
    """ページ1を取得してトータルページ数を把握し、残りページを並列取得する。"""
    body = None
    for attempt in range(4):
        p1 = requests.get(
            f"{HOST}/api/public/{endpoint}",
            auth=(PUBLIC_KEY, SECRET_KEY),
            params={**base_params, "page": 1},
            timeout=30,
        )
        if p1.status_code == 429:
            time.sleep(0.5 * (2 ** attempt))
            continue
        p1.raise_for_status()
        body = p1.json()
        break

    if body is None:
        return []

    items = body.get("data", [])
    meta = body.get("meta", {})
    total_pages = meta.get("totalPages") or max(1, (meta.get("totalItems", 0) + base_params["limit"] - 1) // base_params["limit"])

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


def _fetch_traces(from_timestamp: str, to_timestamp: str | None = None) -> list:
    params = {"limit": 50, "fromTimestamp": from_timestamp}
    if to_timestamp:
        params["toTimestamp"] = to_timestamp
    return _fetch_all_pages("traces", params)


def _fetch_all_observations(from_timestamp: str, to_timestamp: str | None = None) -> dict:
    """LangGraph observations を一括取得して {traceId: [obs, ...]} で返す。"""
    params = {"limit": 50, "fromTimestamp": from_timestamp, "name": "LangGraph"}
    if to_timestamp:
        params["toTimestamp"] = to_timestamp
    items = _fetch_all_pages("observations", params)
    obs_by_trace: dict = defaultdict(list)
    for obs in items:
        tid = obs.get("traceId", "")
        if tid:
            obs_by_trace[tid].append(obs)
    return obs_by_trace


# ---------- トレースからの情報抽出 ----------

def _to_jst(ts_str: str) -> str:
    if not ts_str:
        return ""
    try:
        dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        return dt.astimezone(JST).strftime("%Y/%m/%d %H:%M")
    except Exception:
        return ts_str


def _get_user_message(trace: dict) -> str:
    try:
        inp = trace.get("input") or {}
        for m in inp.get("messages", []):
            if m.get("role") == "user":
                content = m.get("content", "")
                return (content if isinstance(content, str) else str(content))[:200]
    except Exception:
        pass
    return ""


def _get_assistant_message(trace: dict) -> str:
    try:
        out = trace.get("output") or {}
        for m in out.get("messages", []):
            if m.get("role") == "assistant":
                content = m.get("content", "")
                return (content if isinstance(content, str) else str(content))[:200]
    except Exception:
        pass
    return ""


def _get_agent_info(trace: dict) -> tuple:
    try:
        inp = trace.get("input") or {}
        return inp.get("current_agent_id", ""), inp.get("agent_display_name", "")
    except Exception:
        return "", ""


# ---------- セッション集計 ----------

def _aggregate_sessions_basic(traces: list) -> list:
    """トレースのみからセッションを集計する（observation なし）。"""
    sessions: dict = defaultdict(lambda: {
        "start_time": None,
        "turn_count": 0,
        "total_latency": 0.0,
        "userId": "",
        "agent_display_name": "",
        "first_user_message": "",
        "last_assistant_message": "",
        "_first_trace_id": "",
        "_last_trace_id": "",
    })

    for t in sorted(traces, key=lambda x: x.get("timestamp", "")):
        sid = t.get("sessionId", "unknown")
        ts = t.get("timestamp", "")
        s = sessions[sid]

        if s["start_time"] is None or ts < s["start_time"]:
            s["start_time"] = ts
            s["_first_trace_id"] = t.get("id", "")

        s["_last_trace_id"] = t.get("id", "")
        s["turn_count"] += 1
        s["total_latency"] += t.get("latency") or 0.0
        s["userId"] = s["userId"] or t.get("userId", "")

        _, agent_name = _get_agent_info(t)
        s["agent_display_name"] = s["agent_display_name"] or agent_name

        user_msg = _get_user_message(t)
        if user_msg and not s["first_user_message"]:
            s["first_user_message"] = user_msg

        asst_msg = _get_assistant_message(t)
        if asst_msg:
            s["last_assistant_message"] = asst_msg

    rows = []
    for sid, s in sorted(sessions.items(), key=lambda x: x[1]["start_time"] or ""):
        rows.append({
            "sessionId":              sid,
            "start_time":             _to_jst(s["start_time"]),
            "turn_count":             s["turn_count"],
            "total_latency":          round(s["total_latency"], 3),
            "userId":                 s["userId"],
            "agent_display_name":     s["agent_display_name"],
            "first_user_message":     s["first_user_message"],
            "last_assistant_message": s["last_assistant_message"],
            "_first_trace_id":        s["_first_trace_id"],
            "_last_trace_id":         s["_last_trace_id"],
        })
    return rows


def _fill_messages(sessions: list, obs_by_trace: dict) -> list:
    """observation キャッシュから不足メッセージを補完して _first/_last_trace_id を除去する。"""
    for s in sessions:
        if not s["first_user_message"] and s.get("_first_trace_id"):
            for obs in obs_by_trace.get(s["_first_trace_id"], []):
                inp = obs.get("input") or {}
                if isinstance(inp, dict):
                    for m in inp.get("messages", []):
                        if m.get("role") == "user":
                            content = m.get("content", "")
                            s["first_user_message"] = (content if isinstance(content, str) else str(content))[:200]
                            break
                if s["first_user_message"]:
                    break

        if not s["last_assistant_message"] and s.get("_last_trace_id"):
            for obs in obs_by_trace.get(s["_last_trace_id"], []):
                out = obs.get("output") or {}
                if isinstance(out, dict):
                    for m in out.get("messages", []):
                        if m.get("role") == "assistant":
                            content = m.get("content", "")
                            s["last_assistant_message"] = (content if isinstance(content, str) else str(content))[:200]
                            break
                if s["last_assistant_message"]:
                    break

        s.pop("_first_trace_id", None)
        s.pop("_last_trace_id", None)

    return sessions


if __name__ == "__main__":
    mcp.run()
