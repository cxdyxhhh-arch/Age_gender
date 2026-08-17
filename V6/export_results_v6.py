"""从 screenshots/*_v6.json 导出 csv / xlsx 报告（不依赖飞书，纯本地）。

新增功能的所有公共函数统一以 ``exp_`` 前缀命名，避免与现有模块（batch_run_v6 /
tiktok_age_workflow_v6 / recover_v6）的函数名冲突，也便于在 grep 时一眼识别。

用法:
    # 导出磁盘上全部结果到 export_out/ 目录
    python3 export_results_v6.py

    # 只导出"输入表里出现过的 URL"（需要 .env 配好 INPUT_SPREADSHEET_TOKEN）
    python3 export_results_v6.py --urls-from-input-sheet

    # 只导出 --urls-file 里列出的 URL（每行一个）
    python3 export_results_v6.py --urls-file my_urls.txt

    # 限制数量 + dry-run 先看匹配统计
    python3 export_results_v6.py --limit 100 --dry-run
"""

from __future__ import annotations

import argparse
import csv
import glob
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

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

import batch_run_v6 as b  # noqa: E402

RESULT_GLOB = "screenshots/*_v6.json"
DEFAULT_OUTPUT_DIR = HERE / "export_out"


# ---------------------------------------------------------------------------
# exp_ 前缀工具函数
# ---------------------------------------------------------------------------

def exp_agent_names() -> List[str]:
    """从环境变量取 AGENT_CONFIGS，保证与 batch_run_v6 的 agent 名单一致。"""
    raw = os.getenv("AGENT_CONFIGS") or "agent_a,agent_b,agent_c"
    return [s.strip() for s in raw.split(",") if s.strip()]


def exp_normalize_url_key(val: Any) -> str:
    """薄封装，直接复用 batch_run_v6._normalize_url_key 保证匹配规则一致。"""
    return b._normalize_url_key(val)


def exp_load_disk_results(screenshot_dir: Path,
                          match_keys: Optional[set] = None,
                          limit: Optional[int] = None
                          ) -> Tuple[Dict[str, Dict[str, Any]], Dict[str, float]]:
    """读取 screenshots 目录下所有 *_v6.json。

    与 recover_v6._load_disk_results 语义相同，但：
    - 允许指定截图目录
    - 允许提前按 URL key 过滤（减少内存占用）
    - 允许 limit（用于快速预览）

    返回 (results_by_key, mtimes_by_key)，results_by_key 里保留 mtime 最新的一条。
    """
    out: Dict[str, Dict[str, Any]] = {}
    mtimes: Dict[str, float] = {}
    pattern = str(screenshot_dir / "*_v6.json")
    files = glob.glob(pattern)
    files.sort(key=os.path.getmtime, reverse=True)  # 新→旧，limit 下先拿到新的
    added = 0
    skipped_by_filter = 0
    for f in files:
        try:
            d = json.load(open(f, encoding="utf-8"))
        except Exception:
            continue
        url = d.get("url") or d.get("profile_link")
        if not url:
            continue
        key = exp_normalize_url_key(url)
        if not key:
            continue
        if match_keys is not None and key not in match_keys:
            skipped_by_filter += 1
            continue
        mt = os.path.getmtime(f)
        if key in out:
            # 新文件已排在前，旧的跳过
            continue
        # 把源文件路径塞进结果，方便追踪
        d["__json_file__"] = str(Path(f).resolve())
        out[key] = d
        mtimes[key] = mt
        added += 1
        if limit and added >= limit:
            break
    if match_keys is not None:
        print(f"  [exp] 读磁盘: {len(files)} 个文件, 命中 {added} 个, "
              f"被 --urls-* 过滤掉 {skipped_by_filter} 个")
    else:
        print(f"  [exp] 读磁盘: {len(files)} 个文件, 去重后 {added} 个唯一 URL")
    return out, mtimes


