"""批量跑 TikTok 账号年龄/性别标注 v6（多机分片版）。

在 V6 中增加：
- 结构化证据输出：aging_signal_count, hard_triggers_45_plus, has_45plus_trigger 等字段
- 规则后处理：自动纠正 45+ 被低估、25-34 与衰老信号冲突等问题
- 边界复核器：对高风险样本进行二次判定，减少系统性偏差
- confidence 校准：发生规则纠偏时自动降级 confidence

底层调用 tiktok_age_workflow_v6.run()，飞书表结构扩展为 V6 共识字段：
Sheet1 的 profile_link 列，Sheet2 的 [Agent-Labeling]age/final_result/error/confidence 列。

用法:
    # 单机：
    python3 batch_run_v6.py --limit 20 --workers 4 --flush-every 10
    # 多机分片（一般由 dispatch.sh 通过环境变量注入 WORKER_ID/TOTAL_WORKERS）：
    WORKER_ID=0 TOTAL_WORKERS=2 python3 batch_run_v6.py --limit 0 --workers 12 --flush-every 20
    python3 batch_run_v6.py --skip-dedup --limit 5  # 即使 URL 已有结果也重新跑

    单机全参数
    python3 batch_run_v6.py \
    --token "MaTws7lJohae55t7TY8mSRaIyJb" \       # 飞书电子表格 token
    --sheet1 "Sheet1" \                           # 输入 sheet（profile_link 列所在）
    --sheet2 "Sheet2" \                           # 输出 sheet（结果写入）
    --limit 50 \                                  # 最多处理多少个账号（<=0 表示不限）
    --workers 4 \                                 # 并行进程数（默认 min(4, CPU核心数)）
    --flush-every 10 \                            # 每处理 N 条写回一次飞书
    --skip-dedup \                                # 跳过去重（即使已有结果也重新跑）
    --max-sheet1-rows 0 \                         # 最多从 Sheet1 读取多少行（<=0=用全局 MAX_SHEET_ROWS）
    --worker-id 0 \                               # 分片编号（单机通常为 0）
    --total-workers 1 \                           # 机器总数（单机为 1）
    --auto-clean-minutes 10                       # 每 N 分钟清理截图（<=0 关闭）
    --sheet1 "Sheet1"
    --sheet2 "link having gt r1 p4" 
"""

from __future__ import annotations

import argparse
import json
import hashlib
import os
import re
import sys
import time
import threading
from concurrent.futures import ProcessPoolExecutor
from multiprocessing import cpu_count
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from dotenv import load_dotenv  # noqa: E402
# override=True 确保 .env 里的值总是覆盖终端里已有的旧环境变量
load_dotenv(HERE / ".env", override=True)

import lark_sheets  # noqa: E402
import tiktok_age_workflow_v6 as tiktok_age_workflow_v6  # noqa: E402
import labeling_spec_v6 as spec_v3  # noqa: E402

# ========================
# 默认配置 (v3 与 v2 保持相同的 Sheet1/Sheet2 结构)
# ========================
# 兼容两套变量命名：优先 BATCH_*，回退到 INPUT/OUTPUT_*，再回退到硬编码默认
DEFAULT_SPREADSHEET_TOKEN = (
    os.getenv("BATCH_SPREADSHEET_TOKEN")
    or os.getenv("SPREADSHEET_TOKEN")
    or os.getenv("OUTPUT_SPREADSHEET_TOKEN")
    or os.getenv("INPUT_SPREADSHEET_TOKEN")
    or "MaTws7lJohae55t7TY8mSRaIyJb"
)
DEFAULT_SHEET1_NAME = (os.getenv("BATCH_SHEET1") or os.getenv("INPUT_SHEET_NAME") or "Sheet1")  # 输入 profile_link
DEFAULT_SHEET2_NAME = (os.getenv("BATCH_SHEET2") or os.getenv("OUTPUT_SHEET_NAME") or "Sheet2")  # 输出 + 人工标注

# Sheet2 中 agent 输出的列名
AGENT_AGE_COL = "[Agent-Labeling]age"
AGENT_FINAL_COL = "[Agent-Labeling]final_result"
AGENT_ERROR_COL = "[Agent-Labeling]error"
AGENT_CONFIDENCE_COL = "[Agent-Labeling]confidence"
AGENT_REASONING_COL = "[Agent-Labeling]reasoning"
AGENT_TIER1_COL = "[Agent-Labeling]tier1"
AGENT_TIER2_COL = "[Agent-Labeling]tier2"
AGENT_GENDER_COL = "[Agent-Labeling]gender"
AGENT_EVIDENCE_COL = "[Agent-Labeling]evidence"
# 多 agent 模式：可配的分层一致等级（整数；1=最严，0=未达成任何等级）
AGENT_TIER1_LEVEL_COL = "[Agent-Labeling]tier1_consensus_level"
AGENT_TIER2_LEVEL_COL = "[Agent-Labeling]tier2_consensus_level"

