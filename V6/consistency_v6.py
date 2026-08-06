"""一致性计算 v6：对比人工标注 vs agent 标注。

- 规范化走 labeling_spec_v6.normalize_* 系列函数（保证 agent 与人工使用同一份规范）
- 解析使用 labeling_spec_v6.parse_age_tag / parse_final_result_dict

用法:
    python3 consistency_v6.py
    python3 consistency_v6.py --token MaTws7lJohae55t7TY8mSRaIyJb --sheet Sheet2
    python3 consistency_v6.py --json-out /tmp/detail.json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from dotenv import load_dotenv  # noqa: E402
load_dotenv(HERE / ".env", override=True)

import lark_sheets  # noqa: E402
import labeling_spec_v6 as spec_v3  # noqa: E402

SPEC = spec_v3.load_spec()

DEFAULT_TOKEN = (
    os.getenv("BATCH_SPREADSHEET_TOKEN")
    or os.getenv("SPREADSHEET_TOKEN")
    or os.getenv("OUTPUT_SPREADSHEET_TOKEN")
    or os.getenv("INPUT_SPREADSHEET_TOKEN")
    or "MaTws7lJohae55t7TY8mSRaIyJb"
)
DEFAULT_SHEET = os.getenv("BATCH_SHEET2") or os.getenv("OUTPUT_SHEET_NAME") or "Sheet2"

HUMAN_AGE_COL = "[Labeling]age"
HUMAN_FINAL_COL = "[Labeling]final_result"
AGENT_AGE_COL = "[Agent-Labeling]age"
AGENT_FINAL_COL = "[Agent-Labeling]final_result"
AGENT_ERROR_COL = "[Agent-Labeling]error"
AGENT_CONSENSUS_LEVEL_COL = "[Agent-Labeling]consensus_level"
AGENT_ACCEPTED_GT_COL = "[Agent-Labeling]accepted_gt"
AGENT_TIER1_CONSENSUS_LEVEL_COL = "[Agent-Labeling]tier1_consensus_level"
AGENT_TIER2_CONSENSUS_LEVEL_COL = "[Agent-Labeling]tier2_consensus_level"
AGENT_TIER1_ACCEPTED_GT_COL = "[Agent-Labeling]tier1_accepted_gt"
PROFILE_LINK_COL = "profile_link"


# ---------------------------
# 飞书读取
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


def _read_range_values(spreadsheet_token: str, sheet_id: str, a1_range: str, bearer: str) -> List[List[Any]]:
    resp = lark_sheets._get_json(
        f"/open-apis/sheets/v2/spreadsheets/{spreadsheet_token}/values/{sheet_id}!{a1_range}",
        token=bearer,
    )
    if resp.get("code") not in (0, None):
        raise RuntimeError(f"读取 {a1_range} 失败: {resp.get('code')} {resp.get('msg')}")
    return (resp.get("data") or {}).get("valueRange", {}).get("values") or []


def _read_rows_paginated(spreadsheet_token: str, sheet_id: str,
                         col_letter_start: str, col_letter_end: str,
                         bearer: str,
                         page_size: int = 500, max_pages: Optional[int] = None,
                         stop_on_empty_pages: int = 2) -> List[List[Any]]:
    """从第 2 行开始分页读取 {col_letter_start}:{col_letter_end} 的数据。

    遇到连续 stop_on_empty_pages 整页为空就停止，最多读 max_pages 页。
    max_pages 缺省时按全局 MAX_SHEET_ROWS 换算，避免旧的 20000 行硬上限。
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
        empty_pages_in_a_row = 0
        # 飞书可能把末尾的空行截掉，也可能保留。把真正的空行（全 None/空）也算进去，
        # 避免把 page 里只因为有一列有值而保留的稀疏行误判为"有数据"。
        rows.extend(page)
        if len(page) < page_size:
            # 这页没装满，说明飞书这边已经读到末尾了
            break
        start_row = end_row + 1
    return rows


def _col_letter(col_idx: int) -> str:
    s: List[str] = []
    n = col_idx
    while True:
        s.append(chr(65 + (n % 26)))
        n = n // 26 - 1
        if n < 0:
            break
    return "".join(reversed(s))