def exp_read_input_sheet_urls(token: Optional[str] = None,
                               sheet_name: Optional[str] = None) -> List[str]:
    """从飞书输入表读 URL 列表（复用 batch_run_v6.read_sheet1_profile_links）。

    优先级: CLI 显式传入 > .env 环境变量 > 硬编码默认。
    """
    effective_token = (
        token
        or os.getenv("INPUT_SPREADSHEET_TOKEN")
        or os.getenv("BATCH_SPREADSHEET_TOKEN")
        or os.getenv("OUTPUT_SPREADSHEET_TOKEN")
        or os.getenv("SPREADSHEET_TOKEN")
        or "MaTws7lJohae55t7TY8mSRaIyJb"
    )
    if not effective_token:
        raise RuntimeError(
            "缺少飞书表格 token：请传 --token，或在 .env 里配置 "
            "INPUT_SPREADSHEET_TOKEN / BATCH_SPREADSHEET_TOKEN"
        )
    effective_sheet = (
        sheet_name
        or os.getenv("BATCH_SHEET1")
        or os.getenv("INPUT_SHEET_NAME")
        or "Sheet1"
    )
    print(f"  [exp] 飞书输入表: token={effective_token[:6]}***  "
          f"sheet_name_arg={sheet_name!r}  "
          f"sheet_final={effective_sheet!r}")
    return b.read_sheet1_profile_links(effective_token, effective_sheet)


def exp_read_urls_file(path: Path) -> List[str]:
    """从本地 txt/csv 读 URL（每行一个），去重保留顺序。"""
    urls: List[str] = []
    seen: set = set()
    with open(path, encoding="utf-8") as f:
        for line in f:
            s = line.strip().strip('"').strip("'").strip(",")
            if not s:
                continue
            if s.lower().startswith(("http://", "https://", "www.", "tiktok.com/")):
                k = exp_normalize_url_key(s)
                if k and k not in seen:
                    seen.add(k)
                    urls.append(s)
    return urls


def _flatten_evidence_brief(ev: Any) -> Dict[str, str]:
    """从单个 agent 的 evidence 里抽出少量结构化字段，便于直接在表中筛选。"""
    flat: Dict[str, str] = {}
    if not isinstance(ev, dict):
        return flat
    for k in ("aging_signal_count", "has_45plus_trigger",
              "account_status", "subject_validity"):
        v = ev.get(k)
        if v is None:
            continue
        if isinstance(v, (list, dict)):
            flat[k] = json.dumps(v, ensure_ascii=False)
        else:
            flat[k] = str(v)
    ht = ev.get("hard_triggers_45_plus")
    if isinstance(ht, list) and ht:
        flat["hard_triggers_45_plus"] = "|".join(str(x) for x in ht)
    ag_sigs = ev.get("aging_signals")
    if ag_sigs is not None and not (isinstance(ag_sigs, str) and ag_sigs.strip() in ("无", "none", "None", "")):
        flat["aging_signals_brief"] = str(ag_sigs)
    return flat


