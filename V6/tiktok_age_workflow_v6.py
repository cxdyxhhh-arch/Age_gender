"""TikTok 账号年龄 / 性别标注 — v6

V6 主要能力:
- 结果文件名带 WORKER_TAG（共享文件系统多机并发时避免冲突）
- system prompt / user prompt 从 labeling_spec_v6.json 渲染
- 输出规范化用 labeling_spec_v6.normalize_agent_output
- 输出写入飞书 Sheet2 的 [Agent-Labeling] age / final_result / error / confidence 列
- 新增规则后处理：自动纠正 45+ 被低估、25-34 与衰老信号冲突
- 新增边界复核器：对高风险样本进行二次判定（多 agent 协同）
- 新增 confidence 校准：发生规则纠偏时自动降级

用法:
    python3 tiktok_age_workflow_v6.py https://www.tiktok.com/@somebody
或作为模块:
    import tiktok_age_workflow_v6 as v6
    result = v6.run(url, headless=True, retry=1)
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

try:
    from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError  # type: ignore
except Exception:
    sync_playwright = None  # type: ignore
    PlaywrightTimeoutError = Exception

try:
    import httpx  # type: ignore
except ImportError:
    httpx = None

from dotenv import load_dotenv  # type: ignore

load_dotenv(HERE / ".env", override=True)

import labeling_spec_v6 as spec_v3  # noqa: E402

SPEC = spec_v3.load_spec()
SYSTEM_PROMPT = spec_v3.build_full_system_prompt(SPEC)

# --------------------
# JSON 输出目录（screenshots/{handle}_{timestamp}_{worker_tag}_v6.json）
# 多机共享文件系统时，用 WORKER_TAG 区分不同机器，避免文件名冲突。
# --------------------
OUTPUT_DIR = HERE / "screenshots"
OUTPUT_DIR.mkdir(exist_ok=True)
WORKER_TAG = re.sub(r"[^A-Za-z0-9_.-]", "_", os.getenv("WORKER_TAG", "") or "")


# --------------------
# 自定义异常
# --------------------
class PageUnavailableError(RuntimeError):
    """用户主页不可用（private / 不存在 / 被封禁等）。"""


class AgentRunMode(str, Enum):
    """多 Agent 调度模式。"""

    PARALLEL = "parallel"
    HIGH_CONFIDENCE_GATE = "high_confidence_gate"


# --------------------
# 截图
# --------------------
def _validate_url(url: str) -> str:
    if not url:
        raise ValueError("url 为空")
    s = url.strip()
    # 只有当既不以 http:// 也不以 https:// 开头时才补前缀
    if not s.startswith("http://") and not s.startswith("https://"):
        s = "https://" + s
    if "tiktok.com" not in s.lower():
        # 允许其他域名（测试时可传），发出警告但不抛异常
        pass
    return s


def _scroll_to_load_lazy_images(page, max_loops: int = 6, step_ratio: float = 0.8,
                                pause_ms: int = 1200, max_scroll_px: int = 5000) -> None:
    """只滚动前几屏，触发前几行视频封面懒加载即可。

    做年龄/性别标注不需要滚到底，3-5 屏已经足够看到头像、简介、以及
    足够数量的视频封面。max_scroll_px 限制最大滚动距离，避免页面
    无限加载导致截图过大。
    """
    try:
        viewport = page.evaluate("window.innerHeight") or 800
    except Exception:
        viewport = 800
    pos = 0
    for _ in range(max_loops):
        pos += int(viewport * step_ratio)
        if pos > max_scroll_px:  # 不滚超过 max_scroll_px，避免页面无限加载
            break
        try:
            page.evaluate(f"window.scrollTo(0, {pos})")
        except Exception:
            break
        page.wait_for_timeout(pause_ms)


def _wait_for_images_decoded(page, timeout_ms: int = 20_000, min_ratio: float = 0.9) -> None:
    """轮询等待页面中可见图片真正解码完成（naturalWidth > 0）。

    懒加载的封面即使进入视口，网络慢时也需要时间下载解码。这里直接以
    "已解码图片占比"作为就绪信号，比固定 sleep 更稳。
    """
    deadline = time.time() + timeout_ms / 1000.0
    js = """
    () => {
      const imgs = Array.from(document.images || []);
      const visible = imgs.filter(img => {
        const r = img.getBoundingClientRect();
        return r.width >= 60 && r.height >= 60;
      });
      if (visible.length === 0) return {total: 0, done: 0};
      const done = visible.filter(img => img.complete && img.naturalWidth > 0).length;
      return {total: visible.length, done};
    }
    """
    while time.time() < deadline:
        try:
            stat = page.evaluate(js)
        except Exception:
            return
        total = stat.get("total", 0)
        done = stat.get("done", 0)
        if total > 0 and done / total >= min_ratio:
            return
        page.wait_for_timeout(500)


def capture_profile(url: str, headless: bool = True, timeout_ms: int = 60_000,
                     output_dir: Optional[Path] = None) -> Dict[str, Any]:
    """打开 TikTok 主页，截图并提取部分元信息。

    返回 dict: {"url":..., "handle":..., "display_name":...,
                "bio":..., "stats":...,
                "avatar_path": Path|None, "page_path": Path|None}.
    """
    if sync_playwright is None:
        raise RuntimeError("未安装 playwright。请先运行: pip install playwright && playwright install chromium")

    stamp = time.strftime("%Y%m%d_%H%M%S")
    safe_handle = url.rstrip("/").split("/")[-1].lstrip("@") or "profile"
    out_dir = output_dir or (HERE / "screenshots")
    out_dir.mkdir(exist_ok=True)
    tag = f"_{WORKER_TAG}" if WORKER_TAG else ""
    avatar_path = out_dir / f"{safe_handle}_avatar_{stamp}{tag}_v6.png"
    page_path = out_dir / f"{safe_handle}_page_{stamp}{tag}_v6.png"

    display_name: Optional[str] = None
    bio: Optional[str] = None
    stats_text: Optional[str] = None

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        try:
            context = browser.new_context(
                viewport={"width": 1280, "height": 1600},
                user_agent=(
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0 Safari/537.36"
                ),
            )
            page = context.new_page()
        except Exception:
            try:
                browser.close()
            finally:
                raise
        print(f"  [snap] 打开 {url}")
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
        except Exception:
            try:
                browser.close()
            finally:
                raise
        try:
            page.wait_for_load_state("networkidle", timeout=30_000)
        except Exception:
            pass
        # 检测页面是否不可用（private / 不存在 / 被封禁等）
        page_text = ""
        try:
            page_text = page.inner_text("body").lower()
        except Exception:
            pass
        unavailable_keywords = [
            "this account is private",
            "account is private",
            "couldn't find this account",
            "could not find this account",
            "account banned",
            "account suspended",
            "age restricted",
            "content unavailable",
            "this page isn't available",
            "page not found",
            "something went wrong",
            # 观众管理功能（audience control）登录墙：中英文文案
            "观众管理功能",
            "该创作者启用了观众管理",
            "audience control",
            "enabled audience controls",
            "turned on audience controls",
        ]
        for kw in unavailable_keywords:
            if kw in page_text:
                browser.close()
                raise PageUnavailableError(f"页面不可用: 检测到 '{kw}'")

        # 分段渐进滚动，触发视频封面懒加载，再等图片真正解码完成
        try:
            _scroll_to_load_lazy_images(page)
            _wait_for_images_decoded(page, timeout_ms=40_000)
            # 回到顶部，保证整页截图从头开始
            page.evaluate("window.scrollTo(0, 0)")
            time.sleep(2.0)
        except Exception:
            pass

        # 头像截图（宽 >= 80 的第一张图）
        try:
            imgs = page.locator("img")
            count = imgs.count()
            for i in range(min(count, 20)):
                el = imgs.nth(i)
                try:
                    box = el.bounding_box()
                    if box and box["width"] >= 80 and box["height"] >= 80:
                        el.scroll_into_view_if_needed(timeout=3000)
                        el.screenshot(path=str(avatar_path))
                        if avatar_path.exists() and avatar_path.stat().st_size >= 500:
                            break
                except Exception:
                    continue
        except Exception:
            pass

        # 元信息提取（用 try/except，无则 None）
        try:
            display_name = (page.title() or "").strip() or None
        except Exception:
            pass
        try:
            # 尝试选一些常见元素拿 bio/统计信息
            nodes = page.locator("h1, h2, [data-e2e*='bio'], [data-e2e*='user-bio']")
            if nodes.count() > 0:
                parts = []
                for i in range(min(nodes.count(), 3)):
                    try:
                        parts.append(nodes.nth(i).inner_text(timeout=1500).strip())
                    except Exception:
                        pass
                if parts:
                            bio = parts[0]
                            if len(parts) > 1:
                                stats_text = " / ".join(parts[1:3])[:300] or None
        except Exception:
            pass

        # 整页截图：限制最大高度为 4800px，避免视频多的用户截图超级大
        MAX_SCREENSHOT_HEIGHT = 4800
        try:
            page_height = int(page.evaluate("document.body.scrollHeight") or 0)
            if page_height > MAX_SCREENSHOT_HEIGHT:
                # 高度超限时：不使用 full_page，改为手动指定 clip 区域
                page_width = int(page.evaluate("document.documentElement.clientWidth") or 1280)
                page.screenshot(
                    path=str(page_path),
                    clip={"x": 0, "y": 0, "width": page_width, "height": MAX_SCREENSHOT_HEIGHT},
                )
                print(f"  [snap] 页面高度 {page_height}px > {MAX_SCREENSHOT_HEIGHT}px，截前 {MAX_SCREENSHOT_HEIGHT}px")
            else:
                page.screenshot(path=str(page_path), full_page=True)
        except Exception:
            try:
                page.screenshot(path=str(page_path))
            except Exception:
                pass
        finally:
            try:
                browser.close()
            except Exception:
                pass

    return {
        "url": url,
        "handle": safe_handle,
        "display_name": display_name,
        "bio": bio,
        "stats": stats_text,
        "avatar_path": avatar_path if (avatar_path.exists() and avatar_path.stat().st_size >= 300) else page_path,
        "page_path": page_path if page_path.exists() else None,
    }


# --------------------
# LLM 调用（图片体积检测 + 自动压缩，只在超限时处理）
# --------------------

# 单张图 base64 上限：5MB/base64 ≈ 3.75MB 原图。两张合计约 8MB，
# 再留 2MB 给 text/prompt/JSON，整请求稳稳在 10MB 以内。
_MAX_B64_BYTES_PER_IMAGE = 5 * 1024 * 1024


def _image_to_base64(path: Path) -> str:
    """直接读文件转 base64（不做任何处理），仅作为兜底工具。"""
    import base64
    with open(path, "rb") as f:
        return base64.b64encode(f.read()).decode("ascii")


def _b64_size_bytes(b64: str) -> int:
    return len(b64) * 3 // 4


def _prepare_image_for_llm(path: Path) -> Tuple[str, str]:
    """为 LLM 调用准备一张图片。

    策略（先检测，超出才处理）：
      1. 先读原 PNG base64，若单张 <= 5MB/base64，直接用 — 完全无损。
      2. 超限时才在内存中转 JPEG，从 quality=92（几乎不可见损失）起尝试，
         只在必要时下调 quality 或缩边长。
      3. 返回 (base64, mime_type)，原 PNG 落盘文件保持不变。
    """
    path = Path(str(path))
    raw_b64 = _image_to_base64(path)
    if _b64_size_bytes(raw_b64) <= _MAX_B64_BYTES_PER_IMAGE:
        return raw_b64, "image/png"

    print(f"  [compress] {path.name} 原始 base64 "
          f"≈{_b64_size_bytes(raw_b64)/1024/1024:.2f} MB，超限 → 转 JPEG 压缩")

    try:
        from PIL import Image  # type: ignore
    except Exception as e:
        raise RuntimeError(
            f"图片过大（>{_MAX_B64_BYTES_PER_IMAGE/1024/1024:.0f}MB/base64）"
            f" 但缺少 Pillow。请: pip install Pillow。原错误: {e}"
        )

    import base64
    import io

    with Image.open(path) as im:
        im = im.convert("RGB")
        # 从高保真开始，只在必要时降级
        stages = [
            (92, None), (90, None), (88, None), (85, None),
            (82, None), (80, None), (78, None), (75, None),
            (90, 2000), (85, 1800), (80, 1600), (75, 1400),
            (75, 1200), (70, 1000), (65, 800),
        ]
        for q, side in stages:
            tmp = im
            if side is not None and max(tmp.size) > side:
                ratio = side / max(tmp.size)
                new_size = (int(tmp.size[0] * ratio), int(tmp.size[1] * ratio))
                tmp = tmp.resize(new_size, Image.LANCZOS)
            buf = io.BytesIO()
            tmp.save(buf, format="JPEG", quality=q, optimize=True)
            jpeg_b64 = base64.b64encode(buf.getvalue()).decode("ascii")
            if _b64_size_bytes(jpeg_b64) <= _MAX_B64_BYTES_PER_IMAGE:
                side_note = f", size={tmp.size}" if side is not None else ""
                print(f"  [compress]   ok: quality={q}{side_note}, "
                      f"base64≈{_b64_size_bytes(jpeg_b64)/1024/1024:.2f} MB")
                return jpeg_b64, "image/jpeg"

        # 兜底：quality=50 + 最长边 800
        tmp = im
        if max(tmp.size) > 800:
            ratio = 800 / max(tmp.size)
            tmp = tmp.resize(
                (int(tmp.size[0] * ratio), int(tmp.size[1] * ratio)),
                Image.LANCZOS,
            )
        buf = io.BytesIO()
        tmp.save(buf, format="JPEG", quality=50, optimize=True)
        jpeg_b64 = base64.b64encode(buf.getvalue()).decode("ascii")
        print(f"  [compress]   fallback: quality=50, size={tmp.size}, "
              f"base64≈{_b64_size_bytes(jpeg_b64)/1024/1024:.2f} MB")
        return jpeg_b64, "image/jpeg"


def _clean_json_response(raw: str) -> Dict[str, Any]:
    raw = (raw or "").strip()
    # 先剥掉 markdown 代码围栏（```json ... ``` / ``` ... ```），
    # gemini 等模型常把 JSON 包在围栏里，导致直接 json.loads 失败。
    fence = re.search(r"```(?:json)?\s*(.*?)\s*```", raw, re.DOTALL | re.IGNORECASE)
    if fence:
        raw = fence.group(1).strip()
    else:
        # 没有成对围栏（可能被截断只剩开头 ```json），手动去掉残留反引号/前缀
        raw = raw.strip("`").strip()
        if raw.lower().startswith("json"):
            raw = raw[4:].strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        start, end = raw.find("{"), raw.rfind("}")
        if start >= 0 and end > start:
            try:
                return json.loads(raw[start: end + 1])
            except json.JSONDecodeError:
                pass
    return {}


def _build_user_prompt(snap: Dict[str, Any]) -> str:
    meta = (
        f"TikTok handle: @{snap.get('handle') or 'N/A'}\n"
        f"显示名: {snap.get('display_name') or 'N/A'}\n"
        f"简介: {snap.get('bio') or '(无简介)'}\n"
        f"主页统计: {snap.get('stats') or '(无统计信息)'}"
    )
    return (
        "请基于你将看到的两张 TikTok 主页截图（头像 + 整页）以及下面的元信息，按照 system 指令完成年龄与性别标注，输出严格 JSON。\n\n"
        f"元信息:\n{meta}\n"
    )


def _normalize_confidence(raw: Any) -> str:
    s = str(raw or "").strip().lower()
    return s if s in ("high", "medium", "low") else "medium"


def _parse_bool(raw: Any) -> Optional[bool]:
    if raw is None:
        return None
    if isinstance(raw, bool):
        return raw
    s = str(raw).strip().lower()
    if s in ("true", "1", "yes", "y", "是", "对"):
        return True
    if s in ("false", "0", "no", "n", "否", "不"):
        return False
    return None


def _safe_int(raw: Any) -> Optional[int]:
    if raw is None:
        return None
    if isinstance(raw, int):
        return raw
    try:
        return int(str(raw).strip())
    except Exception:
        return None


def _flatten_text(obj: Any) -> str:
    if obj is None:
        return ""
    if isinstance(obj, str):
        return obj
    if isinstance(obj, (int, float, bool)):
        return str(obj)
    if isinstance(obj, list):
        return "\n".join(_flatten_text(x) for x in obj if x is not None)
    if isinstance(obj, dict):
        parts = []
        for k, v in obj.items():
            if v is None:
                continue
            parts.append(f"{k}: {_flatten_text(v)}")
        return "\n".join(parts)
    return str(obj)


_STRONG_45PLUS_TRIGGERS = [
    "花白", "灰白", "白发", "满头白发",
    "老年斑", "驼背", "退休", "孙辈", "孙子", "孙女",
    "grandma", "grandpa", "retire", "retired", "grandchild",
    "deep wrinkles", "age spots",
]


def _has_45plus_trigger(parsed: Dict[str, Any]) -> bool:
    ev = parsed.get("evidence") if isinstance(parsed.get("evidence"), dict) else {}
    b = _parse_bool(ev.get("has_45plus_trigger"))
    if b is not None:
        return b
    triggers = ev.get("hard_triggers_45_plus")
    if isinstance(triggers, list) and len(triggers) > 0:
        return True
    blob = ("\n".join([
        _flatten_text(ev),
        str(parsed.get("reasoning") or ""),
    ])).lower()
    return any(t.lower() in blob for t in _STRONG_45PLUS_TRIGGERS)


def _aging_signal_count(parsed: Dict[str, Any]) -> Optional[int]:
    ev = parsed.get("evidence") if isinstance(parsed.get("evidence"), dict) else {}
    n = _safe_int(ev.get("aging_signal_count"))
    if n is not None and 0 <= n <= 20:
        return n
    s = str(ev.get("aging_signals") or "")
    if not s or s.strip().lower() in ("无", "none", "n/a"):
        return 0
    hits = 0
    for kw in ("鱼尾纹", "法令纹", "额头纹", "皮肤松弛", "下颌", "颈纹", "白发", "老年斑", "驼背"):
        if kw in s:
            hits += 1
    return hits if hits > 0 else None


_BOUNDARY_45PLUS_SYSTEM_PROMPT = (
    "你是一名年龄边界复核员，只能基于两张主页截图与元信息做判断。\n"
    "你只回答一个问题：该账号年龄 tier1 应该是 25-44 还是 45-55+？\n"
    "必须优先识别 45-55+ 的硬触发：灰白发/花白发、面部深褶皱、明显皮肤松弛或双下巴、老年斑成片、明显驼背、孙辈出镜或退休生活内容。\n"
    "输出严格 JSON，不要输出额外文字。schema: {\"verdict\":\"25-44\"|\"45-55+\",\"confidence\":\"high\"|\"medium\"|\"low\",\"hard_triggers\":[...],\"reasoning\":\"一句话中文\"}"
)


def _call_vision_llm(system_prompt: str,
                    user_prompt: str,
                    avatar_mime: str,
                    avatar_b64: str,
                    page_mime: str,
                    page_b64: str,
                    temperature: float,
                    max_tokens: int) -> str:
    provider = (os.getenv("LLM_PROVIDER") or "").lower()

    mh_ak = os.getenv("MODELHUB_V2_AK")
    mh_model = os.getenv("MODELHUB_V2_MODEL")
    mh_base = os.getenv("MODELHUB_V2_BASE_URL")

    if provider == "modelhub_v2":
        if not mh_ak:
            raise RuntimeError("LLM_PROVIDER=modelhub_v2 但缺少 MODELHUB_V2_AK")
        if httpx is None:
            raise RuntimeError("缺少 httpx 依赖")
        base = mh_base or "https://aidp.bytedance.net"
        url = base.rstrip("/") + "/api/modelhub/online/v2/crawl"
        headers = {"Content-Type": "application/json",
                   "X-TT-LOGID": os.getenv("MODELHUB_LOGID") or f"tiktok_v6_{int(time.time())}"}
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": [
                {"type": "text", "text": user_prompt},
                {"type": "image_url", "image_url": {"url": f"data:{avatar_mime};base64," + avatar_b64}},
                {"type": "image_url", "image_url": {"url": f"data:{page_mime};base64," + page_b64}},
            ]},
        ]

        print("  [llm] 调用 ModelHub v2 视觉模型 ...")
        MAX_RETRIES = 3
        last_exc: Optional[BaseException] = None
        data: Any = None
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                with httpx.Client(timeout=httpx.Timeout(connect=5.0, read=90.0, write=20.0, pool=5.0), verify=True) as http:
                    r = http.post(url, params={"ak": mh_ak}, headers=headers,
                                  json={"stream": False, "messages": messages,
                                        "model": mh_model or "Doubao-1.5-thinking-vision-pro-250428",
                                        "max_tokens": max_tokens, "temperature": temperature})
                if r.status_code == 200:
                    data = r.json()
                    break
                if 400 <= r.status_code < 500 and attempt < MAX_RETRIES:
                    print(f"  [llm] attempt {attempt}/{MAX_RETRIES}: HTTP {r.status_code}, {r.text[:120]}")
                    time.sleep(2 + attempt)
                    last_exc = RuntimeError(f"ModelHub v2 HTTP {r.status_code}: {r.text[:500]}")
                    continue
                raise RuntimeError(f"ModelHub v2 HTTP {r.status_code}: {r.text[:500]}")
            except (httpx.TimeoutException, httpx.NetworkError, OSError) as e:
                if attempt < MAX_RETRIES:
                    print(f"  [llm] attempt {attempt}/{MAX_RETRIES}: 网络异常 {type(e).__name__}: {e}")
                    time.sleep(2 + attempt)
                    last_exc = e
                    continue
                raise

        raw_text = ""
        if isinstance(data, dict):
            try:
                choice = ((data.get("choices") or [{}])[0] or {}).get("message") or {}
                raw_text = str(choice.get("content") or "").strip()
            except Exception:
                raw_text = ""
        if not raw_text and isinstance(data, dict):
            raw_text = json.dumps(data, ensure_ascii=False)[:2000]
        if not raw_text and last_exc is not None:
            raise RuntimeError(f"ModelHub v2 返回空响应: {type(last_exc).__name__}: {last_exc}")
        return raw_text

    try:
        from openai import OpenAI
    except Exception as e:
        raise RuntimeError("未安装 openai。pip install openai") from e

    api_key = (os.getenv("ARK_API_KEY") or os.getenv("DOUBAO_API_KEY") or os.getenv("OPENAI_API_KEY"))
    base_url = (os.getenv("ARK_BASE_URL") or os.getenv("DOUBAO_BASE_URL") or os.getenv("OPENAI_BASE_URL") or
                "https://ark.cn-beijing.volces.com/api/v3")
    model_name = os.getenv("ARK_MODEL") or os.getenv("DOUBAO_MODEL") or os.getenv("OPENAI_MODEL") or "doubao-seed-2-0-pro-260215"
    if not api_key:
        raise RuntimeError("未配置 ARK_API_KEY 或 OPENAI_API_KEY")

    client = OpenAI(api_key=api_key, base_url=base_url)
    print(f"  [llm] 调用视觉模型 {model_name} ...")
    resp = client.chat.completions.create(
        model=model_name,
        temperature=temperature,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": [
                {"type": "text", "text": user_prompt},
                {"type": "image_url", "image_url": {"url": f"data:{avatar_mime};base64," + avatar_b64}},
                {"type": "image_url", "image_url": {"url": f"data:{page_mime};base64," + page_b64}},
            ]},
        ],
        max_tokens=max_tokens,
    )
    return (resp.choices[0].message.content or "").strip()


def estimate_age_with_llm(snap: Dict[str, Any]) -> Dict[str, Any]:
    """调用视觉模型做年龄/性别推断，返回 dict。"""
    provider = (os.getenv("LLM_PROVIDER") or "").lower()
    user_prompt = _build_user_prompt(snap)

    avatar_path = snap.get("avatar_path") or snap.get("page_path")
    page_path = snap.get("page_path") or snap.get("avatar_path")
    if not avatar_path or not page_path:
        raise RuntimeError("截图路径为空，无法调用 LLM")

    # 先检测：只有超限时才压缩；同时返回 mime_type (png 或 jpeg)
    avatar_b64, avatar_mime = _prepare_image_for_llm(Path(str(avatar_path)))
    page_b64, page_mime = _prepare_image_for_llm(Path(str(page_path)))

    # 优先尝试 ModelHub v2 原生（如果配置存在）
    mh_ak = os.getenv("MODELHUB_V2_AK")
    mh_model = os.getenv("MODELHUB_V2_MODEL")
    mh_base = os.getenv("MODELHUB_V2_BASE_URL")

    raw_text = ""

    if provider == "modelhub_v2":
        if not mh_ak:
            raise RuntimeError("LLM_PROVIDER=modelhub_v2 但缺少 MODELHUB_V2_AK")
        if httpx is None:
            raise RuntimeError("缺少 httpx 依赖")
        base = mh_base or "https://aidp.bytedance.net"
        url = base.rstrip("/") + "/api/modelhub/online/v2/crawl"
        headers = {"Content-Type": "application/json",
                   "X-TT-LOGID": os.getenv("MODELHUB_LOGID") or f"tiktok_v3_{int(time.time())}"}
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": [
                {"type": "text", "text": user_prompt},
                {"type": "image_url", "image_url": {"url": f"data:{avatar_mime};base64," + avatar_b64}},
                {"type": "image_url", "image_url": {"url": f"data:{page_mime};base64," + page_b64}},
            ]},
        ]
        print("  [llm] 调用 ModelHub v2 视觉模型 ...")

        # 最多重试 3 次，网络/网关超时通常重试就能好
        MAX_RETRIES = 3
        last_exc: Optional[BaseException] = None
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                with httpx.Client(timeout=httpx.Timeout(connect=5.0, read=90.0, write=20.0, pool=5.0), verify=True) as http:
                    r = http.post(url, params={"ak": mh_ak}, headers=headers,
                                  json={"stream": False, "messages": messages,
                                        "model": mh_model or "Doubao-1.5-thinking-vision-pro-250428",
                                        "max_tokens": 800, "temperature": 0.0})
                if r.status_code == 200:
                    data = r.json()
                    break
                # 403/5xx: 网关超时/后端异常，可重试
                if 400 <= r.status_code < 500 and attempt < MAX_RETRIES:
                    print(f"  [llm] attempt {attempt}/{MAX_RETRIES}: HTTP {r.status_code}, "
                          f"{r.text[:120]}")
                    time.sleep(2 + attempt)  # 指数退避
                    last_exc = RuntimeError(f"ModelHub v2 HTTP {r.status_code}: {r.text[:500]}")
                    continue
                raise RuntimeError(f"ModelHub v2 HTTP {r.status_code}: {r.text[:500]}")
            except (httpx.TimeoutException, httpx.NetworkError, OSError) as e:
                if attempt < MAX_RETRIES:
                    print(f"  [llm] attempt {attempt}/{MAX_RETRIES}: 网络异常 {type(e).__name__}: {e}")
                    time.sleep(2 + attempt)
                    last_exc = e
                    continue
                raise
        else:
            # 3 次重试全失败：降级到 OpenAI/ARK 兼容端点（如果有 key）
            print("  [llm] ModelHub 连续失败，降级尝试 OpenAI/ARK 兼容端点 ...")
            api_key = (os.getenv("ARK_API_KEY") or os.getenv("DOUBAO_API_KEY")
                       or os.getenv("OPENAI_API_KEY"))
            base_url = (os.getenv("ARK_BASE_URL") or os.getenv("DOUBAO_BASE_URL")
                        or os.getenv("OPENAI_BASE_URL"))
            if not api_key or not base_url:
                raise last_exc or RuntimeError("ModelHub 失败且未配置备用 key")
            from openai import OpenAI
            client = OpenAI(api_key=api_key, base_url=base_url)
            resp = client.chat.completions.create(
                model=(os.getenv("ARK_MODEL") or os.getenv("DOUBAO_MODEL")
                       or os.getenv("OPENAI_MODEL") or "doubao-seed-2-0-pro-260215"),
                temperature=0.0, max_tokens=800,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": [
                        {"type": "text", "text": user_prompt},
                        {"type": "image_url", "image_url": {"url": f"data:{avatar_mime};base64," + avatar_b64}},
                        {"type": "image_url", "image_url": {"url": f"data:{page_mime};base64," + page_b64}},
                    ]},
                ],
            )
            raw_text = (resp.choices[0].message.content or "").strip()
            print("  [llm] 降级端点调用成功")
        # ModelHub 正常路径的结果解析（降级路径已经在上面设置了 raw_text）
        if not raw_text:
            try:
                choices = data.get("choices") or []
                if isinstance(choices, list) and choices and isinstance(choices[0], dict):
                    msg = choices[0].get("message") or {}
                    raw_text = msg.get("content") or ""
            except Exception:
                pass
        if not raw_text:
            for key in ("reply", "output_text", "answer", "response", "result"):
                v = data.get(key) if isinstance(data, dict) else None
                if isinstance(v, str) and v:
                    raw_text = v
                    break
        # 兜底
        if not raw_text and isinstance(data, dict):
            raw_text = json.dumps(data, ensure_ascii=False)[:2000]
    else:
        # 默认走 OpenAI 兼容接口（火山方舟 / Doubao 也走这里）
        try:
            from openai import OpenAI
        except Exception as e:
            raise RuntimeError("未安装 openai。pip install openai") from e

        api_key = (os.getenv("ARK_API_KEY") or os.getenv("DOUBAO_API_KEY") or os.getenv("OPENAI_API_KEY"))
        base_url = (os.getenv("ARK_BASE_URL") or os.getenv("DOUBAO_BASE_URL") or os.getenv("OPENAI_BASE_URL") or
                    "https://ark.cn-beijing.volces.com/api/v3")
        model_name = os.getenv("ARK_MODEL") or os.getenv("DOUBAO_MODEL") or os.getenv("OPENAI_MODEL") or "doubao-seed-2-0-pro-260215"
        if not api_key:
            raise RuntimeError("未配置 ARK_API_KEY 或 OPENAI_API_KEY")

        client = OpenAI(api_key=api_key, base_url=base_url)
        print(f"  [llm] 调用视觉模型 {model_name} ...")
        resp = client.chat.completions.create(
            model=model_name,
            temperature=0.0,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": [
                    {"type": "text", "text": user_prompt},
                    {"type": "image_url", "image_url": {"url": f"data:{avatar_mime};base64," + avatar_b64}},
                    {"type": "image_url", "image_url": {"url": f"data:{page_mime};base64," + page_b64}},
                ]},
            ],
            max_tokens=800,
        )
        raw_text = (resp.choices[0].message.content or "").strip()

    parsed = _clean_json_response(raw_text)
    tier1, tier2, gender = spec_v3.normalize_agent_output(parsed)

    confidence = _normalize_confidence(parsed.get("confidence"))
    reasoning = str(parsed.get("reasoning") or "")
    evidence_blob = _flatten_text(parsed.get("evidence")) + "\n" + _flatten_text(reasoning)

    if _has_45plus_trigger(parsed) and tier1 != "45-55+":
        tier1 = "45-55+"
        tier2 = spec_v3.normalize_tier2(tier1, parsed.get("tier2"), evidence_blob, SPEC)
        if confidence == "high":
            confidence = "medium"
    else:
        enable_verifier = (os.getenv("ENABLE_BOUNDARY_VERIFIER_45PLUS") or "1").strip().lower() not in ("0", "false", "no")
        risk_45plus = (
            tier1 != "45-55+" and (
                _has_45plus_trigger(parsed)
                or ((_aging_signal_count(parsed) or 0) >= 3)
            )
        )
        if enable_verifier and risk_45plus:
            verifier_user_prompt = (
                user_prompt
                + "\n\n[初判输出 JSON（截断）]\n"
                + json.dumps(parsed, ensure_ascii=False)[:1600]
                + "\n\n请仅输出 schema JSON。"
            )
            verifier_raw = ""
            if provider == "modelhub_v2":
                headers = {"Content-Type": "application/json",
                           "X-TT-LOGID": os.getenv("MODELHUB_LOGID") or f"tiktok_v6_verify_{int(time.time())}"}
                base = mh_base or "https://aidp.bytedance.net"
                url = base.rstrip("/") + "/api/modelhub/online/v2/crawl"
                messages = [
                    {"role": "system", "content": _BOUNDARY_45PLUS_SYSTEM_PROMPT},
                    {"role": "user", "content": [
                        {"type": "text", "text": verifier_user_prompt},
                        {"type": "image_url", "image_url": {"url": f"data:{avatar_mime};base64," + avatar_b64}},
                        {"type": "image_url", "image_url": {"url": f"data:{page_mime};base64," + page_b64}},
                    ]},
                ]
                with httpx.Client(timeout=httpx.Timeout(connect=5.0, read=90.0, write=20.0, pool=5.0), verify=True) as http:
                    r = http.post(url, params={"ak": mh_ak}, headers=headers,
                                  json={"stream": False, "messages": messages,
                                        "model": mh_model or "Doubao-1.5-thinking-vision-pro-250428",
                                        "max_tokens": 300, "temperature": 0.0})
                if r.status_code == 200:
                    data2 = r.json()
                    try:
                        choice = ((data2.get("choices") or [{}])[0] or {}).get("message") or {}
                        verifier_raw = str(choice.get("content") or "").strip()
                    except Exception:
                        verifier_raw = ""
            else:
                try:
                    from openai import OpenAI
                except Exception as e:
                    raise RuntimeError("未安装 openai。pip install openai") from e
                api_key = (os.getenv("ARK_API_KEY") or os.getenv("DOUBAO_API_KEY") or os.getenv("OPENAI_API_KEY"))
                base_url = (os.getenv("ARK_BASE_URL") or os.getenv("DOUBAO_BASE_URL") or os.getenv("OPENAI_BASE_URL") or
                            "https://ark.cn-beijing.volces.com/api/v3")
                model_name = os.getenv("ARK_MODEL") or os.getenv("DOUBAO_MODEL") or os.getenv("OPENAI_MODEL") or "doubao-seed-2-0-pro-260215"
                if not api_key:
                    raise RuntimeError("未配置 ARK_API_KEY 或 OPENAI_API_KEY")
                client = OpenAI(api_key=api_key, base_url=base_url)
                resp2 = client.chat.completions.create(
                    model=model_name,
                    temperature=0.0,
                    messages=[
                        {"role": "system", "content": _BOUNDARY_45PLUS_SYSTEM_PROMPT},
                        {"role": "user", "content": [
                            {"type": "text", "text": verifier_user_prompt},
                            {"type": "image_url", "image_url": {"url": f"data:{avatar_mime};base64," + avatar_b64}},
                            {"type": "image_url", "image_url": {"url": f"data:{page_mime};base64," + page_b64}},
                        ]},
                    ],
                    max_tokens=300,
                )
                verifier_raw = (resp2.choices[0].message.content or "").strip()

            verifier_parsed = _clean_json_response(verifier_raw)
            verdict = str(verifier_parsed.get("verdict") or "").strip()
            v_conf = _normalize_confidence(verifier_parsed.get("confidence"))
            if verdict == "45-55+" and v_conf in ("high", "medium"):
                tier1 = "45-55+"
                tier2 = spec_v3.normalize_tier2(tier1, parsed.get("tier2"), evidence_blob, SPEC)
                confidence = v_conf

    asc = _aging_signal_count(parsed)
    if tier1 == "25-44" and tier2 == "25-34" and asc is not None and asc >= 2:
        tier2 = "35-44"
        if confidence == "high":
            confidence = "medium"

    tier2 = spec_v3.normalize_tier2(tier1, tier2, evidence_blob, SPEC)

    return {
        "url": snap["url"],
        "handle": snap.get("handle"),
        "display_name": snap.get("display_name"),
        "bio": snap.get("bio"),
        "stats": snap.get("stats"),
        "avatar_screenshot": str(snap.get("avatar_path")),
        "page_screenshot": str(snap.get("page_path")) if snap.get("page_path") else "",
        "tier1": tier1,
        "tier2": tier2,
        "gender": gender,
        "age": [tier1] + ([tier2] if tier2 else []),
        "final_result": {"age": [tier1] + ([tier2] if tier2 else []), "gender": gender},
        "confidence": confidence,
        "reasoning": reasoning,
        "evidence": parsed.get("evidence"),
        "raw_response": raw_text,
        "raw_parsed": json.dumps(parsed, ensure_ascii=False),
    }


def _agent_names() -> List[str]:
    raw = os.getenv("AGENT_CONFIGS") or "agent_a,agent_b,agent_c"
    names = [x.strip() for x in raw.split(",") if x.strip()]
    return names or ["agent_a", "agent_b", "agent_c"]


def _agent_run_mode() -> AgentRunMode:
    raw = (os.getenv("AGENT_RUN_MODE") or AgentRunMode.PARALLEL.value).strip().lower()
    try:
        return AgentRunMode(raw)
    except ValueError as exc:
        allowed = ", ".join(mode.value for mode in AgentRunMode)
        raise ValueError(f"AGENT_RUN_MODE={raw!r} 无效，可选值: {allowed}") from exc


def _agent_prefix(agent_name: str) -> str:
    return re.sub(r"[^A-Z0-9]+", "_", agent_name.upper()).strip("_")


def _agent_env(agent_name: str, key: str, fallback: Optional[str] = None) -> Optional[str]:
    prefix = _agent_prefix(agent_name)
    return os.getenv(f"{prefix}_{key}") or fallback


def _build_agent_system_prompt(agent_name: str) -> str:
    """所有 agent 使用统一的基础 system prompt（与 V5 相同），不再差异化校准。

    差异只来自各 agent 配置的不同模型本身；AGENT_X_CALIBRATION 已废弃。
    """
    return SYSTEM_PROMPT


def _call_vision_llm_for_agent(agent_name: str,
                               system_prompt: str,
                               user_prompt: str,
                               avatar_mime: str,
                               avatar_b64: str,
                               page_mime: str,
                               page_b64: str) -> str:
    provider = (_agent_env(agent_name, "PROVIDER", os.getenv("LLM_PROVIDER") or "") or "").lower()
    timeout_seconds = float(os.getenv("AGENT_TIMEOUT_SECONDS") or "90")

    if provider != "openai" and provider != "ark":
        if httpx is None:
            raise RuntimeError("缺少 httpx 依赖")
        ak = (
            _agent_env(agent_name, "MODELHUB_V2_AK")
            or _agent_env(agent_name, "AK")
            or os.getenv("MODELHUB_V2_AK")
        )
        if not ak:
            raise RuntimeError(f"{agent_name}: modelhub_v2 缺少 AK")
        base = (
            _agent_env(agent_name, "MODELHUB_V2_BASE_URL")
            or _agent_env(agent_name, "BASE_URL")
            or os.getenv("MODELHUB_V2_BASE_URL")
            or "https://aidp.bytedance.net"
        )
        model = (
            _agent_env(agent_name, "MODELHUB_V2_MODEL")
            or _agent_env(agent_name, "MODEL")
            or os.getenv("MODELHUB_V2_MODEL")
            or "Doubao-1.5-thinking-vision-pro-250428"
        )
        api_path = (
            _agent_env(agent_name, "MODELHUB_V2_API_PATH")
            or os.getenv("MODELHUB_V2_API_PATH")
            or "/api/modelhub/online/v2/crawl"
        )
        url = base.rstrip("/") + api_path
        headers = {"Content-Type": "application/json",
                   "X-TT-LOGID": f"tiktok_v6_{agent_name}_{int(time.time())}"}
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": [
                {"type": "text", "text": user_prompt},
                {"type": "image_url", "image_url": {"url": f"data:{avatar_mime};base64," + avatar_b64}},
                {"type": "image_url", "image_url": {"url": f"data:{page_mime};base64," + page_b64}},
            ]},
        ]
        write_timeout = float(os.getenv("AGENT_WRITE_TIMEOUT_SECONDS") or "60")
        max_retries = max(1, int(os.getenv("AGENT_MAX_RETRIES") or "3"))
        # 输出额度：gemini 等模型思考啰嗦容易把 JSON 写到一半被截断，
        # 默认调高到 4000，可用 AGENT_X_MAX_TOKENS / AGENT_MAX_TOKENS 覆盖。
        max_tokens = int(
            _agent_env(agent_name, "MAX_TOKENS")
            or os.getenv("AGENT_MAX_TOKENS")
            or "4000"
        )
        last_exc: Optional[BaseException] = None
        for attempt in range(1, max_retries + 1):
            try:
                with httpx.Client(timeout=httpx.Timeout(connect=5.0, read=timeout_seconds,
                                                        write=write_timeout, pool=5.0),
                                  verify=True) as http:
                    r = http.post(url, params={"ak": ak}, headers=headers,
                                  json={"stream": False, "messages": messages, "model": model,
                                        "max_tokens": max_tokens})
                if r.status_code == 200:
                    data = r.json()
                    try:
                        return str(((data.get("choices") or [{}])[0].get("message") or {}).get("content") or "").strip()
                    except Exception:
                        return json.dumps(data, ensure_ascii=False)[:2000]
                if 400 <= r.status_code < 500 and attempt < max_retries:
                    last_exc = RuntimeError(f"{agent_name}: ModelHub v2 HTTP {r.status_code}: {r.text[:500]}")
                    print(f"  [v6:{agent_name}] attempt {attempt}/{max_retries}: HTTP {r.status_code}")
                    time.sleep(2 + attempt)
                    continue
                raise RuntimeError(f"{agent_name}: ModelHub v2 HTTP {r.status_code}: {r.text[:500]}")
            except (httpx.TimeoutException, httpx.NetworkError, OSError) as e:
                last_exc = e
                if attempt < max_retries:
                    print(f"  [v6:{agent_name}] attempt {attempt}/{max_retries}: 网络异常 {type(e).__name__}: {e}")
                    time.sleep(2 + attempt)
                    continue
                raise
        raise RuntimeError(f"{agent_name}: ModelHub v2 调用失败: {type(last_exc).__name__}: {last_exc}")

    try:
        from openai import OpenAI
    except Exception as e:
        raise RuntimeError("未安装 openai。pip install openai") from e

    api_key = (
        _agent_env(agent_name, "API_KEY")
        or _agent_env(agent_name, "ARK_API_KEY")
        or os.getenv("ARK_API_KEY")
        or os.getenv("DOUBAO_API_KEY")
        or os.getenv("OPENAI_API_KEY")
    )
    base_url = (
        _agent_env(agent_name, "BASE_URL")
        or _agent_env(agent_name, "ARK_BASE_URL")
        or os.getenv("ARK_BASE_URL")
        or os.getenv("DOUBAO_BASE_URL")
        or os.getenv("OPENAI_BASE_URL")
        or "https://ark.cn-beijing.volces.com/api/v3"
    )
    model_name = (
        _agent_env(agent_name, "MODEL")
        or _agent_env(agent_name, "ARK_MODEL")
        or os.getenv("ARK_MODEL")
        or os.getenv("DOUBAO_MODEL")
        or os.getenv("OPENAI_MODEL")
        or "doubao-seed-2-0-pro-260215"
    )
    if not api_key:
        raise RuntimeError(f"{agent_name}: 未配置 API_KEY/ARK_API_KEY/OPENAI_API_KEY")

    client = OpenAI(api_key=api_key, base_url=base_url, timeout=timeout_seconds)
    resp = client.chat.completions.create(
        model=model_name,
        temperature=0.0,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": [
                {"type": "text", "text": user_prompt},
                {"type": "image_url", "image_url": {"url": f"data:{avatar_mime};base64," + avatar_b64}},
                {"type": "image_url", "image_url": {"url": f"data:{page_mime};base64," + page_b64}},
            ]},
        ],
        max_tokens=900,
    )
    return (resp.choices[0].message.content or "").strip()


def _parse_age_list(raw: Any) -> Tuple[str, Optional[str]]:
    if raw is None:
        return SPEC["age"].get("unknown_label", "Unknown"), None
    return spec_v3.parse_age_tag(raw, SPEC)


def _normalize_agent_vote(agent_name: str, parsed: Dict[str, Any], raw_text: str) -> Dict[str, Any]:
    """把模型按 V5 schema 的输出规范化为统一投票结构（tier1/tier2/gender/confidence）。"""
    ev = parsed.get("evidence") if isinstance(parsed.get("evidence"), dict) else {}
    # 兼容：优先 tier1/tier2；没有则回退到 age / top1_age 数组
    raw_age = parsed.get("tier1")
    if raw_age is None:
        raw_age = parsed.get("age") or parsed.get("top1_age")
        tier1, tier2 = _parse_age_list(raw_age)
    else:
        tier1, tier2 = _parse_age_list([parsed.get("tier1"), parsed.get("tier2")])
    age = [tier1] + ([tier2] if tier2 else [])
    gender = spec_v3.normalize_gender(parsed.get("gender"), SPEC)
    return {
        "agent": agent_name,
        "age": age,
        "tier1": tier1,
        "tier2": tier2,
        "gender": gender,
        "confidence": _normalize_confidence(parsed.get("confidence")),
        "final_result": {"age": age, "gender": gender},
        "evidence": ev,
        "reasoning": str(parsed.get("reasoning") or ""),
        "raw_response": raw_text,
        "raw_parsed": parsed,
    }


def _run_one_agent(agent_name: str,
                   user_prompt: str,
                   avatar_mime: str,
                   avatar_b64: str,
                   page_mime: str,
                   page_b64: str) -> Dict[str, Any]:
    try:
        system_prompt = _build_agent_system_prompt(agent_name)
        raw_text = _call_vision_llm_for_agent(
            agent_name, system_prompt, user_prompt,
            avatar_mime, avatar_b64, page_mime, page_b64,
        )
        parsed = _clean_json_response(raw_text)
        return _normalize_agent_vote(agent_name, parsed, raw_text)
    except Exception as exc:  # noqa: BLE001
        return {
            "agent": agent_name,
            "age": ["error"],
            "tier1": "error",
            "tier2": None,
            "gender": "error",
            "confidence": "low",
            "final_result": {"age": ["error"], "gender": "error"},
            "error": f"{type(exc).__name__}: {exc}"[:500],
        }


def _run_agents(agent_names: List[str],
                user_prompt: str,
                avatar_mime: str,
                avatar_b64: str,
                page_mime: str,
                page_b64: str) -> Dict[str, Dict[str, Any]]:
    mode = _agent_run_mode()
    votes: Dict[str, Dict[str, Any]] = {}

    def run_one(name: str) -> Dict[str, Any]:
        vote = _run_one_agent(
            name, user_prompt, avatar_mime, avatar_b64, page_mime, page_b64,
        )
        print(f"  [v6:{name}] age={vote.get('age')} gender={vote.get('gender')} "
              f"conf={vote.get('confidence')}"
              + (f" err={vote.get('error')}" if vote.get("error") else ""))
        return vote

    if mode == AgentRunMode.HIGH_CONFIDENCE_GATE:
        if len(agent_names) != 2:
            raise ValueError(
                "AGENT_RUN_MODE=high_confidence_gate 要求 AGENT_CONFIGS 恰好配置两个 agent"
            )
        first, second = agent_names
        print(f"  [v6] high confidence 门控调用: {first} -> {second}")
        votes[first] = run_one(first)
        first_is_high = (
            not votes[first].get("error")
            and votes[first].get("confidence") == "high"
        )
        if first_is_high:
            votes[second] = run_one(second)
        else:
            reason = (
                "首个 agent 调用失败"
                if votes[first].get("error")
                else f"首个 agent confidence={votes[first].get('confidence')}"
            )
            votes[second] = {
                "agent": second,
                "age": None,
                "tier1": None,
                "tier2": None,
                "gender": None,
                "confidence": None,
                "final_result": None,
                "skipped": True,
                "skip_reason": reason,
            }
            print(f"  [v6:{second}] skipped: {reason}")
        return votes

    max_workers = max(
        1,
        min(len(agent_names), int(os.getenv("NUM_AGENTS") or len(agent_names) or 1)),
    )
    print(f"  [v6] 并行调用 {len(agent_names)} 个 agent: {', '.join(agent_names)}")
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        future_map = {
            pool.submit(
                _run_one_agent,
                name,
                user_prompt,
                avatar_mime,
                avatar_b64,
                page_mime,
                page_b64,
            ): name
            for name in agent_names
        }
        for fut in as_completed(future_map):
            name = future_map[fut]
            votes[name] = fut.result()
            vote = votes[name]
            print(f"  [v6:{name}] age={vote.get('age')} gender={vote.get('gender')} "
                  f"conf={vote.get('confidence')}"
                  + (f" err={vote.get('error')}" if vote.get("error") else ""))
    return votes


# --------------------
# 可配的分层一致性判定
# --------------------
def _consensus_thresholds(num_agents: int) -> List[int]:
    """读取可配的分层一致性阈值。

    CONSENSUS_LEVELS    -> 一致分层的总层数（决定有几个等级）。
    CONSENSUS_THRESHOLDS-> 逐层"达成一致所需最少 agent 数"，从等级1（最严）到等级N，
                           逗号分隔。长度应等于 CONSENSUS_LEVELS。

    返回长度为 levels 的阈值列表，索引 i 对应等级 i+1。阈值会被 clamp 到 [1, num_agents]。
    缺省/非法时回退到合理默认（等级1=全体一致，等级2=多数即 (n//2+1)）。
    """
    raw_levels = (os.getenv("CONSENSUS_LEVELS") or "").strip()
    raw_thresholds = (os.getenv("CONSENSUS_THRESHOLDS") or "").strip()

    thresholds: List[int] = []
    if raw_thresholds:
        for part in raw_thresholds.split(","):
            part = part.strip()
            if not part:
                continue
            try:
                thresholds.append(int(part))
            except ValueError:
                continue

    levels: Optional[int] = None
    if raw_levels:
        try:
            levels = int(raw_levels)
        except ValueError:
            levels = None

    if levels is None:
        levels = len(thresholds) if thresholds else 2

    if not thresholds:
        # 默认：等级1=全体一致，其余层=多数
        majority = max(1, num_agents // 2 + 1)
        thresholds = [num_agents] + [majority] * (levels - 1)

    # 对齐到 levels：过长截断，过短用末位补齐
    if len(thresholds) < levels:
        thresholds = thresholds + [thresholds[-1]] * (levels - len(thresholds))
    elif len(thresholds) > levels:
        thresholds = thresholds[:levels]

    # clamp 到 [1, num_agents]
    return [min(max(1, t), max(1, num_agents)) for t in thresholds]


def _level_from_agreement(max_agreement: int, thresholds: List[int]) -> int:
    """给定"投相同标签的最大群体票数"，返回命中的最严等级数字；都不满足返回 0。"""
    for i, need in enumerate(thresholds, start=1):
        if max_agreement >= need:
            return i
    return 0


def _max_agreement(keys: List[Any]) -> int:
    """一组标签里，出现次数最多的那个标签的票数。空列表返回 0。"""
    if not keys:
        return 0
    counts: Dict[Any, int] = {}
    for k in keys:
        counts[k] = counts.get(k, 0) + 1
    return max(counts.values())


def _tier1_consensus_level(votes: Dict[str, Dict[str, Any]], thresholds: List[int]) -> int:
    """tier1 一致等级：只看 tier1 字符串，统计成功返回的票。"""
    keys = [
        v.get("tier1") or "Unknown"
        for v in votes.values()
        if not v.get("error") and not v.get("skipped")
    ]
    return _level_from_agreement(_max_agreement(keys), thresholds)


def _tier2_consensus_level(votes: Dict[str, Dict[str, Any]], thresholds: List[int]) -> int:
    """tier2 一致等级：tier1 + tier2 都相同才算同一群体，统计成功返回的票。"""
    keys = [
        (v.get("tier1") or "Unknown", v.get("tier2"))
        for v in votes.values()
        if not v.get("error") and not v.get("skipped")
    ]
    return _level_from_agreement(_max_agreement(keys), thresholds)


def _single_agent_result(snap: Dict[str, Any], vote: Dict[str, Any]) -> Dict[str, Any]:
    """单 agent 模式：输出结构与 V5 完全一致（无共识/分层/各 agent 列）。

    仅处理成功投票；调用失败时由上游 re-raise 交给 run() 的错误处理，
    使错误结构也与 V5 完全一致。
    """
    tier1 = vote.get("tier1") or "Unknown"
    tier2 = vote.get("tier2")
    gender = vote.get("gender") or "unknown"
    age = [tier1] + ([tier2] if tier2 else [])
    raw_parsed = vote.get("raw_parsed") or {}
    return {
        "url": snap["url"],
        "handle": snap.get("handle"),
        "display_name": snap.get("display_name"),
        "bio": snap.get("bio"),
        "stats": snap.get("stats"),
        "avatar_screenshot": str(snap.get("avatar_path")),
        "page_screenshot": str(snap.get("page_path")) if snap.get("page_path") else "",
        "tier1": tier1,
        "tier2": tier2,
        "gender": gender,
        "age": age,
        "final_result": {"age": age, "gender": gender},
        "confidence": vote.get("confidence") or "medium",
        "reasoning": vote.get("reasoning") or "",
        "evidence": raw_parsed.get("evidence") if isinstance(raw_parsed, dict) else None,
        "raw_response": vote.get("raw_response") or "",
        "raw_parsed": json.dumps(raw_parsed, ensure_ascii=False),
    }


def estimate_age_with_llm_v6(snap: Dict[str, Any]) -> Dict[str, Any]:
    """V6: 统一 prompt 调用 N 个 agent，输出统一为 V5 结构，再算可配的分层一致等级。

    - 单 agent：输出与 V5 完全一致。
    - 多 agent：输出 tier1_consensus_level / tier2_consensus_level（整数等级）
      + 每个 agent 的 {age, final_result, confidence}。
    """
    user_prompt = _build_user_prompt(snap)
    avatar_path = snap.get("avatar_path") or snap.get("page_path")
    page_path = snap.get("page_path") or snap.get("avatar_path")
    if not avatar_path or not page_path:
        raise RuntimeError("截图路径为空，无法调用 LLM")

    avatar_b64, avatar_mime = _prepare_image_for_llm(Path(str(avatar_path)))
    page_b64, page_mime = _prepare_image_for_llm(Path(str(page_path)))

    agent_names = _agent_names()
    votes = _run_agents(
        agent_names, user_prompt, avatar_mime, avatar_b64, page_mime, page_b64,
    )

    # 单 agent：完全等同 V5。失败时 re-raise，让 run() 产出 V5 结构的错误结果。
    if len(agent_names) == 1:
        only = votes[agent_names[0]]
        if only.get("error"):
            raise RuntimeError(only["error"])
        return _single_agent_result(snap, only)

    # 多 agent：可配的分层一致等级
    thresholds = _consensus_thresholds(len(agent_names))
    tier1_level = _tier1_consensus_level(votes, thresholds)
    tier2_level = _tier2_consensus_level(votes, thresholds)

    agent_results: Dict[str, Dict[str, Any]] = {}
    for name in agent_names:
        v = votes.get(name, {})
        agent_results[name] = {
            "age": v.get("age"),
            "final_result": v.get("final_result"),
            "confidence": v.get("confidence"),
            "status": "skipped" if v.get("skipped") else ("error" if v.get("error") else "completed"),
        }
        if v.get("skip_reason"):
            agent_results[name]["skip_reason"] = v["skip_reason"]

    # 多数/全部 agent 调用失败时，结果是"假 Unknown"（网络/超时所致，非真判不出）。
    # 标记顶层 error 让 batch 计入 err 并便于重跑。
    executed_votes = {
        name: vote for name, vote in votes.items() if not vote.get("skipped")
    }
    error_agents = {
        name: vote.get("error")
        for name, vote in executed_votes.items()
        if vote.get("error")
    }
    sample_error = ""
    if error_agents and len(error_agents) > len(executed_votes) / 2:
        sample_error = "多数 agent 调用失败: " + "; ".join(
            f"{name}:{err}" for name, err in error_agents.items()
        )[:500]

    result = {
        "url": snap["url"],
        "handle": snap.get("handle"),
        "display_name": snap.get("display_name"),
        "bio": snap.get("bio"),
        "stats": snap.get("stats"),
        "avatar_screenshot": str(snap.get("avatar_path")),
        "page_screenshot": str(snap.get("page_path")) if snap.get("page_path") else "",
        "tier1_consensus_level": tier1_level,
        "tier2_consensus_level": tier2_level,
        "agent_results": agent_results,
        "agent_votes_raw": votes,
    }
    if sample_error:
        result["error"] = sample_error
    return result


# --------------------
# 主流程
# --------------------
def _save_result_json(result: Dict[str, Any]) -> Path:
    """把单条结果写成 JSON 文件并返回路径。文件名: {handle}_{timestamp}{tag}_v6.json"""
    handle = result.get("handle") or "profile"
    safe_handle = re.sub(r'[\\/:*?"<>|]', "_", str(handle))
    tag = f"_{WORKER_TAG}" if WORKER_TAG else ""
    out_json = OUTPUT_DIR / f"{safe_handle}_{time.strftime('%Y%m%d_%H%M%S')}{tag}_v6.json"
    # Path 对象转为 str，确保 JSON 可序列化
    serializable = {}
    for k, v in result.items():
        if isinstance(v, Path):
            serializable[k] = str(v)
        else:
            serializable[k] = v
    out_json.write_text(json.dumps(serializable, ensure_ascii=False, indent=2), encoding="utf-8")
    return out_json


def run(url: str, headless: bool = True, retry: int = 1) -> Dict[str, Any]:
    u = _validate_url(url)
    last_err: Optional[BaseException] = None
    for attempt in range(max(0, int(retry)) + 1):
        try:
            snap = capture_profile(u, headless=headless)
            result = estimate_age_with_llm_v6(snap)
            # 为每条 URL 生成一个 JSON 文件（与 v2 一致）
            out_path = _save_result_json(result)
            print(f"  [json] 已落盘: {out_path}")
            return result
        except PageUnavailableError as exc:
            # 页面不可用（private/不存在等），不重试，直接返回 unavailable
            last_err = exc
            break
        except Exception as exc:  # noqa: BLE001
            last_err = exc
            print(f"  ! run attempt {attempt+1} failed: {type(exc).__name__}: {exc}")

    single_mode = len(_agent_names()) == 1
    state = "unavailable" if isinstance(last_err, PageUnavailableError) else "error"
    err_text = f"{type(last_err).__name__}: {last_err}"[:500] if last_err else ""

    if single_mode:
        # 单 agent：错误结构与 V5 完全一致
        error_result = {
            "url": u,
            "handle": None,
            "display_name": None,
            "bio": None,
            "age": [state],
            "final_result": {"age": [state], "gender": state},
            "tier1": state,
            "tier2": None,
            "gender": state,
            "error": err_text,
        }
    else:
        # 多 agent：分层一致等级置 0，无 agent 结果
        error_result = {
            "url": u,
            "handle": None,
            "display_name": None,
            "bio": None,
            "avatar_screenshot": "",
            "page_screenshot": "",
            "tier1_consensus_level": 0,
            "tier2_consensus_level": 0,
            "agent_results": {},
            "error": err_text,
        }
    out_path = _save_result_json(error_result)
    print(f"  [json] 错误结果已落盘: {out_path}")
    return error_result


def main() -> int:
    parser = argparse.ArgumentParser(description="TikTok 账号年龄/性别标注 v3")
    parser.add_argument("url", nargs="?")
    parser.add_argument("--headless", action="store_true", default=True)
    parser.add_argument("--retry", type=int, default=1)
    args = parser.parse_args()

    if not args.url:
        print("请提供 TikTok URL。")
        return 2
    result = run(args.url, headless=args.headless, retry=args.retry)
    print(json.dumps({k: (str(v) if isinstance(v, Path) else v)
                        for k, v in result.items()},
                  ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