# ---------------------------
# 解析一行单元格值 -> (tier1, tier2, gender)
# ---------------------------
def _parse_any(val: Any) -> Tuple[List[str], str]:
    """解析 age+gender。返回 (age_list, gender_str)。age_list 形如 ['25-44','35-44'] 或 ['Unknown']。"""
    if val is None:
        return [], ""
    # dict: 可能是飞书返回的 {text:..., link:...} 也可能是最终结果 JSON
    if isinstance(val, dict):
        # 优先把它当作 final_result 的 dict
        t1, t2, g = spec_v3.parse_final_result_dict(val, SPEC)
        if t1 == SPEC["age"]["unknown_label"] and t2 is None and g == "unknown":
            # 可能只是 "text" 单元格 — 回退为字符串解析
            text = val.get("text") or val.get("link") or ""
            if text:
                t1, t2 = spec_v3.parse_age_tag(text, SPEC)
                return ([t1] + ([t2] if t2 else []), "unknown")
        age = [t1] + ([t2] if t2 else [])
        return age, g
    # list
    if isinstance(val, list):
        if val and isinstance(val[0], dict):
            # 飞书可能返回 [{"text":"..."}]
            first = val[0]
            t = first.get("text") or first.get("link") or ""
            if not t:
                return [], ""
            # 可能是 JSON 串
            t1, t2 = spec_v3.parse_age_tag(t, SPEC)
            return ([t1] + ([t2] if t2 else []), "unknown")
        # 普通 list（如 ["25-44","35-44"]）
        t1, t2 = spec_v3.parse_age_tag(val, SPEC)
        return ([t1] + ([t2] if t2 else []), "unknown")
    # str
    if isinstance(val, str):
        s = val.strip()
        if not s:
            return [], ""
        # 尝试解析为 JSON
        obj: Any = None
        try:
            obj = json.loads(s)
        except json.JSONDecodeError:
            obj = None
        if obj is not None:
            if isinstance(obj, dict) and ("age" in obj or "gender" in obj):
                t1, t2, g = spec_v3.parse_final_result_dict(obj, SPEC)
                return ([t1] + ([t2] if t2 else []), g)
            if isinstance(obj, list):
                t1, t2 = spec_v3.parse_age_tag(obj, SPEC)
                return ([t1] + ([t2] if t2 else []), "unknown")
        # 当作纯文本 age 标签
        t1, t2 = spec_v3.parse_age_tag(s, SPEC)
        return ([t1] + ([t2] if t2 else []), "unknown")
    # 其他类型（数字等）
    t1, t2 = spec_v3.parse_age_tag(str(val), SPEC)
    return ([t1] + ([t2] if t2 else []), "unknown")


def _cell_to_age_gender(row: List[Any],
                        profile_link_col: int,
                        age_col: Optional[int],
                        final_col: Optional[int]) -> Tuple[List[str], str, str]:
    """返回 (age_list, gender, profile_link)。"""
    def _v(idx: Optional[int]) -> Any:
        if idx is None or idx >= len(row):
            return None
        return row[idx]

    profile_link = _v(profile_link_col)
    if isinstance(profile_link, dict):
        profile_link = profile_link.get("text") or profile_link.get("link") or ""
    elif isinstance(profile_link, list) and profile_link and isinstance(profile_link[0], dict):
        profile_link = profile_link[0].get("text") or profile_link[0].get("link") or ""
    else:
        profile_link = str(profile_link) if profile_link is not None else ""

    age_list1, gender1 = _parse_any(_v(age_col)) if age_col is not None else ([], "")
    age_list2, gender2 = _parse_any(_v(final_col)) if final_col is not None else ([], "")

    # final_result 优先（它含 gender），否则退化用 age 列（gender 未知）
    if age_list2:
        return age_list2, gender2 or gender1 or "unknown", profile_link
    return age_list1, gender1 or "unknown", profile_link


