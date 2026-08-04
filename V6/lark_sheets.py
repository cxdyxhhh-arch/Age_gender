"""
飞书 / Lark 电子表格 (Sheets) 读写工具 - 支持输入表 / 输出表分开。

输入表  (URL 列表) : https://bytedance.my.larkoffice.com/sheets/GM2qsTZmShirZCtuVS8mlDrgy9e
输出表  (分析结果) : https://bytedance.my.larkoffice.com/sheets/T0AXszocJhDv2UtDlA9mXNyFy7f

最小可用的表结构:
  输入表 - Sheet1      A1 标题 "URL" (第 2 行起填 TikTok URL)
  输出表 - Sheet1      A1~L1 表头: URL handle display_name bio age_group gender
                       confidence reasoning avatar_screenshot page_screenshot error created_at
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Iterable, List, Optional

HERE = Path(__file__).resolve().parent
try:
    from dotenv import load_dotenv  # type: ignore
    load_dotenv(HERE / ".env", override=True)
except Exception:
    env_path = HERE / ".env"
    if env_path.is_file():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k, v = k.strip(), v.strip().strip('"').strip("'")
            os.environ.setdefault(k, v)

import urllib.parse
import urllib.request
import urllib.error

_TOKEN_CACHE: dict = {"token": None, "expires_at": 0}


# ============================================================= #
# 统一行数上限配置（单一数据源）
# 所有链路（读输入表 / 读输出表去重 / 写回定位空行 / 清空）都从这里取上限，
# 想调整只改 .env 里的 MAX_SHEET_ROWS 即可，不必到处改数字。
# ============================================================= #
_DEFAULT_MAX_SHEET_ROWS = 200000


def max_sheet_rows() -> int:
    """从环境变量 MAX_SHEET_ROWS 读取全局行数上限，非法/缺失时回退默认值。"""
    raw = os.environ.get("MAX_SHEET_ROWS")
    if not raw:
        return _DEFAULT_MAX_SHEET_ROWS
    try:
        v = int(str(raw).strip())
    except (TypeError, ValueError):
        return _DEFAULT_MAX_SHEET_ROWS
    return v if v > 0 else _DEFAULT_MAX_SHEET_ROWS


# 模块级常量：导入时求值一次，供其它模块直接引用（lark_sheets.MAX_SHEET_ROWS）
MAX_SHEET_ROWS = max_sheet_rows()


def _base_url() -> str:
    return os.environ.get("FEISHU_BASE_URL") or "https://open.larkoffice.com"


def _post_json(path: str, body: dict, params: Optional[dict] = None, token: Optional[str] = None) -> dict:
    url = _base_url() + path
    if params:
        url = url + "?" + urllib.parse.urlencode(params)
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    headers = {"Content-Type": "application/json; charset=utf-8"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Lark API HTTP {exc.code}: {raw[:500]}") from exc


def _get_json(path: str, params: Optional[dict] = None, token: Optional[str] = None) -> dict:
    url = _base_url() + path
    if params:
        url = url + "?" + urllib.parse.urlencode(params)
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, headers=headers, method="GET")
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _put_json(path: str, body: dict, params: Optional[dict] = None, token: Optional[str] = None) -> dict:
    url = _base_url() + path
    if params:
        url = url + "?" + urllib.parse.urlencode(params)
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    headers = {"Content-Type": "application/json; charset=utf-8"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=data, headers=headers, method="PUT")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Lark API HTTP {exc.code}: {raw[:500]}") from exc


# ============================================================= #
# 认证
# ============================================================= #
def get_tenant_access_token(app_id: str, app_secret: str) -> str:
    now = int(time.time())
    if _TOKEN_CACHE["token"] and _TOKEN_CACHE["expires_at"] > now + 60:
        return _TOKEN_CACHE["token"]

    resp = _post_json(
        "/open-apis/auth/v3/tenant_access_token/internal",
        body={"app_id": app_id, "app_secret": app_secret},
    )
    token = resp.get("tenant_access_token")
    expire = resp.get("expire", 120 * 60)
    if not token:
        raise RuntimeError(
            f"获取 tenant_access_token 失败: {json.dumps(resp, ensure_ascii=False)[:400]}。"
            f"请确认 FEISHU_APP_ID/FEISHU_APP_SECRET 正确。"
        )
    _TOKEN_CACHE["token"] = token
    _TOKEN_CACHE["expires_at"] = now + int(expire)
    return token


def _token_from_env() -> str:
    app_id = os.environ.get("FEISHU_APP_ID")
    app_secret = os.environ.get("FEISHU_APP_SECRET")
    if not (app_id and app_secret):
        raise RuntimeError("缺少 FEISHU_APP_ID / FEISHU_APP_SECRET")
    return get_tenant_access_token(app_id, app_secret)


# ---------- 输入表 / 输出表分开 ----------
def input_spreadsheet_token() -> str:
    t = os.environ.get("INPUT_SPREADSHEET_TOKEN")
    if not t:
        raise RuntimeError("缺少 INPUT_SPREADSHEET_TOKEN (输入表 token)")
    return t


def output_spreadsheet_token() -> str:
    t = os.environ.get("OUTPUT_SPREADSHEET_TOKEN")
    if not t:
        raise RuntimeError("缺少 OUTPUT_SPREADSHEET_TOKEN (输出表 token)")
    return t


def input_sheet_name() -> str:
    return os.environ.get("INPUT_SHEET_NAME", "Sheet1")


def output_sheet_name() -> str:
    return os.environ.get("OUTPUT_SHEET_NAME", "Sheet1")


def input_url_column() -> str:
    return os.environ.get("INPUT_URL_COLUMN", "URL")


# ============================================================= #
# 列号 <-> A1 记号
# ============================================================= #
_A_Z = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"


def col_to_a1(col: int) -> str:
    s: List[str] = []
    n = col
    while True:
        s.append(_A_Z[n % 26])
        n = n // 26 - 1
        if n < 0:
            break
    return "".join(reversed(s))


def a1_to_col(a1: str) -> int:
    a1 = a1.upper().strip()
    n = 0
    for ch in a1:
        if ch not in _A_Z:
            break
        n = n * 26 + (ord(ch) - ord("A") + 1)
    return n - 1


# ============================================================= #
# sheet id 解析
# ============================================================= #
def _resolve_sheet_id(spreadsheet_token: str, sheet_name_or_id: str) -> str:
    if isinstance(sheet_name_or_id, (int, float)):
        sheet_name_or_id = str(sheet_name_or_id)
    token = _token_from_env()
    resp = _get_json(
        f"/open-apis/sheets/v3/spreadsheets/{spreadsheet_token}/sheets/query",
        token=token,
    )
    if resp.get("code") not in (0, None):
        raise RuntimeError(
            f"读取 sheet 列表失败: code={resp.get('code')}, msg={resp.get('msg','')[:300]}"
        )
    sheets = (resp.get("data") or {}).get("sheets") or []
    for s in sheets:
        sid = s.get("sheet_id") or (s.get("properties") or {}).get("sheet_id")
        title = s.get("title") or (s.get("properties") or {}).get("title")
        if title == sheet_name_or_id:
            return str(sid)
        if str(sid) == str(sheet_name_or_id):
            return str(sid)
    names = ", ".join(
        f"'{s.get('title') or (s.get('properties') or {}).get('title')}'"
        for s in sheets
    )
    raise RuntimeError(f"找不到名为 '{sheet_name_or_id}' 的 sheet, 当前表只有: {names}")


# ============================================================= #
# 读: 从输入表读取 URL
# ============================================================= #
def _read_range_values(spreadsheet: str, sheet_id: str, rng: str, token: str) -> list:
    resp = _get_json(
        f"/open-apis/sheets/v2/spreadsheets/{spreadsheet}/values/{sheet_id}!{rng}",
        token=token,
    )
    if resp.get("code") not in (0, None):
        raise RuntimeError(f"读取 {rng} 失败: code={resp.get('code')}, msg={resp.get('msg','')[:300]}")
    vr = (resp.get("data") or {}).get("valueRange") or {}
    return vr.get("values") or []


def read_urls(max_rows: Optional[int] = None) -> List[str]:
    """从输入表的 URL 列读取一批 URL。max_rows 缺省时用全局 MAX_SHEET_ROWS。"""
    if max_rows is None or max_rows <= 0:
        max_rows = max_sheet_rows()
    spreadsheet = input_spreadsheet_token()
    sheet_id = _resolve_sheet_id(spreadsheet, input_sheet_name())
    token = _token_from_env()
    header = input_url_column()

    end_row = max_rows + 1
    rows = _read_range_values(spreadsheet, sheet_id, f"A1:ZZ{end_row}", token)
    if not rows:
        return []

    header_row = rows[0]
    col_index: Optional[int] = None
    for i, val in enumerate(header_row):
        s = (str(val) if val is not None else "").strip()
        if s == header:
            col_index = i
            break
    if col_index is None:
        raise RuntimeError(
            f"在输入表 '{input_sheet_name()}' 第 1 行找不到列名 '{header}'。"
            f"实际表头前 20 列: {[str(x) for x in header_row[:20]]}"
        )

    urls: list[str] = []
    for row in rows[1:]:
        if col_index >= len(row):
            continue
        v = row[col_index]
        if isinstance(v, dict):
            s = (v.get('text') or v.get('link') or "").strip()
        elif isinstance(v, list) and len(v) > 0 and isinstance(v[0], dict):
            s = (v[0].get('text') or v[0].get('link') or "").strip()
        else:
            s = (str(v) if v is not None else "").strip()
        if s:
            urls.append(s)

    seen: set[str] = set()
    uniq: list[str] = []
    for u in urls:
        if u in seen:
            continue
        seen.add(u)
        uniq.append(u)
    return uniq


# ============================================================= #
# 写: 把结果追加到输出表
# ============================================================= #
OUTPUT_HEADERS = [
    "URL", "handle", "display_name", "bio",
    "age_group", "gender", "confidence", "reasoning",
    "avatar_screenshot", "page_screenshot", "error", "created_at",
]


def _ensure_header(spreadsheet: str, sheet_id: str, token: str) -> None:
    range_ref = f"A1:{col_to_a1(len(OUTPUT_HEADERS) - 1)}1"
    rows = _read_range_values(spreadsheet, sheet_id, range_ref, token)
    first_row = rows[0] if rows else []
    nonempty = [c for c in first_row if isinstance(c, str) and c.strip()]
    if not nonempty:
        _put_json(
            f"/open-apis/sheets/v2/spreadsheets/{spreadsheet}/values",
            body={"valueRange": {"range": f"{sheet_id}!{range_ref}", "values": [OUTPUT_HEADERS]}},
            params={"valueInputOption": "UserEntered"},
            token=token,
        )


def write_rows(records: Iterable[dict], batch_size: int = 30) -> None:
    """
    把每条 record 按 OUTPUT_HEADERS 顺序取字段, 追加到输出表的末尾。
    """
    records = list(records)
    if not records:
        return

    spreadsheet = output_spreadsheet_token()
    sheet_id = _resolve_sheet_id(spreadsheet, output_sheet_name())
    token = _token_from_env()

    _ensure_header(spreadsheet, sheet_id, token)

    # 找第一空行
    last_col_letter = col_to_a1(len(OUTPUT_HEADERS) - 1)
    rows = _read_range_values(spreadsheet, sheet_id, f"A2:{last_col_letter}{max_sheet_rows() + 1}", token)
    first_empty_row = 2
    for i, row in enumerate(rows):
        is_empty = True
        for cell in row:
            if isinstance(cell, str) and cell.strip():
                is_empty = False
                break
            if isinstance(cell, (int, float)) and cell not in (0, 0.0):
                is_empty = False
                break
        if is_empty:
            first_empty_row = 2 + i
            break
    else:
        first_empty_row = 2 + len(rows)

    rows_values: list[list[str]] = []
    for r in records:
        row = []
        for h in OUTPUT_HEADERS:
            if h == "URL":
                v = r.get("url") or r.get("URL") or ""
            else:
                v = r.get(h.lower(), "")
            if v is None:
                v = ""
            v = str(v)
            if len(v) > 5000:
                v = v[:5000] + "..."
            row.append(v)
        rows_values.append(row)

    print(f"写入输出表: sheet_id={sheet_id}, 从第 {first_empty_row} 行开始, 共 {len(rows_values)} 条")

    for i in range(0, len(rows_values), batch_size):
        chunk = rows_values[i : i + batch_size]
        from_row = first_empty_row + i
        to_row = from_row + len(chunk) - 1
        range_ref = f"A{from_row}:{col_to_a1(len(OUTPUT_HEADERS) - 1)}{to_row}"

        resp = _put_json(
            f"/open-apis/sheets/v2/spreadsheets/{spreadsheet}/values",
            body={
                "valueRange": {
                    "range": f"{sheet_id}!{range_ref}",
                    "values": chunk,
                }
            },
            params={"valueInputOption": "UserEntered"},
            token=token,
        )
        if resp.get("code") not in (0, None):
            raise RuntimeError(
                f"写入失败: code={resp.get('code')}, msg={resp.get('msg','')[:300]}"
            )
        print(f"  已写入 {len(chunk)} 行, 范围 {range_ref}")


def read_existing_urls() -> set[str]:
    """读取输出表中已经写过的 URL，用于去重。
    读取 A 列（第 1 列）从第 2 行开始的值。
    """
    spreadsheet = output_spreadsheet_token()
    sheet_id = _resolve_sheet_id(spreadsheet, output_sheet_name())
    token = _token_from_env()

    rows = _read_range_values(spreadsheet, sheet_id, f"A2:A{max_sheet_rows() + 1}", token)
    existing: set[str] = set()
    for row in rows:
        if not row:
            continue
        v = row[0]
        if isinstance(v, dict):
            s = (v.get('text') or v.get('link') or "").strip()
        elif isinstance(v, list) and len(v) > 0 and isinstance(v[0], dict):
            s = (v[0].get('text') or v[0].get('link') or "").strip()
        else:
            s = (str(v) if v is not None else "").strip()
        if s:
            existing.add(s)
    print(f"  输出表中已有 {len(existing)} 个 URL")
    return existing


def clear_output_sheet() -> None:
    """清空输出表的数据（保留表头第 1 行）。
    通过把第 2-2000 行全部写入空字符串实现。
    """
    spreadsheet = output_spreadsheet_token()
    sheet_id = _resolve_sheet_id(spreadsheet, output_sheet_name())
    token = _token_from_env()

    _ensure_header(spreadsheet, sheet_id, token)

    # 先读一下看看有多少行数据
    last_col_letter = col_to_a1(len(OUTPUT_HEADERS) - 1)
    rows = _read_range_values(spreadsheet, sheet_id, f"A2:{last_col_letter}{max_sheet_rows() + 1}", token)
    data_rows = 0
    for row in rows:
        has_data = any(
            (isinstance(c, str) and c.strip()) or
            (isinstance(c, (int, float)) and c not in (0, 0.0))
            for c in row
        )
        if has_data:
            data_rows += 1
    if data_rows == 0:
        print("  输出表没有数据需要清空")
        return

    print(f"  清空输出表中 {data_rows} 行数据（保留表头）...")

    # 写空字符串覆盖每一行
    empty_rows = [["" for _ in range(len(OUTPUT_HEADERS))] for _ in range(max(data_rows, 100))]
    range_ref = f"A2:{last_col_letter}{1 + len(empty_rows)}"
    _put_json(
        f"/open-apis/sheets/v2/spreadsheets/{spreadsheet}/values",
        body={"valueRange": {"range": f"{sheet_id}!{range_ref}", "values": empty_rows}},
        params={"valueInputOption": "UserEntered"},
        token=token,
    )
    print("  清空完成")


if __name__ == "__main__":
    import sys

    if len(sys.argv) >= 2 and sys.argv[1] == "--list-sheets":
        # 同时列出输入/输出两个表格
        for name, token_fn in [("输入表", input_spreadsheet_token),
                                ("输出表", output_spreadsheet_token)]:
            try:
                spreadsheet = token_fn()
                token = _token_from_env()
                resp = _get_json(
                    f"/open-apis/sheets/v3/spreadsheets/{spreadsheet}/sheets/query",
                    token=token,
                )
                sheets = ((resp.get("data") or {}).get("sheets")) or []
                print(f"[{name}] {spreadsheet} 包含 {len(sheets)} 个 sheet:")
                for s in sheets:
                    sid = s.get("sheet_id") or (s.get("properties") or {}).get("sheet_id")
                    title = s.get("title") or (s.get("properties") or {}).get("title")
                    print(f"  - title='{title}'  sheet_id={sid}")
            except Exception as e:
                print(f"[{name}] 读取失败: {e}")
            print()
    elif len(sys.argv) >= 2 and sys.argv[1] == "--read":
        urls = read_urls(max_rows=1000)
        print(f"从输入表 '{input_sheet_name()}' 的 '{input_url_column()}' 列读到 {len(urls)} 条 URL:")
        for u in urls[:10]:
            print("  -", u)
        if len(urls) > 10:
            print(f"  ... 共 {len(urls)} 条")
    else:
        print("用法:")
        print("  python lark_sheets.py --list-sheets   # 列出输入表 / 输出表的所有 sheet")
        print("  python lark_sheets.py --read           # 从输入表读 URL 列表")
