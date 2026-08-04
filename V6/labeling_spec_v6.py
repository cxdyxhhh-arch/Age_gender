"""labeling_spec_v6 的 Python 工具层。

用法示例:
    import labeling_spec_v6 as spec
    s = spec.load_spec()                    # 读 spec
    print(spec.build_system_prompt(s))        # 得到给模型的 system prompt
    print(spec.build_user_prompt(s, snap))  # 得到用户 prompt 片段（含 tier + heuristics）

    # 规范化
    print(spec.normalize_tier1("25-44"))                  # "25-44"
    print(spec.normalize_tier1("25-44岁"))                 # "25-44"
    print(spec.normalize_tier2("25-44", "35-44"))          # "35-44"
    print(spec.normalize_tier2("25-44", "妈"))             # "35-44"（命中关键词回退）
    print(spec.normalize_gender("女"))                      # "female"

    # 把 age list / dict parse 成 (tier1, tier2)
    print(spec.parse_age_tag(["25-44", "35-44"]))          # ("25-44", "35-44")
    print(spec.parse_age_tag({"age":["25-44","35-44"],     # -> ("25-44", "35-44") / "female"
                              "gender":"female"}))

    # 工具: 把一条模型输出 (raw dict) 规范化到最终 (tier1, tier2, gender)
    tier1, tier2, gender = spec.normalize_agent_output(
        {"tier1":"25-44岁","tier2":"35-44","gender":"女","reasoning":"妈妈账号"}
    )
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

HERE = Path(__file__).resolve().parent
DEFAULT_SPEC_PATH = HERE / "labeling_spec_v6.json"


# ---------------------- load ---------------------- #
def load_spec(path: Optional[Path] = None) -> Dict[str, Any]:
    """读取 labeling_spec_v6.json。"""
    with open(path or DEFAULT_SPEC_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


# ---------------------- normalize ---------------------- #
def _clean_key(s: Optional[str]) -> str:
    if s is None:
        return ""
    return re.sub(r"[\s\-_\"'`，。！？、·（）()]+", "", str(s)).strip().lower()


def normalize_gender(raw: Any, spec: Optional[Dict] = None) -> str:
    if raw is None:
        return "unknown"
    s = str(raw).strip().lower()
    if not s:
        return "unknown"
    if spec is None:
        spec = load_spec()
    aliases = (spec or load_spec()).get("gender", {}).get("normalize_aliases", {})
    # 1) 严格
    if s in aliases:
        return aliases[s]
    # 2) 清理后匹配
    key = _clean_key(s)
    for k, v in aliases.items():
        if _clean_key(k) == key:
            return v
    # 3) 关键词兜底
    if any(x in s for x in ("女", "female", "woman", "girl", "she", "her", "miss", "mrs")):
        return "female"
    if any(x in s for x in ("男", "male", "man", "boy", "he", "him", "mr")):
        return "male"
    return "unknown"


def _tier1_defs(spec: Dict) -> List[Dict]:
    return spec.get("age", {}).get("tiers", [])


def normalize_tier1(raw: Any, spec: Optional[Dict] = None) -> str:
    if spec is None:
        spec = load_spec()
    unknown_label = spec["age"].get("unknown_label", "Unknown")
    if raw is None:
        return unknown_label
    s = str(raw).strip()
    if not s:
        return unknown_label
    valid_tier1_names = [t.get("tier1") for t in _tier1_defs(spec) if t.get("tier1")]
    # 1) 精确
    if s in valid_tier1_names:
        return s
    # 2) 去符号后精确
    s_key = _clean_key(s)
    for cand in valid_tier1_names:
        if _clean_key(cand) == s_key:
            return cand
    # 3) 别名表（精确匹配 key）
    aliases = spec.get("normalize", {}).get("tier1_aliases", {})
    if s in aliases:
        return aliases[s]
    # 4) 去符号后精确匹配别名
    cleaned_aliases = {_clean_key(k): v for k, v in aliases.items()}
    if s_key in cleaned_aliases:
        return cleaned_aliases[s_key]
    # 5) 关键字包含匹配 — 别名表中的任何一个 key 片段
    for k, v in aliases.items():
        if k and _clean_key(k) and _clean_key(k) in s_key:
            return v
    # 5) 数字兜底: 字符串里找所有两位+数字，再按 midpoint 找到最近的 tier
    nums = re.findall(r"\d{2,3}", s)
    if nums:
        ages = [int(n) for n in nums if int(n) < 120]
        if ages:
            # 选最大值（一般是出生年份才会很大；但年龄场景取 max 更稳妥）
            age = max(ages)
            tiers = [t for t in _tier1_defs(spec) if t.get("age_midpoint") is not None]
            if tiers:
                nearest = min(tiers, key=lambda t: abs(t["age_midpoint"] - age))
                return nearest["tier1"]
    return unknown_label


def normalize_tier2(tier1: str, raw_tier2: Any,
                    reasoning_text: Optional[str] = None,
                    spec: Optional[Dict] = None) -> Optional[str]:
    if spec is None:
        spec = load_spec()
    if tier1 == spec["age"].get("unknown_label", "Unknown"):
        return None
    # 找到这个 tier1 的定义
    target = None
    for t in _tier1_defs(spec):
        if t.get("tier1") == tier1:
            target = t
            break
    if target is None:
        return None
    valid_tier2 = list(target.get("tier2", []))
    # 如果 tier1 只有一个子标签（例如 Under 13 / 13-17 SA），直接用它
    if len(valid_tier2) == 1:
        return valid_tier2[0]
    if not valid_tier2:
        return None

    if raw_tier2 is None:
        raw_s = ""
    else:
        raw_s = str(raw_tier2).strip()

    # 1) 精确
    if raw_s and raw_s in valid_tier2:
        return raw_s
    # 2) 去符号 / 大小写
    if raw_s:
        key = _clean_key(raw_s)
        for cand in valid_tier2:
            if _clean_key(cand) == key:
                return cand
        # 3) Unknown_* 映射
        if "unknown" in key:
            # 找当前 tier1 对应的 Unknown_*
            for cand in valid_tier2:
                if _clean_key(cand).startswith("unknown"):
                    return cand

    # 4) 数字推理
    if raw_s:
        nums = re.findall(r"\d{2,3}", raw_s)
        if nums:
            ages = [int(n) for n in nums if int(n) < 120]
            if ages:
                age = ages[0]  # 用第一个数字作为代表
                # 在 valid_tier2 里找包含这个数字范围的项
                for cand in valid_tier2:
                    cnums = re.findall(r"\d{2,3}", cand)
                    if cnums and min(int(c) for c in cnums) <= age <= max(int(c) for c in cnums):
                        return cand

    # 5) 从 reasoning_text 里回挖关键词
    if reasoning_text:
        keywords_map = spec.get("normalize", {}).get("tier2_keywords", {})
        text_lower = str(reasoning_text).lower()
        for cand_tier2, kws in keywords_map.items():
            if cand_tier2 not in valid_tier2:
                continue
            if any(str(kw).lower() in text_lower for kw in kws):
                return cand_tier2

    # 6) 兜底 — 返回 Unknown_*
    for cand in valid_tier2:
        if _clean_key(cand).startswith("unknown"):
            return cand
    return valid_tier2[-1]


# ---------------------- parse tag ---------------------- #
def parse_age_tag(val: Any, spec: Optional[Dict] = None) -> Tuple[str, Optional[str]]:
    """解析 `["tier1","tier2"]` / `{"age":[...]}` / "25-44" 到 (tier1, tier2)。"""
    if spec is None:
        spec = load_spec()
    unknown_label = spec["age"].get("unknown_label", "Unknown")

    if val is None:
        return unknown_label, None

    # dict: {"age":["25-44","35-44"], "gender":"female"}
    if isinstance(val, dict):
        age = val.get("age")
        if isinstance(age, list) and age:
            t1 = normalize_tier1(age[0], spec)
            t2_raw = age[1] if len(age) >= 2 else None
            if t1 == unknown_label:
                return unknown_label, None
            return t1, normalize_tier2(t1, t2_raw, str(val.get("reasoning", "")), spec)
        if isinstance(age, str):
            t1 = normalize_tier1(age, spec)
            if t1 == unknown_label:
                return unknown_label, None
            return t1, normalize_tier2(t1, None, str(val.get("reasoning", "")), spec)

    # list
    if isinstance(val, list):
        if not val:
            return unknown_label, None
        t1 = normalize_tier1(val[0], spec)
        if t1 == unknown_label:
            return unknown_label, None
        t2_raw = val[1] if len(val) >= 2 else None
        return t1, normalize_tier2(t1, t2_raw, None, spec)

    # str 单独的 tier1
    s = str(val).strip()
    t1 = normalize_tier1(s, spec)
    if t1 == unknown_label:
        return unknown_label, None
    return t1, normalize_tier2(t1, None, None, spec)


def parse_final_result_dict(val: Any, spec: Optional[Dict] = None) -> Tuple[str, Optional[str], str]:
    """解析 `{"age":[...],"gender":"..."}` 或单独的 age，返回 (tier1, tier2, gender)。"""
    if spec is None:
        spec = load_spec()
    if isinstance(val, dict):
        t1, t2 = parse_age_tag(val.get("age"), spec)
        gender = normalize_gender(val.get("gender"), spec)
        return t1, t2, gender
    t1, t2 = parse_age_tag(val, spec)
    return t1, t2, "unknown"


# ---------------------- normalize full agent output ---------------------- #
def normalize_agent_output(agent_dict: Dict[str, Any],
                           spec: Optional[Dict] = None) -> Tuple[str, Optional[str], str]:
    """把模型返回的 `{"tier1":"...","tier2":"...","gender":"...","reasoning":"..."}` 规范化。"""
    if spec is None:
        spec = load_spec()
    reasoning = str(agent_dict.get("reasoning") or agent_dict.get("evidence") or "")
    raw_tier1 = agent_dict.get("tier1")
    raw_tier2 = agent_dict.get("tier2")
    raw_gender = agent_dict.get("gender")
    # fallback: 有些模型直接写 "age_group" / "age"
    if raw_tier1 is None and agent_dict.get("age_group"):
        raw_tier1 = agent_dict.get("age_group")
    if raw_tier1 is None and agent_dict.get("age"):
        age_val = agent_dict.get("age")
        if isinstance(age_val, list) and age_val:
            raw_tier1 = age_val[0]
            raw_tier2 = age_val[1] if len(age_val) >= 2 else raw_tier2
        elif isinstance(age_val, str):
            raw_tier1 = age_val
    if isinstance(raw_tier1, list):
        raw_tier1, raw_tier2 = raw_tier1[0], (raw_tier1[1] if len(raw_tier1) >= 2 else None)

    t1 = normalize_tier1(raw_tier1, spec)
    if t1 == spec["age"].get("unknown_label", "Unknown"):
        return t1, None, normalize_gender(raw_gender, spec)
    t2 = normalize_tier2(t1, raw_tier2, reasoning, spec)
    return t1, t2, normalize_gender(raw_gender, spec)


# ---------------------- build prompt from spec ---------------------- #
def build_system_prompt(spec: Dict[str, Any]) -> str:
    """渲染 system prompt（给模型做判定依据）。"""
    p = spec["core_principles"]
    lines = []
    lines.append("你是一名 TikTok 账号年龄与性别标注员，只能基于用户提供的主页截图做判定，禁止补充截图以外的信息。")
    lines.append("")
    lines.append("【核心判定原则】")
    lines.append("- 合理推断：无需 100% 确定，结合全部可用信息做合理推断，尽量减少 Unknown 标签。")
    lines.append("- 避免偏见：结合市场与文化常识判断；陌生文化背景账号谨慎处理；禁止依托种族、外貌做主观假设。")
    lines.append("")
    lines.append("【信息优先级（高→低）】")
    for i, item in enumerate(p.get("information_priority", []), 1):
        lines.append(f"  {i}. {item}")
    lines.append(f"  * {p.get('priority_note', '')}")
    lines.append("")
    lines.append("【固定推理顺序】")
    for i, item in enumerate(p.get("reasoning_order", []), 1):
        lines.append(f"  {i}. {item}")
    lines.append("")
    lines.append("【年龄标注为 Unknown 的情况（满足任一即 Unknown）】")
    for item in p.get("unknown_rules_for_age", []):
        lines.append(f"  - {item}")
    lines.append("")
    if p.get("age_calibration"):
        lines.append("【年龄校准原则（重要，优先级高于细分档默认规则）】")
        for item in p.get("age_calibration", []):
            lines.append(f"  - {item}")
        lines.append("")
    lines.append("【性别标注原则】")
    for item in p.get("gender_rules", []):
        lines.append(f"  - {item}")
    lines.append("")
    return "\n".join(lines)


def build_age_labels_section(spec: Dict[str, Any]) -> str:
    """渲染「每个 tier 的特征」段落。"""
    tiers = spec["age"]["tiers"]
    lines = ["【年龄标签体系（两层分类）】"]
    lines.append("tier1 = 粗分类；tier2 = 细分类（tier1 != Unknown 时才有 tier2）。")
    lines.append("")
    for t in tiers:
        t1 = t["tier1"]
        lines.append(f"--- tier1: {t1} ({t.get('synopsis', '')}) ---")
        t2_list = t.get("tier2", [])
        lines.append(f"可选 tier2: {', '.join(t2_list) if t2_list else '无'}")
        for key in ("main_features", "secondary_features",
                    "shared_features_18_24",
                    "features_18_20", "features_21_24",
                    "features_25_34", "features_35_44",
                    "features_45_54", "features_55_plus",
                    "unknown_18_24_rules", "unknown_25_44_rules", "unknown_45_55_plus_rules"):
            if key in t:
                header = key.replace("_", " ").replace("features", "特征")
                lines.append(f"  * {header}:")
                for bullet in t[key]:
                    lines.append(f"    - {bullet}")
        lines.append("")
    lines.append("【边界注意】")
    for note in spec.get("age", {}).get("boundary_notes", []):
        lines.append(f"  - {note}")
    lines.append("")
    return "\n".join(lines)


def build_user_prompt_instruction(spec: Dict[str, Any]) -> str:
    """渲染给模型的「输出格式」部分。"""
    tmpl = spec.get("agent_prompt_template", {})
    lines = []
    lines.append("【输出格式】严格按 JSON 输出，不允许输出代码块或额外文字。")
    lines.append("字段:")
    lines.append("  evidence  -> object，每个字段用一句话中文要点：")
    for ef in tmpl.get("requested_evidence_fields", []):
        lines.append(f"    - {ef}")
    lines.append("  tier1     -> string，必须是下列之一: "
                 + ", ".join(t["tier1"] for t in spec["age"]["tiers"]))
    lines.append("  tier2     -> string 或 null；当 tier1 != Unknown 时必填，从对应 tier1 的子选项中选一个；")
    lines.append("               具体子选项见每个 tier1 的描述。")
    lines.append("  gender    -> male / female / unknown")
    lines.append("  confidence-> high / medium / low")
    lines.append(f"  reasoning -> 中文总结（不超过 {tmpl.get('max_reasoning_chars', 120)} 字）")
    lines.append("")
    lines.append("示例（不要照抄，按真实判断）:")
    lines.append('{"evidence":{"account_status":"正常","subject_validity":"本人出镜",')
    lines.append('"age_clue_video":"职场穿搭+亲子画面","age_clue_avatar":"成熟面容",')
    lines.append('"age_clue_bio":"妈妈/mom","aging_signals":"法令纹+眼角细纹",')
    lines.append('"aging_signal_count":2,"hard_triggers_45_plus":[],"has_45plus_trigger":false,')
    lines.append('"boundary_18_24_vs_25_44":"无","boundary_25_44_vs_45_plus":"无",')
    lines.append('"gender_clue_text":"无","gender_clue_visual":"女性发型与妆容"},')
    lines.append('"tier1":"25-44","tier2":"35-44","gender":"female","confidence":"high",')
    lines.append('"reasoning":"画面为职场妈妈，命中法令纹与眼角细纹，定 35-44。"}')
    return "\n".join(lines)


def build_full_system_prompt(spec: Dict[str, Any]) -> str:
    return build_system_prompt(spec) + build_age_labels_section(spec) + build_user_prompt_instruction(spec)
