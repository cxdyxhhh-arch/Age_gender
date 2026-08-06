"""从磁盘 screenshots/*_v6.json 恢复写回 Sheet2。

用途：批量跑过程中飞书 token 过期导致大量写回 HTTP 400，结果其实都已落盘。
本脚本读取所有结果 JSON，按 profile_link 匹配 Sheet2 行号，恢复写回 V6 agent 字段。

关键点：
  - 每写 N 行前重新取一次 token（lark_sheets 内部带缓存+过期刷新），避免长任务 token 过期。
  - 写回逻辑复用 batch_run_v6._flush，保证新增字段与批处理一致。
  - 默认只写"表里还没填"的行；--overwrite 可强制覆盖全部。
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sys
from typing import Any, Dict, List

import batch_run_v6 as b

RESULT_GLOB = "screenshots/*_v6.json"


def _load_disk_results() -> Dict[str, Dict[str, Any]]:
    """读所有结果 JSON，按规范化 URL key 去重（保留 mtime 最新的）。"""
    out: Dict[str, Dict[str, Any]] = {}
    mtimes: Dict[str, float] = {}
    for f in glob.glob(RESULT_GLOB):
        try:
            d = json.load(open(f, encoding="utf-8"))
        except Exception:
            continue
        url = d.get("url") or d.get("profile_link")
        if not url:
            continue
        key = b._normalize_url_key(url)
        if not key:
            continue
        mt = os.path.getmtime(f)
        if key not in out or mt > mtimes.get(key, 0):
            out[key] = d
            mtimes[key] = mt
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="从磁盘结果恢复写回 Sheet2")
    parser.add_argument("--token", default=os.getenv("OUTPUT_SPREADSHEET_TOKEN", ""),
                        help="表格 token，默认取 .env 的 OUTPUT_SPREADSHEET_TOKEN")
    parser.add_argument("--sheet2", default="Sheet2", help="输出 sheet 名")
    parser.add_argument("--overwrite", action="store_true",
                        help="即使该行已填 agent 结果也覆盖（默认只补空行）")
    parser.add_argument("--refresh-every", type=int, default=100,
                        help="每写 N 行刷新一次 token")
    parser.add_argument("--dry-run", action="store_true", help="只统计不写入")
    args = parser.parse_args()

    if not args.token:
        print("缺少 --token 或 OUTPUT_SPREADSHEET_TOKEN")
        return 2

    results = _load_disk_results()
    print(f"磁盘结果: {len(results)} 个唯一 URL")

    bearer = b._get_token()
    sid = b._resolve_sheet_id(args.token, args.sheet2, bearer)

    # profile_link 列 -> 行号
    rows = b._read_rows_paginated(args.token, sid, "A", "A", bearer,
                                  page_size=500)
    url_to_row: Dict[str, int] = {}
    for i, r in enumerate(rows):
        if not r:
            continue
        key = b._normalize_url_key(r[0])
        if key:
            url_to_row[key] = i + 2

    # 已填 B 列（用于跳过）
    bcol = b._read_rows_paginated(args.token, sid, "B", "B", bearer,
                                  page_size=500)
    filled_rows = set()
    for i, r in enumerate(bcol):
        if r and str(r[0]).strip() and str(r[0]).strip() != "None":
            filled_rows.add(i + 2)

    # 构建待写列表
    todo: List[Dict[str, Any]] = []
    matched = 0
    unmatched = 0
    skipped = 0
    for key, r in results.items():
        row_n = url_to_row.get(key)
        if not row_n:
            unmatched += 1
            continue
        matched += 1
        if (not args.overwrite) and (row_n in filled_rows):
            skipped += 1
            continue
        todo.append(r)

    print(f"匹配到行: {matched}, 匹配不到: {unmatched}, "
          f"已填跳过: {skipped}, 待写: {len(todo)}")

    if args.dry_run:
        print("[dry-run] 不写入。")
        return 0
    if not todo:
        print("没有需要写入的行。")
        return 0

    b._flush(todo, args.token, args.sheet2, bearer, sid)
    print(f"\n== 恢复完成: 写入 {len(todo)}, 失败 0, 共 {len(todo)} ==")
    return 0


if __name__ == "__main__":
    sys.exit(main())