# ---------------------------
# main
# ---------------------------
def main() -> int:
    parser = argparse.ArgumentParser(description="一致性计算 v3")
    parser.add_argument("--token", default=DEFAULT_TOKEN)
    parser.add_argument("--sheet", default=DEFAULT_SHEET,
                        help="人工 + agent 结果所在 sheet，默认 Sheet2")
    parser.add_argument("--json-out", default=None,
                        help="把每条样本的明细写进这个 JSON 文件")
    args = parser.parse_args()

    bearer = _get_token()
    sheet_id = _resolve_sheet_id(args.token, args.sheet, bearer)

    print(f"[读表] token={args.token} sheet={args.sheet}")

    row1 = _read_range_values(args.token, sheet_id, "A1:ZZ1", bearer)
    if not row1:
        print("表为空。")
        return 2
    header = [str(v).strip() for v in row1[0]]
    print(f"       表头: {header[:30]}")

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
    human_age_col = _find_col(HUMAN_AGE_COL)
    human_final_col = _find_col(HUMAN_FINAL_COL)
    agent_age_col = _find_col(AGENT_AGE_COL)
    agent_final_col = _find_col(AGENT_FINAL_COL)
    agent_error_col = _find_col(AGENT_ERROR_COL)
    consensus_level_col = _find_col(AGENT_CONSENSUS_LEVEL_COL)
    accepted_gt_col = _find_col(AGENT_ACCEPTED_GT_COL)
    tier1_consensus_level_col = _find_col(AGENT_TIER1_CONSENSUS_LEVEL_COL)
    tier2_consensus_level_col = _find_col(AGENT_TIER2_CONSENSUS_LEVEL_COL)
    tier1_accepted_gt_col = _find_col(AGENT_TIER1_ACCEPTED_GT_COL)

    agent_names = [
        name.strip() for name in (os.getenv("AGENT_CONFIGS") or "").split(",")
        if name.strip()
    ]
    agent_columns = {
        name: (
            _find_exact(f"[Agent-Labeling]{name}_age"),
            _find_exact(f"[Agent-Labeling]{name}_final_result"),
        )
        for name in agent_names
    }
    multi_agent_mode = any(age is not None or final is not None
                           for age, final in agent_columns.values())

    if profile_link_col is None:
        print(f"找不到 profile_link 列。表头: {header[:30]}")
        return 2

    # 逐列读取，避免整表读；分页读，连续空页就停
    agent_result_cols = [
        i for pair in agent_columns.values() for i in pair if i is not None
    ]
    all_cols_to_read = [i for i in [profile_link_col, human_age_col, human_final_col,
                                    agent_age_col, agent_final_col, agent_error_col,
                                    consensus_level_col, accepted_gt_col,
                                    tier1_consensus_level_col, tier2_consensus_level_col,
                                    tier1_accepted_gt_col]
                        if i is not None] + agent_result_cols
    max_col = max(all_cols_to_read)

    rows = _read_rows_paginated(args.token, sheet_id, "A", _col_letter(max_col), bearer,
                                page_size=500)
    print(f"       共 {len(rows)} 行数据")

    samples: List[Dict[str, Any]] = []
    n_total = 0
    match_final = 0
    match_both = 0
    match_tier1 = 0
    match_gender = 0
    missing = 0
    agent_errors = 0
    agent_unavailable = 0
    consensus_counts: Dict[str, int] = {"strict": 0, "balanced": 0, "none": 0}
    accepted_total = 0
    accepted_match_final = 0
    accepted_match_both = 0
    accepted_match_tier1 = 0
    accepted_match_gender = 0
    # tier1 共识子集：只要求 tier1 一致（忽略 tier2 / gender）
    tier1_consensus_counts: Dict[str, int] = {"strict": 0, "balanced": 0, "none": 0}
    tier1_accepted_total = 0
    tier1_accepted_match_tier1 = 0

    for row in rows:
        human_age, human_gender, pl = _cell_to_age_gender(row, profile_link_col, human_age_col, human_final_col)
        if multi_agent_mode:
            votes = []
            for age_col, final_col in agent_columns.values():
                age, gender, _ = _cell_to_age_gender(row, profile_link_col, age_col, final_col)
                if age and age[0] not in (SPEC["age"]["unknown_label"], "error", "unavailable"):
                    votes.append((tuple(age), gender))
            if votes:
                (best_age, agent_gender), _ = Counter(votes).most_common(1)[0]
                agent_age = list(best_age)
            else:
                agent_age, agent_gender = [], ""
        else:
            agent_age, agent_gender, _ = _cell_to_age_gender(
                row, profile_link_col, agent_age_col, agent_final_col
            )
        consensus_level = "none"
        if consensus_level_col is not None and consensus_level_col < len(row):
            consensus_level = str(row[consensus_level_col] or "none").strip().lower() or "none"
        accepted_gt = consensus_level in ("strict", "balanced")
        if multi_agent_mode and tier2_consensus_level_col is not None:
            raw_level = row[tier2_consensus_level_col] if tier2_consensus_level_col < len(row) else 0
            try:
                level = int(raw_level or 0)
            except (TypeError, ValueError):
                level = 0
            consensus_level = "strict" if level == 1 else "balanced" if level > 1 else "none"
            accepted_gt = level > 0
        if consensus_level not in consensus_counts:
            consensus_counts[consensus_level] = 0
        if accepted_gt_col is not None and accepted_gt_col < len(row):
            raw_accepted = str(row[accepted_gt_col] or "").strip().lower()
            if raw_accepted in ("true", "1", "yes", "y", "是"):
                accepted_gt = True
            elif raw_accepted in ("false", "0", "no", "n", "否"):
                accepted_gt = False

        tier1_consensus_level = "none"
        if tier1_consensus_level_col is not None and tier1_consensus_level_col < len(row):
            tier1_consensus_level = str(row[tier1_consensus_level_col] or "none").strip().lower() or "none"
        tier1_accepted_gt = tier1_consensus_level in ("strict", "balanced")
        if multi_agent_mode:
            raw_level = row[tier1_consensus_level_col] if (
                tier1_consensus_level_col is not None and tier1_consensus_level_col < len(row)
            ) else 0
            try:
                level = int(raw_level or 0)
            except (TypeError, ValueError):
                level = 0
            tier1_consensus_level = "strict" if level == 1 else "balanced" if level > 1 else "none"
            tier1_accepted_gt = level > 0
        if tier1_consensus_level not in tier1_consensus_counts:
            tier1_consensus_counts[tier1_consensus_level] = 0
        if tier1_accepted_gt_col is not None and tier1_accepted_gt_col < len(row):
            raw_t1_accepted = str(row[tier1_accepted_gt_col] or "").strip().lower()
            if raw_t1_accepted in ("true", "1", "yes", "y", "是"):
                tier1_accepted_gt = True
            elif raw_t1_accepted in ("false", "0", "no", "n", "否"):
                tier1_accepted_gt = False

        # agent_error 列
        agent_err = None
        if agent_error_col is not None and agent_error_col < len(row):
            v = row[agent_error_col]
            if isinstance(v, str) and v.strip():
                agent_err = v.strip()
            elif isinstance(v, dict):
                agent_err = v.get("text") or ""

        has_human = bool(human_age) and human_age and not (len(human_age) == 1 and human_age[0] in (SPEC["age"]["unknown_label"], "", "Unknown"))
        has_agent = bool(agent_age) and agent_age[0] not in (SPEC["age"]["unknown_label"], "unavailable", "error")

        if not pl:
            continue
        if not has_human:
            missing += 1
            samples.append({"profile_link": pl, "human_age": None, "human_gender": None,
                            "agent_age": agent_age, "agent_gender": agent_gender,
                            "status": "MISSING"})
            continue
        if not has_agent and agent_age and agent_age[0] == "unavailable":
            agent_unavailable += 1
            samples.append({"profile_link": pl, "human_age": human_age, "human_gender": human_gender,
                            "agent_age": agent_age, "agent_gender": agent_gender,
                            "status": "UNAVAILABLE", "error": agent_err})
            continue
        if not has_agent and agent_age and agent_age[0] == "error":
            agent_errors += 1
            samples.append({"profile_link": pl, "human_age": human_age, "human_gender": human_gender,
                            "agent_age": agent_age, "agent_gender": agent_gender,
                            "status": "AGENT_ERROR", "error": agent_err})
            continue
        if not has_agent:
            missing += 1
            samples.append({"profile_link": pl, "human_age": None, "human_gender": None,
                            "agent_age": None, "agent_gender": None, "status": "MISSING"})
            continue

        n_total += 1
        consensus_counts[consensus_level] = consensus_counts.get(consensus_level, 0) + 1
        tier1_consensus_counts[tier1_consensus_level] = tier1_consensus_counts.get(tier1_consensus_level, 0) + 1
        same_final = (human_age == agent_age and human_gender == agent_gender)
        same_both = human_age == agent_age
        same_tier1 = (human_age[0] == agent_age[0] if human_age and agent_age else False)
        same_gender = human_gender == agent_gender

        if same_final: match_final += 1
        if same_both: match_both += 1
        if same_tier1: match_tier1 += 1
        if same_gender: match_gender += 1
        if accepted_gt:
            accepted_total += 1
            if same_final: accepted_match_final += 1
            if same_both: accepted_match_both += 1
            if same_tier1: accepted_match_tier1 += 1
            if same_gender: accepted_match_gender += 1
        if tier1_accepted_gt:
            tier1_accepted_total += 1
            if same_tier1: tier1_accepted_match_tier1 += 1
        samples.append({
            "profile_link": pl,
            "human_age": human_age,
            "human_gender": human_gender,
            "agent_age": agent_age,
            "agent_gender": agent_gender,
            "consensus_level": consensus_level,
            "accepted_gt": accepted_gt,
            "tier1_consensus_level": tier1_consensus_level,
            "tier1_accepted_gt": tier1_accepted_gt,
            "final_result_eq": same_final,
            "both_tiers_eq": same_both,
            "tier1_eq": same_tier1,
            "gender_eq": same_gender,
            "status": "OK",
        })

    def _pct(num, den) -> str:
        if den <= 0:
            return "n/a"
        return f"{num}/{den} = {100.0 * num / den:.2f}%"

    print("\n=========== 一致性计算结果 (v6) ===========")
    print(f"分母（有效样本数）: {n_total}")
    print(f"缺失/无法解析样本 : {missing}")
    print(f"agent 出错样本   : {agent_errors}")
    print(f"页面不可用样本   : {agent_unavailable}")
    print(f"consensus 分布   : {consensus_counts}")
    print()
    print(f"(a) final_result 一致  : {_pct(match_final, n_total)}")
    print(f"(b) 两个 tier 都一致   : {_pct(match_both, n_total)}")
    print(f"(c) 第 1 个 tier 一致  : {_pct(match_tier1, n_total)}")
    print(f"(d) gender 一致        : {_pct(match_gender, n_total)}")
    print()
    print("----------- accepted_gt 子集（完整 tier1+tier2 共识）-----------")
    print(f"accepted 样本数       : {accepted_total}")
    print(f"(a) final_result 一致  : {_pct(accepted_match_final, accepted_total)}")
    print(f"(b) 两个 tier 都一致   : {_pct(accepted_match_both, accepted_total)}")
    print(f"(c) 第 1 个 tier 一致  : {_pct(accepted_match_tier1, accepted_total)}")
    print(f"(d) gender 一致        : {_pct(accepted_match_gender, accepted_total)}")
    print()
    print("----------- tier1 共识子集（仅 tier1 一致，忽略 tier2/gender）-----------")
    print(f"tier1 consensus 分布 : {tier1_consensus_counts}")
    print(f"tier1 accepted 样本数 : {tier1_accepted_total}")
    print(f"(c) 第 1 个 tier 一致  : {_pct(tier1_accepted_match_tier1, tier1_accepted_total)}")
    print()

    if args.json_out:
        out = {
            "spreadsheet_token": args.token,
            "sheet": args.sheet,
            "generated_at": __import__("time").strftime("%Y-%m-%d %H:%M:%S"),
            "totals": {"n_total": n_total, "missing": missing, "agent_errors": agent_errors, "agent_unavailable": agent_unavailable},
            "consensus_counts": consensus_counts,
            "tier1_consensus_counts": tier1_consensus_counts,
            "rates": {
                "final_result_match": match_final,
                "both_tiers_match": match_both,
                "tier1_match": match_tier1,
                "gender_match": match_gender,
                "accepted_total": accepted_total,
                "accepted_final_result_match": accepted_match_final,
                "accepted_both_tiers_match": accepted_match_both,
                "accepted_tier1_match": accepted_match_tier1,
                "accepted_gender_match": accepted_match_gender,
                "tier1_accepted_total": tier1_accepted_total,
                "tier1_accepted_tier1_match": tier1_accepted_match_tier1,
            },
            "samples": samples,
        }
        Path(args.json_out).write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[落盘] 样本级明细已写入: {args.json_out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