# 可选元数据列（从页面抓取的非 LLM 字段）
META_HANDLE_COL = "handle"
META_DISPLAY_NAME_COL = "display_name"
META_BIO_COL = "bio"
META_STATS_COL = "stats"


# 输出列预设
# profile_link 始终写入，不受配置影响
_OUTPUT_PRESETS: Dict[str, List[str]] = {
    # core: 保持 V6 原有行为，仅核心 4 列
    "core": ["age", "final_result", "confidence", "error"],
    # minimal: core + reasoning（便于人工快速 review）
    "minimal": ["age", "final_result", "confidence", "reasoning", "error"],
    # standard: minimal + tier1/tier2/gender（拆分项便于筛选/排序）
    "standard": ["age", "final_result", "confidence", "reasoning",
                 "tier1", "tier2", "gender", "error"],
    # full: 所有单值字段 + evidence（完整审计信息）
    "full": ["age", "final_result", "confidence", "reasoning",
             "tier1", "tier2", "gender", "evidence",
             "handle", "display_name", "bio", "stats", "error"],
}

# 单 agent 模式下，字段 key -> (列名, 结果中取值的 key)
_SINGLE_COL_MAP: Dict[str, Tuple[str, str]] = {
    "age":          (AGENT_AGE_COL,          "age"),
    "final_result": (AGENT_FINAL_COL,        "final_result"),
    "confidence":   (AGENT_CONFIDENCE_COL,   "confidence"),
    "reasoning":    (AGENT_REASONING_COL,    "reasoning"),
    "tier1":        (AGENT_TIER1_COL,        "tier1"),
    "tier2":        (AGENT_TIER2_COL,        "tier2"),
    "gender":       (AGENT_GENDER_COL,       "gender"),
    "evidence":     (AGENT_EVIDENCE_COL,     "evidence"),
    "handle":       (META_HANDLE_COL,        "handle"),
    "display_name": (META_DISPLAY_NAME_COL,  "display_name"),
    "bio":          (META_BIO_COL,           "bio"),
    "stats":        (META_STATS_COL,         "stats"),
    "error":        (AGENT_ERROR_COL,        "error"),
}

# 多 agent 模式下，每个 agent 可附加的字段（age/final_result/confidence 始终写）
_MULTI_AGENT_EXTRA_FIELDS = {"reasoning", "evidence"}


def _resolve_output_cols() -> Tuple[List[str], List[str]]:
    """解析 OUTPUT_COLS 环境变量，返回 (单agent字段列表, 多agent每agent额外字段列表)。

    OUTPUT_COLS 取值:
      - 预设名: core / minimal / standard / full
      - 逗号分隔自定义字段列表: age,final_result,confidence,reasoning
    未设置时默认 core（保持向后兼容）。
    profile_link 始终写入，不需要在列表中指定。
    """
    raw = (os.getenv("OUTPUT_COLS") or "core").strip().lower()
    if raw in _OUTPUT_PRESETS:
        single_fields = list(_OUTPUT_PRESETS[raw])
    else:
        single_fields = [s.strip() for s in raw.split(",") if s.strip()]
        # 过滤无效字段名
        single_fields = [f for f in single_fields if f in _SINGLE_COL_MAP]

    # 多 agent 模式下，共识等级列 + 每 agent 的 age/final_result/confidence 始终写，
    # 额外字段从 single_fields 中提取属于 _MULTI_AGENT_EXTRA_FIELDS 的子集
    multi_extra = [f for f in single_fields if f in _MULTI_AGENT_EXTRA_FIELDS]
    return single_fields, multi_extra


def _agent_names() -> List[str]:
    raw = os.getenv("AGENT_CONFIGS") or "agent_a,agent_b,agent_c"
    return [s.strip() for s in raw.split(",") if s.strip()]


def _agent_col_name(agent_name: str, field: str) -> str:
    return f"[Agent-Labeling]{agent_name}_{field}"


# ---------------------------
# 飞书读写工具
# ---------------------------
def _get_token() -> str:
    app_id = os.getenv("FEISHU_APP_ID")
    app_secret = os.getenv("FEISHU_APP_SECRET")
    if not (app_id and app_secret):
        raise RuntimeError("缺少 FEISHU_APP_ID / FEISHU_APP_SECRET，无法访问飞书")
    return lark_sheets.get_tenant_access_token(app_id, app_secret)


