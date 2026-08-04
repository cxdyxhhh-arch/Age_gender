"""只截图，不调用任何大模型 — 输入源为飞书表格。

从 Sheet1 的 profile_link 列读取 TikTok 主页链接，逐个打开并截图（头像 + 整页），
截图落在 screenshots/ 目录。全程只调用 tiktok_age_workflow_v6.capture_profile，
不触碰 estimate_age_with_llm* / 任何 LLM 端点，因此无需配置 ARK_API_KEY /
MODELHUB_V2_AK 等模型相关变量，只需要飞书凭证 FEISHU_APP_ID / FEISHU_APP_SECRET。

用法:
    # 用 .env 里的默认 token/sheet，截前 20 个
    python3 snap_only_v6.py --limit 20

    # 指定表格与 sheet
    python3 snap_only_v6.py \
        --token "MaTws7lJohae55t7TY8mSRaIyJb" \
        --sheet1 "Sheet1" \
        --limit 0 \                 # <=0 表示不限
        --workers 4                 # 并行截图的浏览器数（默认 4）

    # 多机分片（与 batch_run_v6 相同的 md5 取模分片）
    WORKER_ID=0 TOTAL_WORKERS=2 python3 snap_only_v6.py --limit 0 --workers 8
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Dict, List

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(HERE / ".env", override=True)

# 复用批量脚本里已有的飞书读取 / 分片工具，避免重复实现
import batch_run_v6 as batch  # noqa: E402
import tiktok_age_workflow_v6 as v6  # noqa: E402


def _snap_one(url: str, headless: bool = True) -> Dict[str, Any]:
    """对单个 URL 只截图，返回结果 dict（不写 JSON、不调模型）。"""
    u = v6._validate_url(url)
    try:
        snap = v6.capture_profile(u, headless=headless)
        return {
            "url": u,
            "handle": snap.get("handle"),
            "avatar_path": str(snap.get("avatar_path") or ""),
            "page_path": str(snap.get("page_path") or ""),
            "state": "ok",
        }
    except v6.PageUnavailableError as exc:
        return {"url": u, "state": "unavailable", "error": str(exc)}
    except Exception as exc:  # noqa: BLE001
        return {"url": u, "state": "error", "error": f"{type(exc).__name__}: {exc}"[:500]}


def main() -> int:
    ap = argparse.ArgumentParser(description="只截图不调模型，输入源为飞书 Sheet1")
    ap.add_argument("--token", default=batch.DEFAULT_SPREADSHEET_TOKEN,
                    help="飞书电子表格 token（默认取 .env / batch 默认值）")
    ap.add_argument("--sheet1", default=batch.DEFAULT_SHEET1_NAME,
                    help="输入 sheet 名（profile_link 列所在）")
    ap.add_argument("--limit", type=int, default=20,
                    help="最多截多少个账号（<=0 表示不限）")
    ap.add_argument("--workers", type=int, default=4,
                    help="并行截图的浏览器数量")
    ap.add_argument("--max-sheet1-rows", type=int, default=0,
                    help="最多从 Sheet1 读取多少行；<=0 表示用全局 MAX_SHEET_ROWS（.env 配置）")
    ap.add_argument("--worker-id", type=int,
                    default=int(os.getenv("WORKER_ID") or 0),
                    help="分片编号（默认取环境变量 WORKER_ID）")
    ap.add_argument("--total-workers", type=int,
                    default=int(os.getenv("TOTAL_WORKERS") or 1),
                    help="机器总数（默认取环境变量 TOTAL_WORKERS）")
    ap.add_argument("--no-headless", action="store_true",
                    help="显示浏览器窗口（默认无头）")
    args = ap.parse_args()

    # 1) 从飞书 Sheet1 读取 URL 列表（复用 batch 的列自动识别 + 分页逻辑）
    urls = batch.read_sheet1_profile_links(args.token, args.sheet1,
                                           max_rows=args.max_sheet1_rows)

    # 2) 多机分片（与 batch_run_v6 相同的 md5 取模，保证跨机一致）
    if args.total_workers > 1:
        before = len(urls)
        urls = batch._filter_shard(urls, args.worker_id, args.total_workers)
        print(f"  [shard] worker {args.worker_id}/{args.total_workers}: "
              f"{before} -> {len(urls)} 个 URL")

    # 3) 去重 + 截断
    seen: set = set()
    deduped: List[str] = []
    for u in urls:
        key = batch._normalize_url_key(u) or u
        if key in seen:
            continue
        seen.add(key)
        deduped.append(u)
    urls = deduped
    if args.limit and args.limit > 0:
        urls = urls[: args.limit]

    if not urls:
        print("没有可截图的 URL。")
        return 0

    headless = not args.no_headless
    workers = max(1, args.workers)
    print(f"  [snap-only] 共 {len(urls)} 个 URL，headless={headless}，workers={workers}")
    print(f"  [snap-only] 截图输出目录: {v6.OUTPUT_DIR}")

    ok = unavailable = error = 0
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=workers) as pool:
        fut_map = {pool.submit(_snap_one, u, headless): u for u in urls}
        for i, fut in enumerate(as_completed(fut_map), start=1):
            r = fut.result()
            state = r.get("state")
            if state == "ok":
                ok += 1
                print(f"  [{i}/{len(urls)}] ok   {r['url']}\n"
                      f"        avatar={r['avatar_path']}\n"
                      f"        page  ={r['page_path']}")
            elif state == "unavailable":
                unavailable += 1
                print(f"  [{i}/{len(urls)}] skip {r['url']} 页面不可用: {r.get('error')}")
            else:
                error += 1
                print(f"  [{i}/{len(urls)}] ERR  {r['url']} {r.get('error')}")

    dt = time.time() - t0
    print(f"\n  [snap-only] 完成: ok={ok} unavailable={unavailable} error={error} "
          f"用时 {dt:.1f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
