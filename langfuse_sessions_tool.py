import io
import requests
from collections import defaultdict
from datetime import datetime, timezone, timedelta

from ibm_watsonx_orchestrate.agent_builder.tools import tool
from ibm_watsonx_orchestrate.agent_builder.connections import ConnectionType
from ibm_watsonx_orchestrate.run import connections

LANGFUSE_APP_ID = "m-langfuse"
JST = timezone(timedelta(hours=9))

HEADER_BG = "375623"
EVEN_ROW_BG = "EBF5E0"
ROW_HEIGHT = 15
FONT_NAME = "游ゴシック"

COLUMNS = [
    ("sessionId",             "セッションID",         28),
    ("start_time",            "開始時刻 (JST)",       18),
    ("turn_count",            "ターン数",              10),
    ("total_latency",         "合計レイテンシ(秒)",   18),
    ("userId",                "ユーザーID",            28),
    ("agent_id",              "エージェントID",        36),
    ("agent_display_name",    "エージェント名",        20),
    ("first_user_message",    "最初のユーザー発言",   42),
    ("last_assistant_message","最後のAI応答",          42),
]

RIGHT_ALIGNED = {"turn_count", "total_latency"}


@tool(
    name="m_export_langfuse_sessions",
    display_name="M - Langfuse セッション集計 Excel エクスポート",
    expected_credentials=[{"app_id": LANGFUSE_APP_ID, "type": ConnectionType.KEY_VALUE}],
)
def export_langfuse_sessions() -> bytes:
    """Langfuse からトレースを取得し、セッション単位に集計した Excel ファイルをバイト列で返す。

    Returns:
        bytes: セッション集計 Excel (.xlsx) のバイト列（ファイルダウンロード用）。
    """
    creds = connections.key_value(LANGFUSE_APP_ID)
    public_key = creds.get("LANGFUSE_PUBLIC_KEY")
    secret_key = creds.get("LANGFUSE_SECRET_KEY")
    host = creds.get("LANGFUSE_HOST", "https://cloud.langfuse.com")

    traces = _fetch_all_traces(public_key, secret_key, host)
    rows = _aggregate_sessions(traces)
    return _build_xlsx(rows)


# ---------- Langfuse API ----------

def _fetch_all_traces(public_key: str, secret_key: str, host: str) -> list:
    traces = []
    page = 1
    while True:
        response = requests.get(
            f"{host}/api/public/traces",
            auth=(public_key, secret_key),
            params={"page": page, "limit": 50},
        )
        response.raise_for_status()
        data = response.json()
        items = data.get("data", [])
        if not items:
            break
        traces.extend(items)
        if len(items) < 50:
            break
        page += 1
    return traces


def _get_user_message(trace: dict) -> str:
    try:
        for m in trace.get("input", {}).get("messages", []):
            if m.get("role") == "user":
                return m.get("content", "")[:200]
    except Exception:
        pass
    return ""


def _get_assistant_message(trace: dict) -> str:
    try:
        for m in trace.get("output", {}).get("messages", []):
            if m.get("role") == "assistant":
                return m.get("content", "")[:200]
    except Exception:
        pass
    return ""


def _get_agent_info(trace: dict) -> tuple:
    try:
        inp = trace.get("input", {})
        return inp.get("current_agent_id", ""), inp.get("agent_display_name", "")
    except Exception:
        return "", ""


def _to_jst(ts_str: str) -> str:
    if not ts_str:
        return ""
    try:
        dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        return dt.astimezone(JST).strftime("%Y/%m/%d %H:%M")
    except Exception:
        return ts_str


# ---------- 集計 ----------

def _aggregate_sessions(traces: list) -> list:
    sessions: dict = defaultdict(lambda: {
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

        agent_id, agent_name = _get_agent_info(t)
        s["agent_id"] = s["agent_id"] or agent_id
        s["agent_display_name"] = s["agent_display_name"] or agent_name

        user_msg = _get_user_message(t)
        if user_msg and not s["first_user_message"]:
            s["first_user_message"] = user_msg

        asst_msg = _get_assistant_message(t)
        if asst_msg:
            s["last_assistant_message"] = asst_msg

    rows = []
    for sid, s in sorted(sessions.items(), key=lambda x: x[1]["start_time"] or "", reverse=True):
        rows.append({
            "sessionId":              sid,
            "start_time":             _to_jst(s["start_time"]),
            "turn_count":             s["turn_count"],
            "total_latency":          round(s["total_latency"], 3),
            "userId":                 s["userId"],
            "agent_id":               s["agent_id"],
            "agent_display_name":     s["agent_display_name"],
            "first_user_message":     s["first_user_message"],
            "last_assistant_message": s["last_assistant_message"],
        })
    return rows


# ---------- Excel 出力 ----------

def _build_xlsx(rows: list) -> bytes:
    from openpyxl import Workbook
    from openpyxl.styles import PatternFill, Font, Alignment
    from openpyxl.utils import get_column_letter

    wb = Workbook()
    ws = wb.active
    ws.title = "セッション集計"

    header_fill = PatternFill("solid", fgColor=HEADER_BG)
    even_fill   = PatternFill("solid", fgColor=EVEN_ROW_BG)
    header_font = Font(name=FONT_NAME, bold=True, color="FFFFFF")
    data_font   = Font(name=FONT_NAME)

    # ヘッダー行
    for col_idx, (key, label, width) in enumerate(COLUMNS, start=1):
        cell = ws.cell(row=1, column=col_idx, value=label)
        cell.fill = header_fill
        cell.font = header_font
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=False)
        ws.column_dimensions[get_column_letter(col_idx)].width = width

    ws.auto_filter.ref = f"A1:{get_column_letter(len(COLUMNS))}1"
    ws.freeze_panes = "A2"
    ws.row_dimensions[1].height = ROW_HEIGHT

    # データ行
    for row_idx, row in enumerate(rows, start=2):
        fill = even_fill if row_idx % 2 == 0 else None
        for col_idx, (key, _label, _width) in enumerate(COLUMNS, start=1):
            value = row[key]
            cell = ws.cell(row=row_idx, column=col_idx, value=value)
            if fill:
                cell.fill = fill
            cell.font = data_font
            align = "right" if key in RIGHT_ALIGNED else "left"
            cell.alignment = Alignment(horizontal=align, vertical="center", wrap_text=False)
        ws.row_dimensions[row_idx].height = ROW_HEIGHT

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