def exp_flatten_result(data: Dict[str, Any],
                       agent_names: List[str]) -> Dict[str, Any]:
    """把单条 *_v6.json 拍平成"一行宽表"字典。

    返回的 dict key 即最终表头。包含：
    - 基础信息（url / handle / display_name / bio ...）
    - 共识信息（tier1_consensus_level / 汇总后的 final_tier1/gender ...）
    - 每个 agent 独立列：{agent}_status, {agent}_tier1, {agent}_gender,
      {agent}_confidence, {agent}_reasoning, {agent}_evidence_json ...
    """
    row: Dict[str, Any] = {}

    row["url"] = data.get("url") or data.get("profile_link") or ""
    row["handle"] = data.get("handle") or ""
    row["display_name"] = data.get("display_name") or ""
    row["bio"] = data.get("bio") or ""
    row["stats"] = data.get("stats") or ""
    row["avatar_screenshot"] = data.get("avatar_screenshot") or ""
    row["page_screenshot"] = data.get("page_screenshot") or ""
    row["json_file"] = data.get("__json_file__") or ""
    row["tier1_consensus_level"] = data.get("tier1_consensus_level", "")
    row["tier2_consensus_level"] = data.get("tier2_consensus_level", "")
    row["error"] = data.get("error") or ""

    # final_*：优先从 agent_results 挑一个已完成且非 skipped 的 agent 代表
    # （V6 没有单独 final_result 顶层字段；如果将来有就改这里优先取它）
    agent_results = data.get("agent_results") or {}
    votes_raw = data.get("agent_votes_raw") or {}
    final_tier1 = final_tier2 = final_gender = final_confidence = ""
    for an in agent_names:
        ar = agent_results.get(an) or {}
        vr = votes_raw.get(an) or {}
        status = (ar.get("status") or (ar.get("skipped") and "skipped")
                  or ("error" if ar.get("error") else "") or "completed")
        if status == "completed":
            age_list = (vr.get("age") if isinstance(vr.get("age"), list) else None)
            if age_list:
                final_tier1 = str(age_list[0] or "")
                if len(age_list) > 1:
                    final_tier2 = str(age_list[1] or "")
            fr = vr.get("final_result") if isinstance(vr.get("final_result"), dict) else {}
            if fr.get("gender"):
                final_gender = str(fr["gender"])
            if vr.get("confidence"):
                final_confidence = str(vr["confidence"])
            break
    row["final_tier1"] = final_tier1
    row["final_tier2"] = final_tier2
    row["final_gender"] = final_gender
    row["final_confidence"] = final_confidence

    # 每个 agent 独立列
    for an in agent_names:
        ar = agent_results.get(an) or {}
        vr = votes_raw.get(an) or {}

        status = (ar.get("status")
                  or ("skipped" if ar.get("skipped") else "")
                  or ("error" if ar.get("error") else "")
                  or ("completed" if vr.get("tier1") else "")
                  or "")
        skip_reason = ar.get("skip_reason") or ""
        err = ar.get("error") or vr.get("error") or ""

        tier1 = vr.get("tier1") or ""
        tier2 = vr.get("tier2") or ""
        gender = vr.get("gender") or (
            vr["final_result"]["gender"]
            if isinstance(vr.get("final_result"), dict) and vr["final_result"].get("gender")
            else ""
        )
        confidence = vr.get("confidence") or ar.get("confidence") or ""
        age = vr.get("age")
        if isinstance(age, list):
            age_str = json.dumps(age, ensure_ascii=False)
        else:
            age_str = ""
        final_result = vr.get("final_result")
        if isinstance(final_result, dict):
            final_result_str = json.dumps(final_result, ensure_ascii=False)
        else:
            final_result_str = ""

        reasoning = vr.get("reasoning") or ""
        evidence = vr.get("evidence")
        if isinstance(evidence, dict):
            evidence_json = json.dumps(evidence, ensure_ascii=False)
        elif evidence is not None:
            evidence_json = str(evidence)
        else:
            evidence_json = ""

        row[f"{an}_status"] = status
        row[f"{an}_skip_reason"] = skip_reason
        row[f"{an}_error"] = err
        row[f"{an}_tier1"] = tier1
        row[f"{an}_tier2"] = tier2
        row[f"{an}_gender"] = gender
        row[f"{an}_confidence"] = confidence
        row[f"{an}_age"] = age_str
        row[f"{an}_final_result"] = final_result_str
        row[f"{an}_reasoning"] = reasoning
        row[f"{an}_evidence_json"] = evidence_json

        # evidence 结构化子字段（方便筛选/对比）
        brief = _flatten_evidence_brief(vr.get("evidence"))
        for bk, bv in brief.items():
            row[f"{an}_{bk}"] = bv

        # 原始响应（做故障排查时用）
        raw_resp = vr.get("raw_response") or ""
        row[f"{an}_raw_response"] = raw_resp

    return row