def _resolve_sheet_id(spreadsheet_token: str, sheet_name: str, bearer: str) -> str:
    resp = lark_sheets._get_json(
        f"/open-apis/sheets/v3/spreadsheets/{spreadsheet_token}/sheets/query",
        params={},
        token=bearer,
    )
    if resp.get("code") not in (0, None):
        raise RuntimeError(f"读取 sheet 列表失败: {resp.get('code')} {resp.get('msg')}")
    sheets = (resp.get("data") or {}).get("sheets") or []
    for s in sheets:
        title = (s.get("properties") or {}).get("title") or s.get("title")
        sid = s.get("sheet_id") or (s.get("properties") or {}).get("sheet_id")
        if title == sheet_name:
            return str(sid)
    titles = [(s.get("properties") or {}).get("title") or s.get("title") for s in sheets]
    raise RuntimeError(f"找不到名为 {sheet_name!r} 的 sheet，可用: {titles}")


def _read_rows_paginated(spreadsheet_token: str, sheet_id: str,
                         col_letter_start: str, col_letter_end: str,
                         bearer: str,
                         page_size: int = 500, max_pages: Optional[int] = None,
                         stop_on_empty_pages: int = 2) -> List[List[Any]]:
    """从第 2 行开始分页读取 {col_letter_start}:{col_letter_end} 的数据。

    遇到连续 stop_on_empty_pages 整页为空就停止，最多读 max_pages 页。
    max_pages 缺省时按全局 MAX_SHEET_ROWS 换算，保证读输出表去重 / 定位空行
    时不会被旧的 20000 行硬上限截断。
    不依赖飞书 API 返回的 rowCount（它经常=1）。
    """
    if max_pages is None or max_pages <= 0:
        max_pages = max(1, (lark_sheets.max_sheet_rows() + page_size - 1) // page_size)
    rows: List[List[Any]] = []
    start_row = 2
    empty_pages_in_a_row = 0
    for _ in range(max_pages):
        end_row = start_row + page_size - 1
        a1_range = f"{col_letter_start}{start_row}:{col_letter_end}{end_row}"
        page = _read_range_values(spreadsheet_token, sheet_id, a1_range, bearer)
        if not page:
            empty_pages_in_a_row += 1
            if empty_pages_in_a_row >= stop_on_empty_pages:
                break
            start_row = end_row + 1
            continue
        has_data = False
        for r in page:
            if r and any(v is not None and str(v).strip() for v in r):
                has_data = True
                break
        if not has_data:
            empty_pages_in_a_row += 1
            if empty_pages_in_a_row >= stop_on_empty_pages:
                break
            start_row = end_row + 1
            continue
        empty_pages_in_a_row = 0
        rows.extend(page)
        if len(page) < page_size:
            break
        start_row = end_row + 1
    return rows


def _read_range_values(spreadsheet_token: str, sheet_id: str, a1_range: str, bearer: str) -> List[List[Any]]:
    """返回二维数组 values，行从 1 开始。"""
    resp = lark_sheets._get_json(
        f"/open-apis/sheets/v2/spreadsheets/{spreadsheet_token}/values/{sheet_id}!{a1_range}",
        params={},
        token=bearer,
    )
    if resp.get("code") not in (0, None):
        raise RuntimeError(f"读取 {a1_range} 失败: {resp.get('code')} {resp.get('msg')}")
    return (resp.get("data") or {}).get("valueRange", {}).get("values") or []


def _write_cell(spreadsheet_token: str, sheet_id: str, cell_a1: str, value: str, bearer: str) -> None:
    resp = lark_sheets._put_json(
        f"/open-apis/sheets/v2/spreadsheets/{spreadsheet_token}/values",
        body={"valueRange": {"range": f"{sheet_id}!{cell_a1}:{cell_a1}", "values": [[value]]}},
        params={"valueInputOption": "UserEntered"},
        token=bearer,
    )
    if resp.get("code") not in (0, None):
        raise RuntimeError(f"写入 {cell_a1} 失败: {resp.get('code')} {resp.get('msg')}")


def _write_row_cells(spreadsheet_token: str, sheet_id: str,
                     row_n: int, col_vals: Dict[int, str], bearer: str) -> None:
    """把同一行的多个单元格写回，连续列合并成单次 PUT（减少请求数）。"""
    if not col_vals:
        return
    cols = sorted(col_vals)
    # 把连续的列索引切成段
    segments: List[List[int]] = []
    for c in cols:
        if segments and c == segments[-1][-1] + 1:
            segments[-1].append(c)
        else:
            segments.append([c])
    for seg in segments:
        start, end = seg[0], seg[-1]
        a1 = f"{_col_letter(start)}{row_n}:{_col_letter(end)}{row_n}"
        values = [[col_vals[c] for c in seg]]
        resp = lark_sheets._put_json(
            f"/open-apis/sheets/v2/spreadsheets/{spreadsheet_token}/values",
            body={"valueRange": {"range": f"{sheet_id}!{a1}", "values": values}},
            params={"valueInputOption": "UserEntered"},
            token=bearer,
        )
        if resp.get("code") not in (0, None):
            raise RuntimeError(f"写入 {a1} 失败: {resp.get('code')} {resp.get('msg')}")


def _col_letter(col_idx: int) -> str:
    """0-indexed -> A1 列字母。"""
    s: List[str] = []
    n = col_idx
    while True:
        s.append(chr(65 + (n % 26)))
        n = n // 26 - 1
        if n < 0:
            break
    return "".join(reversed(s))


def _normalize_url_key(val: Any) -> str:
    """把任意单元格 URL 规范化成用于匹配的 key: 'tiktok.com/@handle'。"""
    if val is None:
        return ""
    # dict/list 形式（飞书 URL 单元格）
    if isinstance(val, dict):
        s = (val.get("text") or val.get("link") or "").strip()
    elif isinstance(val, list) and val and isinstance(val[0], dict):
        s = (val[0].get("text") or val[0].get("link") or "").strip()
    else:
        s = str(val).strip()
    if not s:
        return ""
    # 去掉协议前缀与 www.
    s2 = s.lower()
    for prefix in ("https://", "http://"):
        if s2.startswith(prefix):
            s = s[len(prefix):]
            s2 = s2[len(prefix):]
    if s2.startswith("www."):
        s = s[4:]
    # 去掉结尾 /
    s = s.rstrip("/")
    # 保留原始大小写（handle 大小写无关紧要）
    return s or ""


def _shard_index(url: str, total: int) -> int:
    """对 URL 做稳定哈希，返回它归属的分片编号 [0, total)。

    用 md5(规范化URL) 而非内置 hash()，保证跨进程/跨机器结果一致。
    """
    key = _normalize_url_key(url) or url
    h = hashlib.md5(key.encode("utf-8")).hexdigest()
    return int(h, 16) % total


def _filter_shard(urls: List[str], worker_id: int, total: int) -> List[str]:
    """只保留属于本分片的 URL。total<=1 时不分片，返回全部。"""
    if total <= 1:
        return urls
    return [u for u in urls if _shard_index(u, total) == worker_id]


# ---------------------------
# 读取 Sheet1 (profile_link 列表)
# ---------------------------
def read_sheet1_profile_links(spreadsheet_token: str, sheet1_name: str,
                               max_rows: Optional[int] = None) -> List[str]:
    bearer = _get_token()
    sheet_id = _resolve_sheet_id(spreadsheet_token, sheet1_name, bearer)

    if max_rows is None or max_rows <= 0:
        max_rows = lark_sheets.max_sheet_rows()

    # 先只读第 1 行表头，找 profile_link 列
    row1 = _read_range_values(spreadsheet_token, sheet_id, "A1:ZZ1", bearer)
    if not row1:
        raise RuntimeError(f"Sheet1 '{sheet1_name}' 为空")
    header = [str(v).strip() for v in row1[0]]

    link_col = None
    for i, h in enumerate(header):
        h_norm = h.lower()
        if "profile_link" in h_norm or "profile link" in h_norm or h_norm == "url" or "tiktok" in h_norm:
            link_col = i
            break
    if link_col is None:
        raise RuntimeError(f"Sheet1 '{sheet1_name}' 第 1 行找不到 profile_link/URL 列，实际表头: {header}")

    # 只读那一列，从第 2 行开始；分页读，连续空页就停，不超过 max_rows
    letter = _col_letter(link_col)
    max_pages = max(1, (max_rows + 499) // 500)
    rows = _read_rows_paginated(spreadsheet_token, sheet_id, letter, letter, bearer,
                                page_size=500, max_pages=max_pages)
    urls: List[str] = []
    seen_keys: set[str] = set()
    for row in rows:
        if not row:
            continue
        v = row[0]
        if isinstance(v, dict):
            s = (v.get("text") or v.get("link") or "").strip()
        elif isinstance(v, list) and v and isinstance(v[0], dict):
            s = (v[0].get("text") or v[0].get("link") or "").strip()
        elif isinstance(v, str):
            s = v.strip()
        else:
            s = str(v).strip() if v else ""
        key = _normalize_url_key(s)
        if key and key not in seen_keys:
            seen_keys.add(key)
            urls.append(s)
    print(f"  [sheet1] 在第 {link_col + 1} 列 ({letter}) 读到 {len(urls)} 个 profile_link")
    return urls


# ---------------------------
# 读取 Sheet2 已有结果（用于去重）
# ---------------------------
def read_sheet2_rows(spreadsheet_token: str, sheet2_name: str) -> List[Dict[str, Any]]:
    """返回 [{profile_link:..., agent_age:..., agent_final:..., labeling_final:...}] 的列表，用于去重与一致性计算。"""
    bearer = _get_token()
    sheet_id = _resolve_sheet_id(spreadsheet_token, sheet2_name, bearer)

    row1 = _read_range_values(spreadsheet_token, sheet_id, "A1:ZZ1", bearer)
    if not row1:
        raise RuntimeError(f"Sheet2 '{sheet2_name}' 为空")
    header = [str(v).strip() for v in row1[0]]
    # 保留中间空列的位置；只能裁掉尾部空列，否则 header 索引会与数据行错位。
    while header and not header[-1]:
        header.pop()

    def _find_col(*keywords) -> Optional[int]:
        for i, h in enumerate(header):
            for kw in keywords:
                if kw.lower() in h.lower():
                    return i
        return None

    def _find_exact(title: str) -> Optional[int]:
        title = title.lower()
        for i, h in enumerate(header):
            if h.lower() == title:
                return i
        return None

    profile_link_col = _find_col("profile_link")
    if profile_link_col is None:
        profile_link_col = _find_col("url", "handle")
    if profile_link_col is None:
        raise RuntimeError(f"Sheet2 '{sheet2_name}' 第 1 行找不到 profile_link 列")
    age_col = _find_exact(AGENT_AGE_COL)
    final_col = _find_exact(AGENT_FINAL_COL)
    agent_result_cols = [
        (_find_exact(_agent_col_name(name, "age")),
         _find_exact(_agent_col_name(name, "final_result")))
        for name in _agent_names()
    ]
    labeling_final_col = _find_col("[Labeling]final_result")
    if labeling_final_col is None:
        labeling_final_col = _find_col("[Labeling] age")

    # 逐列读（按关键列的最大索引 + 1），避免整表读取 10MB
    result_cols = [i for pair in agent_result_cols for i in pair if i is not None]
    max_col = max([i for i in [profile_link_col, age_col, final_col, labeling_final_col]
                   if i is not None] + result_cols)
    rows = _read_rows_paginated(spreadsheet_token, sheet_id, "A", _col_letter(max_col), bearer)

    results: List[Dict[str, Any]] = []
    for row in rows:
        def _cell(idx: int) -> Any:
            if idx is None or idx >= len(row):
                return None
            return row[idx]

        pl_raw = _cell(profile_link_col)
        pl_key = _normalize_url_key(pl_raw)
        if not pl_key:
            continue
        agent_age = _cell(age_col)
        agent_final = _cell(final_col)
        # 只要任一 Agent 已有结果，就视为该 URL 已处理。
        for age_idx, final_idx in agent_result_cols:
            if not agent_age and age_idx is not None:
                agent_age = _cell(age_idx)
            if not agent_final and final_idx is not None:
                agent_final = _cell(final_idx)
        results.append({
            "profile_link": pl_key,
            "agent_age": agent_age,
            "agent_final": agent_final,
            "labeling_final": _cell(labeling_final_col),
        })
    print(f"  [sheet2] 读到 {len(results)} 行已有数据")
    return results


# ---------------------------
# 单账号 worker
# ---------------------------
def _process_one(url: str) -> Dict[str, Any]:
    try:
        return tiktok_age_workflow_v6.run(url, headless=True, retry=1)
    except Exception as exc:  # noqa: BLE001
        err_msg = f"{type(exc).__name__}: {exc}"[:500]
        # 把子进程中的错误直接打印到 stderr，避免被吞掉
        print(f"  [worker ERROR] {url} -> {err_msg}", file=sys.stderr, flush=True)
        if len(_agent_names()) == 1:
            # 单 agent：错误结构与 V5 完全一致
            return {
                "url": url,
                "age": ["error"],
                "final_result": {"age": ["error"], "gender": "error"},
                "tier1": "error",
                "tier2": None,
                "gender": "error",
                "error": err_msg,
            }
        return {
            "url": url,
            "tier1_consensus_level": 0,
            "tier2_consensus_level": 0,
            "agent_results": {},
            "error": err_msg,
        }


# ---------------------------
# flush: 批量写回 Sheet2
# ---------------------------
def _flush(results: List[Dict[str, Any]],
           spreadsheet_token: str, sheet2_name: str,
           bearer: str, sheet_id: str) -> None:
    if not results:
        return

    bearer = _get_token()

    row1 = _read_range_values(spreadsheet_token, sheet_id, "A1:ZZ1", bearer)
    if not row1:
        header = []
    else:
        header = [str(v).strip() if v else "" for v in row1[0]]
        # 去掉尾部空列：飞书 API 返回 A1:ZZ1 会包含大量尾部空字符串，
        # 导致 len(header) 固定为 702，新增列被写到极右侧（AAA 列附近）。
        while header and not header[-1]:
            header.pop()

    single_mode = len(_agent_names()) == 1
    single_fields, multi_extra_fields = _resolve_output_cols()

    if single_mode:
        required_headers = ["profile_link"]
        for f in single_fields:
            if f in _SINGLE_COL_MAP:
                required_headers.append(_SINGLE_COL_MAP[f][0])
    else:
        required_headers = [
            "profile_link",
            AGENT_TIER1_LEVEL_COL,
            AGENT_TIER2_LEVEL_COL,
        ]
        for an in _agent_names():
            required_headers.extend([
                _agent_col_name(an, "age"),
                _agent_col_name(an, "final_result"),
                _agent_col_name(an, "confidence"),
            ])
            for ef in multi_extra_fields:
                col_suffix = _SINGLE_COL_MAP[ef][0].replace("[Agent-Labeling]", "")
                required_headers.append(_agent_col_name(an, col_suffix))
        required_headers.append(AGENT_ERROR_COL)

    col_indices: Dict[str, int] = {}
    for title in required_headers:
        found = False
        for i, h in enumerate(header):
            if h.lower() == title.lower():
                col_indices[title] = i
                found = True
                break
        if not found:
            new_idx = len(header)
            col_indices[title] = new_idx
            header.append(title)
            letter = _col_letter(new_idx)
            try:
                _write_cell(spreadsheet_token, sheet_id, f"{letter}1", title, bearer)
                print(f"    flush: Sheet2 新增列 {letter} = {title}")
            except Exception as exc:
                print(f"    flush: 新增列失败 {exc}")

    pl_col = col_indices["profile_link"]
    pl_letter = _col_letter(pl_col)
    existing_rows = _read_rows_paginated(spreadsheet_token, sheet_id,
                                         pl_letter, pl_letter, bearer)
    url_to_row1: Dict[str, int] = {}
    max_used_row1: int = 1
    for i, row in enumerate(existing_rows):
        if not row:
            continue
        v = row[0]
        key = _normalize_url_key(v)
        if key:
            sheet_row = i + 2
            url_to_row1[key] = sheet_row
            if sheet_row > max_used_row1:
                max_used_row1 = sheet_row

    written = 0
    for r in results:
        url = r.get("url") or r.get("profile_link") or ""
        if not url:
            continue
        url_key = _normalize_url_key(url)
        if not url_key:
            continue
        existing_row = url_to_row1.get(url_key)

        def _as_str(val) -> str:
            if val is None:
                return ""
            if isinstance(val, (list, dict)):
                return json.dumps(val, ensure_ascii=False)
            return str(val)

        if single_mode:
            cells: Dict[int, str] = {}
            for f in single_fields:
                if f not in _SINGLE_COL_MAP:
                    continue
                col_name, result_key = _SINGLE_COL_MAP[f]
                if col_name not in col_indices:
                    continue
                if f == "age":
                    val = json.dumps(
                        r.get("age") or r.get("final_result", {}).get("age", ["Unknown"]),
                        ensure_ascii=False,
                    )
                elif f == "final_result":
                    val = json.dumps(r.get("final_result") or {
                        "age": r.get("age", ["Unknown"]),
                        "gender": r.get("gender", "unknown"),
                    }, ensure_ascii=False)
                else:
                    val = _as_str(r.get(result_key))
                cells[col_indices[col_name]] = val
        else:
            tier1_level_val = _as_str(r.get("tier1_consensus_level", 0))
            tier2_level_val = _as_str(r.get("tier2_consensus_level", 0))
            err_val = _as_str(r.get("error")) or ""

            cells = {
                col_indices[AGENT_TIER1_LEVEL_COL]: tier1_level_val,
                col_indices[AGENT_TIER2_LEVEL_COL]: tier2_level_val,
                col_indices[AGENT_ERROR_COL]: err_val,
            }

            agent_results_data = r.get("agent_results") or {}
            agent_votes_raw = r.get("agent_votes_raw") or {}
            for an in _agent_names():
                ar = agent_results_data.get(an) or {}
                cells[col_indices[_agent_col_name(an, "age")]] = _as_str(ar.get("age"))
                cells[col_indices[_agent_col_name(an, "final_result")]] = _as_str(ar.get("final_result"))
                cells[col_indices[_agent_col_name(an, "confidence")]] = _as_str(ar.get("confidence"))
                # 额外字段（reasoning/evidence）从 agent_votes_raw 里取
                vote = agent_votes_raw.get(an) or {}
                for ef in multi_extra_fields:
                    col_suffix = _SINGLE_COL_MAP[ef][0].replace("[Agent-Labeling]", "")
                    col_key = _agent_col_name(an, col_suffix)
                    if col_key in col_indices:
                        cells[col_indices[col_key]] = _as_str(vote.get(ef))

        if existing_row:
            row_n = existing_row
            _write_row_cells(spreadsheet_token, sheet_id, row_n, cells, bearer)
        else:
            max_used_row1 += 1
            new_row_n = max_used_row1
            new_cells = {pl_col: url}
            new_cells.update(cells)
            _write_row_cells(spreadsheet_token, sheet_id, new_row_n, new_cells, bearer)
            # 同一批次后续结果应命中刚写入的行，避免重复 URL 产生重复记录。
            url_to_row1[url_key] = new_row_n
        written += 1
    print(f"  [flush] 已写回 {written} 条到 Sheet2 '{sheet2_name}' (output_cols={single_fields if single_mode else 'multi'})")


def _start_screenshot_cleaner(interval_minutes: int = 10,
                               min_age_minutes: int = 5) -> threading.Thread:
    """启动守护线程：每 interval_minutes 分钟清理 screenshots/*.png。

    - 只删除修改时间在 min_age_minutes 之前的图片，避免误删正在写的新文件。
    - 守护线程随主进程结束自动退出，不留孤儿进程。
    - 失败时静默，不影响主任务。
    """
    screenshot_dir = HERE / "screenshots"
    min_age_sec = min_age_minutes * 60
    interval_sec = max(1, int(interval_minutes) * 60)

    def _loop() -> None:
        while True:
            try:
                time.sleep(interval_sec)
                if not screenshot_dir.exists():
                    continue
                now = time.time()
                deleted = 0
                saved_bytes = 0
                for f in screenshot_dir.iterdir():
                    try:
                        if not f.is_file():
                            continue
                        if f.suffix.lower() != ".png":
                            continue
                        # 只删老图片，避开正在写入的
                        mtime = f.stat().st_mtime
                        if now - mtime < min_age_sec:
                            continue
                        size = f.stat().st_size
                        f.unlink()
                        deleted += 1
                        saved_bytes += size
                    except Exception:
                        continue
                if deleted > 0:
                    mb = saved_bytes / 1024 / 1024
                    print(f"  [auto-clean] 已删除 {deleted} 张老截图 (约 {mb:.1f} MB)")
            except Exception:
                # 任何异常都不影响主线程，继续下一轮
                continue

    t = threading.Thread(target=_loop, name="screenshot-cleaner", daemon=True)
    t.start()
    return t


# ---------------------------
# main
# ---------------------------
def main() -> int:
    parser = argparse.ArgumentParser(description="批量 TikTok 账号标注 v6（多机分片）")
    parser.add_argument("--token", default=DEFAULT_SPREADSHEET_TOKEN,
                        help="飞书电子表格 token（默认读取 .env 或 MaTws7lJohae55t7TY8mSRaIyJb）")
    parser.add_argument("--sheet1", default=DEFAULT_SHEET1_NAME, help="输入 profile_link 的 sheet 名，默认 Sheet1")
    parser.add_argument("--sheet2", default=DEFAULT_SHEET2_NAME, help="输出 + 人工标注的 sheet 名，默认 Sheet2")
    parser.add_argument("--limit", type=int, default=50, help="最多处理多少个账号；<=0 表示不限制（处理本分片全部）")
    parser.add_argument("--workers", type=int, default=min(4, max(1, cpu_count())), help="并行进程数")
    parser.add_argument("--flush-every", type=int, default=10, help="每处理 N 条写回一次")
    parser.add_argument("--skip-dedup", action="store_true", help="即使 Sheet2 里已有结果也重新跑")
    parser.add_argument("--max-sheet1-rows", type=int, default=0,
                        help="最多从 Sheet1 读取多少行 profile_link；<=0 表示用全局 "
                             "MAX_SHEET_ROWS（.env 配置，默认 200000）")
    parser.add_argument("--worker-id", type=int, default=int(os.getenv("WORKER_ID", "0")),
                        help="本机分片编号，从 0 开始（多机静态分片）。默认读 WORKER_ID 环境变量")
    parser.add_argument("--total-workers", type=int, default=int(os.getenv("TOTAL_WORKERS", "1")),
                        help="参与分片的机器总数 N（多机静态分片）。默认读 TOTAL_WORKERS 环境变量")
    parser.add_argument("--auto-clean-minutes", type=int, default=10,
                        help="每 N 分钟清理 screenshots/*.png，<=0 关闭。默认 10")
    args = parser.parse_args()

    # 校验分片参数
    if args.total_workers < 1:
        print(f"--total-workers 必须 >=1，当前 {args.total_workers}")
        return 2
    if not (0 <= args.worker_id < args.total_workers):
        print(f"--worker-id 必须在 [0, {args.total_workers}) 内，当前 {args.worker_id}")
        return 2

    print(f"[v6] spreadsheet_token = {args.token}")
    print(f"[v6] sheet1 = {args.sheet1}   sheet2 = {args.sheet2}")
    print(f"[v6] LLM_PROVIDER = {os.getenv('LLM_PROVIDER', '(未设置，走默认 openai 兼容)')}")
    print(f"[v6] 分片: worker-id={args.worker_id} / total-workers={args.total_workers}"
          f"  WORKER_TAG={os.getenv('WORKER_TAG', '(无)')}")

    # 1) 从 Sheet1 读 profile_link 列表
    urls = read_sheet1_profile_links(args.token, args.sheet1, max_rows=args.max_sheet1_rows)
    if not urls:
        print("Sheet1 没有 profile_link，请先填好。")
        return 2

    # 1.5) 静态分片：只保留属于本机的 URL（在去重之前做，保证分片归属稳定）
    if args.total_workers > 1:
        before = len(urls)
        urls = _filter_shard(urls, args.worker_id, args.total_workers)
        print(f"  分片: {before} -> {len(urls)} "
              f"(本机 worker {args.worker_id}/{args.total_workers} 负责的 URL)")

    # 2) 从 Sheet2 读已有结果用于去重
    sheet2_rows = []
    try:
        sheet2_rows = read_sheet2_rows(args.token, args.sheet2)
    except Exception as exc:
        print(f"  读取 Sheet2 失败，将以空表处理: {exc}")

    existing_urls = {r["profile_link"] for r in sheet2_rows if r.get("agent_age") or r.get("agent_final")}
    if not args.skip_dedup:
        before = len(urls)
        # 用规范化 key 做匹配，避免 https/http/www 差异导致重复跑
        urls = [u for u in urls if _normalize_url_key(u) not in existing_urls]
        print(f"  去重: {before} -> {len(urls)} (跳过 {before - len(urls)} 个已处理 URL)")

    if args.limit > 0:
        urls = urls[: args.limit]
    if not urls:
        print("没有需要处理的 URL。")
        return 0

    print(f"  将要处理 {len(urls)} 个 URL，并发 {args.workers}，每 {args.flush_every} 条写回一次。\n")

    # 2.5) 启动截图自动清理线程（每 N 分钟清一次，只删已落盘超过 5 分钟的图片）
    if args.auto_clean_minutes > 0:
        _start_screenshot_cleaner(interval_minutes=args.auto_clean_minutes)
        print(f"  [auto-clean] 每 {args.auto_clean_minutes} 分钟清理 screenshots/*.png")

    # 3) 并发跑
    bearer = _get_token()
    sheet_id = _resolve_sheet_id(args.token, args.sheet2, bearer)

    results_batch: List[Dict[str, Any]] = []
    failed_results: List[Dict[str, Any]] = []  # flush 失败的结果，末尾统一重试
    ok_count = 0
    err_count = 0
    start = time.time()
    single_mode = len(_agent_names()) == 1

    with ProcessPoolExecutor(max_workers=args.workers) as pool:
        for idx, result in enumerate(pool.map(_process_one, urls), 1):
            has_err = bool(result.get("error"))
            if has_err:
                err_count += 1
            else:
                ok_count += 1
            if single_mode:
                age = result.get("age") or ["Unknown"]
                gender = result.get("gender") or "unknown"
                detail = f"age={age} gender={gender}"
            else:
                detail = (f"t1L={result.get('tier1_consensus_level', 0)} "
                          f"t2L={result.get('tier2_consensus_level', 0)}")
            print(f"[{idx}/{len(urls)}] {result.get('url')} -> {detail}"
                  + (f" [err: {result['error'][:80]}]" if has_err else "") + f" [OK:{ok_count} err:{err_count}]")
            results_batch.append(result)

            if args.flush_every > 0 and (len(results_batch) % args.flush_every == 0):
                try:
                    _flush(results_batch, args.token, args.sheet2, bearer, sheet_id)
                except Exception as exc:
                    # 不再把失败批次留在 results_batch 里累积，转入 failed_results 末尾重试
                    failed_results.extend(results_batch)
                    print(f"  ⚠️  flush 失败（{len(results_batch)} 条转入末尾重试）: {exc}",
                          file=sys.stderr, flush=True)
                finally:
                    results_batch = []

    # 最后一次 flush（当前批次 + 之前失败累积的）
    remaining = results_batch + failed_results
    if remaining:
        try:
            _flush(remaining, args.token, args.sheet2, bearer, sheet_id)
        except Exception as exc:
            print(f"  ⚠️  final flush 失败！{len(remaining)} 条未能写回 Sheet2: {exc}\n"
                  f"     结果已落盘在 screenshots/*_v6.json，可运行 "
                  f"`python3 recover_v6.py --token {args.token}` 从磁盘恢复写回。",
                  file=sys.stderr, flush=True)

    elapsed = int(time.time() - start)
    print(f"\n== 完成: ok={ok_count}, err={err_count}, 用时 {elapsed} 秒 ==")
    if len(_agent_names()) == 1:
        print(f"结果已写入 Sheet2 '{args.sheet2}' 列: {AGENT_AGE_COL} / {AGENT_FINAL_COL} / "
              f"{AGENT_CONFIDENCE_COL} / {AGENT_ERROR_COL}")
    else:
        agent_cols = " / ".join(
            f"{_agent_col_name(an, 'age')},{_agent_col_name(an, 'final_result')},{_agent_col_name(an, 'confidence')}"
            for an in _agent_names()
        )
        print(f"结果已写入 Sheet2 '{args.sheet2}' 列: {AGENT_TIER1_LEVEL_COL} / "
              f"{AGENT_TIER2_LEVEL_COL} / {agent_cols} / {AGENT_ERROR_COL}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