def exp_build_header(agent_names: List[str],
                     sample_row: Optional[Dict[str, Any]] = None,
                     per_agent_fields: Optional[List[str]] = None,
                     base_fields: Optional[List[str]] = None,
                     strict: bool = False) -> List[str]:
    """构建输出列头：基础固定列在前，按 agent 顺序展开，末尾追加 evidence brief 动态列。

    - per_agent_fields: 指定每个 agent 只输出哪些子字段（如 ['age','gender','reasoning']）；
      None 时输出全部默认字段。
    - base_fields: 指定基础列；None 时输出全部默认基础列。
    - strict: True 时不把 sample_row 里的额外列补到末尾（用于精确列筛选）。
    """
    default_base = [
        "url", "handle", "display_name", "bio", "stats",
        "final_tier1", "final_tier2", "final_gender", "final_confidence",
        "tier1_consensus_level", "tier2_consensus_level",
        "avatar_screenshot", "page_screenshot", "json_file", "error",
    ]
    default_per_agent = [
        "status", "skip_reason", "error",
        "tier1", "tier2", "gender", "confidence",
        "age", "final_result",
        "reasoning", "evidence_json",
        "aging_signal_count", "has_45plus_trigger",
        "account_status", "subject_validity",
        "hard_triggers_45_plus", "aging_signals_brief",
        "raw_response",
    ]
    base = base_fields if base_fields is not None else default_base
    per_agent = per_agent_fields if per_agent_fields is not None else default_per_agent
    header: List[str] = list(base)
    for an in agent_names:
        for field in per_agent:
            header.append(f"{an}_{field}")
    # 如果 sample_row 里还有多出来的列（例如某 evidence 中罕见字段），补到末尾
    if sample_row and not strict:
        existing = set(header)
        for k in sample_row.keys():
            if k not in existing:
                header.append(k)
    return header


def exp_write_csv(rows: List[Dict[str, Any]],
                  header: List[str],
                  csv_path: Path) -> None:
    """写 csv（UTF-8 with BOM，这样 Excel/WPS 直接打开不乱码）。"""
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    with open(csv_path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=header, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            row_out = {}
            for k in header:
                v = r.get(k, "")
                if v is None:
                    v = ""
                # Excel 限制：单元格 32767 字符。csv 其实没有硬限制，
                # 但为了"和 xlsx 对得上"，超 32000 做兜底截断并标 ...
                if isinstance(v, str) and len(v) > 32700:
                    v = v[:32700] + "...[TRUNCATED]"
                row_out[k] = v
            w.writerow(row_out)


def exp_write_xlsx(rows: List[Dict[str, Any]],
                   header: List[str],
                   xlsx_path: Path) -> None:
    """写 xlsx。优先 pandas+openpyxl；没有就用纯 openpyxl；都没有时报错提示安装。"""
    xlsx_path.parent.mkdir(parents=True, exist_ok=True)

    # 策略 1：pandas 存在（最快，而且可以顺便用它控制列宽 hint）
    try:
        import pandas as pd  # type: ignore
        df = pd.DataFrame([{k: r.get(k, "") for k in header} for r in rows], columns=header)
        df.to_excel(xlsx_path, index=False, engine="openpyxl")
        return
    except ImportError:
        pass
    except Exception as exc:  # noqa: BLE001
        print(f"  [exp] pandas to_excel 异常，回退纯 openpyxl: {exc}")

    # 策略 2：纯 openpyxl
    try:
        from openpyxl import Workbook  # type: ignore
    except ImportError as e:
        raise RuntimeError(
            "导出 xlsx 需要 pandas 或 openpyxl。请: pip install openpyxl pandas"
        ) from e

    wb = Workbook(write_only=True)
    ws = wb.create_sheet("results")
    ws.append(header)
    for r in rows:
        line = []
        for k in header:
            v = r.get(k, "")
            if v is None:
                v = ""
            if isinstance(v, str) and len(v) > 32767:
                v = v[:32700] + "...[TRUNCATED]"
            line.append(v)
        ws.append(line)
    wb.save(xlsx_path)


def exp_build_rows_from_disk(results_by_key: Dict[str, Dict[str, Any]],
                             ordered_keys: List[str],
                             agent_names: List[str]) -> List[Dict[str, Any]]:
    """按 ordered_keys 顺序组装行（ordered_keys 里有但磁盘没有的，行里填占位 URL）。"""
    rows: List[Dict[str, Any]] = []
    seen_in_rows: set = set()
    missing = 0
    for key in ordered_keys:
        if key in seen_in_rows:
            continue
        seen_in_rows.add(key)
        d = results_by_key.get(key)
        if d is None:
            missing += 1
            rows.append({
                "url": "",
                "handle": "",
                "display_name": "",
                "bio": "",
                "stats": "",
                "final_tier1": "",
                "final_tier2": "",
                "final_gender": "",
                "final_confidence": "",
                "tier1_consensus_level": "",
                "tier2_consensus_level": "",
                "avatar_screenshot": "",
                "page_screenshot": "",
                "json_file": "",
                "error": "MISSING_IN_SCREENSHOTS",
                "__missing_url_key__": key,
            })
        else:
            rows.append(exp_flatten_result(d, agent_names))
    # 磁盘里有但 ordered_keys 没覆盖的（比如无筛选时），按 key 排序追加
    if len(ordered_keys) == 0 or ordered_keys == sorted(results_by_key.keys()):
        # ordered_keys 是"空列表+全量"时这里不会触发；主要处理"筛选条件没有覆盖全部磁盘结果"的场景
        pass
    for key in sorted(results_by_key.keys()):
        if key in seen_in_rows:
            continue
        seen_in_rows.add(key)
        rows.append(exp_flatten_result(results_by_key[key], agent_names))
    if missing:
        print(f"  [exp] 注意: {missing} 个 URL 在磁盘结果中找不到对应 *_v6.json，"
              f"error=MISSING_IN_SCREENSHOTS")
    return rows


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description="从 screenshots/*_v6.json 导出 csv/xlsx")
    parser.add_argument("--screenshots-dir", default=str(HERE / "screenshots"),
                        help="截图与JSON目录，默认 ./screenshots")
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR),
                        help="输出目录，默认 ./export_out")
    parser.add_argument("--prefix", default="",
                        help="输出文件名前缀，默认用时间戳")
    parser.add_argument("--urls-from-input-sheet", action="store_true",
                        help="从飞书输入表取URL（需要 token：--token 或 .env）")
    parser.add_argument("--token", default="",
                        help="飞书 spreadsheet token（优先级高于 .env 的 INPUT/OUTPUT/BATCH_SPREADSHEET_TOKEN）")
    parser.add_argument("--sheet", default="",
                        help="飞书输入 sheet 名（优先级高于 .env 的 BATCH_SHEET1/INPUT_SHEET_NAME）")
    parser.add_argument("--urls-file", default="",
                        help="从本地 txt/csv 取 URL（每行一个）")
    parser.add_argument("--limit", type=int, default=0,
                        help="最多处理多少个磁盘JSON（<=0 不限）")
    parser.add_argument("--format", default="both",
                        choices=["both", "csv", "xlsx"],
                        help="导出格式（默认同时输出 csv+xlsx）")
    parser.add_argument("--per-agent-fields", default="",
                        help="逗号分隔，每个 agent 只导出这些子字段（如 age,gender,reasoning）；空=全量")
    parser.add_argument("--base-fields", default="",
                        help="逗号分隔，只导出这些基础列（如 url）；空=全量基础列")
    parser.add_argument("--minimal", action="store_true",
                        help="等价于 --base-fields url --per-agent-fields age,gender,reasoning")
    parser.add_argument("--dry-run", action="store_true",
                        help="只打印统计与表头，不写文件")
    args = parser.parse_args()

    screenshots_dir = Path(args.screenshots_dir).resolve()
    output_dir = Path(args.output_dir).resolve()
    limit = args.limit if args.limit > 0 else None

    t0 = time.time()
    agent_names = exp_agent_names()
    print(f"[exp] agent 名单: {agent_names}")
    print(f"[exp] screenshots 目录: {screenshots_dir}")
    print(f"[exp] 输出目录: {output_dir}")
    if args.token or args.sheet or args.urls_from_input_sheet:
        print(f"[exp] CLI 解析 --token={args.token!r}  --sheet={args.sheet!r}  "
              f"--urls-from-input-sheet={args.urls_from_input_sheet}")

    # 1) 确定 URL 筛选范围
    ordered_keys: List[str] = []
    match_keys: Optional[set] = None
    cli_input_token = args.token.strip() or None
    cli_input_sheet = args.sheet.strip() or None
    if args.urls_from_input_sheet and args.urls_file:
        print("  ⚠️  --urls-from-input-sheet 与 --urls-file 同时指定，取并集")
    if args.urls_from_input_sheet:
        print("[exp] 从飞书输入表读取URL列表...")
        urls1 = exp_read_input_sheet_urls(token=cli_input_token, sheet_name=cli_input_sheet)
        for u in urls1:
            k = exp_normalize_url_key(u)
            if k and k not in ordered_keys:
                ordered_keys.append(k)
    if args.urls_file:
        uf = Path(args.urls_file).resolve()
        if not uf.is_file():
            print(f"  ❌ --urls-file 不存在: {uf}")
            return 2
        urls2 = exp_read_urls_file(uf)
        for u in urls2:
            k = exp_normalize_url_key(u)
            if k and k not in ordered_keys:
                ordered_keys.append(k)
    if ordered_keys:
        match_keys = set(ordered_keys)
        print(f"[exp] URL 筛选: {len(match_keys)} 个唯一 key")
    else:
        print("[exp] 无 URL 筛选，导出 screenshots 下所有 *_v6.json")

    # 2) 读磁盘 JSON
    results_by_key, _ = exp_load_disk_results(screenshots_dir, match_keys=match_keys, limit=limit)
    if not results_by_key:
        print("  ❌ 没有任何结果匹配。请检查目录/筛选条件/磁盘落盘。")
        return 1

    # 3) 如果没指定筛选，就按磁盘结果的 key 排序生成有序行
    if not ordered_keys:
        ordered_keys = sorted(results_by_key.keys())

    # 4) 拍平成行
    rows = exp_build_rows_from_disk(results_by_key, ordered_keys, agent_names)
    sample_row = rows[0]

    # 解析列筛选（--minimal 是便捷开关，等价于 base=url per_agent=age,gender,reasoning）
    per_agent_fields: Optional[List[str]] = None
    base_fields: Optional[List[str]] = None
    if args.minimal:
        base_fields = ["url"]
        per_agent_fields = ["age", "gender", "reasoning"]
    if args.per_agent_fields.strip():
        per_agent_fields = [s.strip() for s in args.per_agent_fields.split(",") if s.strip()]
    if args.base_fields.strip():
        base_fields = [s.strip() for s in args.base_fields.split(",") if s.strip()]
    strict = bool(args.minimal or args.per_agent_fields.strip() or args.base_fields.strip())

    header = exp_build_header(agent_names, sample_row,
                              per_agent_fields=per_agent_fields,
                              base_fields=base_fields,
                              strict=strict)
    print(f"[exp] 共 {len(rows)} 行, {len(header)} 列")
    if strict:
        print(f"[exp] 列筛选已启用: base={base_fields}  per_agent={per_agent_fields}")

    if args.dry_run:
        print("[dry-run] 表头前 24 列（后面省略）:")
        for h in header[:24]:
            print(f"  - {h}")
        if len(header) > 24:
            print(f"  ... 还有 {len(header) - 24} 列")
        print(f"[dry-run] 预计输出 {len(rows)} 行 × {len(header)} 列。")
        return 0

    # 5) 写文件
    stamp = time.strftime("%Y%m%d_%H%M%S")
    prefix = f"{args.prefix}_" if args.prefix else ""
    csv_path = output_dir / f"{prefix}age_gender_export_{stamp}.csv"
    xlsx_path = output_dir / f"{prefix}age_gender_export_{stamp}.xlsx"

    wrote: List[str] = []
    if args.format in ("both", "csv"):
        t = time.time()
        exp_write_csv(rows, header, csv_path)
        wrote.append(f"csv ({csv_path.name}, {(csv_path.stat().st_size/1024/1024):.1f}MB, "
                     f"{time.time()-t:.1f}s)")
    if args.format in ("both", "xlsx"):
        try:
            t = time.time()
            exp_write_xlsx(rows, header, xlsx_path)
            wrote.append(f"xlsx ({xlsx_path.name}, {(xlsx_path.stat().st_size/1024/1024):.1f}MB, "
                         f"{time.time()-t:.1f}s)")
        except Exception as exc:  # noqa: BLE001
            print(f"  ⚠️  xlsx 导出失败（csv 仍可用）: {exc}")

    print(f"\n== 导出完成: 总耗时 {time.time()-t0:.1f}s, {len(rows)} 行 × {len(header)} 列 ==")
    print("写出文件:")
    for w in wrote:
        print(f"  - {w}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
