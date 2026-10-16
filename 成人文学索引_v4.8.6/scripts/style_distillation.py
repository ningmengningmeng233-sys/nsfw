#!/usr/bin/env python3
"""文风蒸馏：轻量风格卡生成与完整蒸馏扩展接口。

v4.6.3 修复：
- 目标书没有章节索引时返回 error，不再生成“抽样 0/0”的占位成功卡；
- 书名/作者经 book_key 规范化，章节索引匹配改为字面量前缀（书名含 `[` 不再误匹配）；
- 轻量风格卡过滤「无」/占位符/空项，人物表格行转成可读文本，并做去重；
- 结果携带 card 文本，CLI 与 orchestrate style 会直接输出卡片内容。

v4.6.5 修复：
- 抽样硬上限 _MAX_LIGHTWEIGHT_SEGMENTS 生效（此前 top-10 再无条件追加首末段，
  实际可抽到 12 段，违反协议"抽样 8~10 段"）；
- 卡片标题标注「素材底稿」：脚本产出只是索引素材的机械抽取，最终七维风格卡
  （每维「观察 + 可执行规则」、锚点每轴 ≤2 句且每句 ≤15 字）必须由编排器
  （AI）按 references/索引内容.md 第九节重写；
- 金句锚点截断至 60 字、剧情概要截断至 120 字，避免段落级情节复述整段带入；
- 诚实边界补充「未做原文语言抽查」说明。

v4.6.6 风格卡 v2（借鉴 image2-styles 定性骨架，方法论移植）：
- 新增「量化语言指纹」：按章节索引 frontmatter 的 源文件 对被抽段做原文
  抽样（预算 min(500, 被抽段总行数×5%)，协议第 4 级抽查由蒸馏触发词授权），
  脚本实测键值对指标——平均句长 / 对话字符占比 / 省略号密度 / 问叹句占比 /
  段落长度中位数，替代"语言指纹=金句摘录"的假指纹；
- 新增「组合公式」模板（[一句话定性]+[内容需求]+[规则]+[禁则]），对齐
  image-style skill 的 generation_formula，由编排器填充；
- 最终风格卡升为 v2 九节结构，见 references/索引内容.md 第九节。

C7-1：外部文风技能支持「技能目录」：
- INDEX_STYLE_SKILL / --style-skill 指向目录时按 SKILL.md 的 style-entry →
  scripts/run_style.py → run.py 顺序解析入口并 subprocess 调用；
- 解析不到入口时返回人话报错（点名查过哪几个候选），不外泄目录占位提示，
  也不把 traceback 甩给用户；
- 外部产物非零退出时 error 取 stderr **尾部**并做脱敏/去堆栈处理。

C7-2：风格卡加机器可读产出：
- 轻量蒸馏与目录式完整蒸馏都在 style-card.md 同目录同词干再写
  style-card.json（字段见 schemas/style-card.schema.json），创作侧不必再
  从 Markdown 抠字段；写盘前用 scripts/style_card_schema.py 按 schema 校验；
- style-card.md 的渲染逐字未变（素材底稿仍是人读层）。
"""
import argparse
import json
import os
import re
import shlex
import subprocess
import sys
import time
from datetime import date
from pathlib import Path
from typing import Any

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

try:
    from _common import book_key, parse_frontmatter, read_text_auto, resolve_under
    from _logging import get_logger
    import style_card_schema
except ImportError:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from _common import book_key, parse_frontmatter, read_text_auto, resolve_under
    from _logging import get_logger
    import style_card_schema

logger = get_logger("style_distillation")

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$", re.MULTILINE)
_MAX_LIGHTWEIGHT_SEGMENTS = 10
_PLACEHOLDER_TOKENS = {"无", "无。", "（编排器补写）", "-", "—"}

# 风格卡机器可读产物（C7-2）
_STYLE_CARD_MD_NAME = "style-card.md"
_STYLE_CARD_JSON_NAME = "style-card.json"
# 目录式外部文风技能的入口解析顺序（C7-1）：SKILL.md 的 style-entry 优先，其次这两个默认名
_STYLE_ENTRY_CANDIDATES = ("scripts/run_style.py", "run.py")
# 「组合公式」槽位与「不可写」禁则：md 渲染与 json 产物共用同一份常量，防双源漂移
_COMBO_FORMULA_SLOTS = (
    ("一句话定性", "<宏观 medium 式定性句，≤40 字，综合各维填写>"),
    ("内容需求", "<用户本次创作要求>"),
    ("叙事规则", "<从叙事模型维选 2~3 条可执行规则>"),
    ("语言规则", "<从量化语言指纹选 3~5 条阈值化指标，如 对话占比 0.3~0.5>"),
    ("人物规则", "<从人物塑造维选 1~2 条>"),
    ("禁则", "<不可写维全部适用项>"),
)
_NEVER_ITEMS = (
    "不将抽样结论夸大为全书风格定论",
    "不虚构索引中未出现的原文、行号或创作手法",
    "不做段落级复述、情节复述或长文摘抄",
)
_INCOMPLETE_MARKERS = ("索引中未收录", "未做原文抽查")

# v4.6.6 量化语言指纹：原文抽样预算与统计正则
_MAX_LANGUAGE_SAMPLE_LINES = 500
_LANGUAGE_SAMPLE_RATIO = 0.05
_SENT_SPLIT_RE = re.compile(r"[。！？!?…]+")
_QUOTE_RE = re.compile(r"「[^」]*」|『[^』]*』|“[^”]*”|\"[^\"]{2,}\"")
_ELLIPSIS_RE = re.compile(r"…+|——")


def _safe_path(root: Path, relative: str) -> Path | None:
    resolved = resolve_under(str(root), relative)
    return Path(resolved) if resolved else None


def _extract_sections(text: str) -> dict[str, dict[str, Any]]:
    headings = []
    for m in _HEADING_RE.finditer(text):
        headings.append((len(m.group(1)), m.group(2).strip(), m.start(), m.end()))

    sections: dict[str, dict[str, Any]] = {}
    i = 0
    n = len(headings)
    while i < n:
        lvl, title, _, body_start = headings[i]
        if lvl != 2:
            i += 1
            continue
        next_i = i + 1
        section_end = headings[next_i][2] if next_i < n else len(text)
        subs: dict[str, str] = {}
        j = i + 1
        while j < n and headings[j][0] > lvl:
            if headings[j][0] == 3:
                sub_title = headings[j][1]
                k = j + 1
                while k < n and headings[k][0] > 3:
                    k += 1
                sub_end = headings[k][2] if k < n else section_end
                subs[sub_title] = text[headings[j][3]:sub_end].strip()
            j += 1
        sections[title] = {"body": text[body_start:section_end].strip(), "subs": subs}
        i = j
    return sections


def _bullet_lines(content: str) -> list[str]:
    lines = []
    for raw in content.splitlines():
        s = raw.strip()
        if s.startswith(("- ", "* ", "+ ")):
            lines.append(s[2:].strip())
        elif s.startswith("-") and len(s) > 1 and s[1] != "-":
            lines.append(s[1:].strip())
    return [ln for ln in lines if ln]


def _first_line(content: str) -> str:
    for line in content.splitlines():
        s = line.strip()
        if s:
            return s
    return ""


def _clean(items: list[str]) -> list[str]:
    """去空、去「无」/占位、去重并保持顺序。"""
    out: list[str] = []
    seen: set[str] = set()
    for item in items:
        s = item.strip().strip("|").strip()
        if not s or s in _PLACEHOLDER_TOKENS or "（编排器补写）" in s:
            continue
        if s in seen:
            continue
        seen.add(s)
        out.append(s)
    return out


def _table_row_to_text(row: str) -> str | None:
    """把小说索引「人物关系链」的 Markdown 表格行转成可读短句。"""
    s = row.strip()
    if not s.startswith("|") or not s.endswith("|"):
        return None
    cells = [c.strip() for c in s.strip("|").split("|")]
    if len(cells) < 3 or any(c in ("", "---", "人物") for c in cells[:2]):
        return None
    name, role, relation = cells[0], cells[1], cells[2]
    if name.startswith("-"):
        return None
    if relation and relation not in _PLACEHOLDER_TOKENS:
        return f"{name}（{role}）：{relation}"
    return f"{name}（{role}）"


def _truncate(text: str, limit: int) -> str:
    """超长条目截断，避免整段情节复述/长金句原样进入卡片。"""
    s = text.strip()
    return s if len(s) <= limit else s[:limit].rstrip() + "……"


def _strip_frontmatter_lines(lines: list[str]) -> list[str]:
    """去掉段文件头部 `--- ... ---` frontmatter，返回正文行。"""
    if lines and lines[0].strip() == "---":
        for i in range(1, len(lines)):
            if lines[i].strip() == "---":
                return lines[i + 1 :]
    return lines


def _compute_language_metrics(lines: list[str]) -> dict[str, str]:
    """对原文样本行做键值对量化统计（对齐 image-style 的 surface language）。

    纯机械统计，不依赖 LLM：平均句长 / 对话字符占比 / 省略号密度 /
    问叹句占比 / 段落长度中位数。样本不足 100 有效字符时返回空表。
    """
    text = "\n".join(lines)
    total_chars = len(re.sub(r"\s", "", text))
    if total_chars < 100:
        return {}

    sentences = [s.strip() for s in _SENT_SPLIT_RE.split(text) if s.strip()]
    avg_sentence = (sum(len(s) for s in sentences) / len(sentences)) if sentences else 0.0
    quoted = sum(len(m.group(0)) for m in _QUOTE_RE.finditer(text))
    ellipses = len(_ELLIPSIS_RE.findall(text))
    excl_q = len(re.findall(r"[！？!?]", text))
    paragraphs = sorted(
        len(re.sub(r"\s", "", ln)) for ln in lines if ln.strip()
    )
    median_paragraph = paragraphs[len(paragraphs) // 2] if paragraphs else 0

    return {
        "sample_chars": str(total_chars),
        "average_sentence_length_chars": f"{avg_sentence:.1f}",
        "dialogue_char_ratio": f"{quoted / total_chars:.2f}",
        "ellipsis_per_1000_chars": f"{ellipses * 1000 / total_chars:.1f}",
        "excl_question_sentence_ratio": f"{excl_q / max(len(sentences), 1):.2f}",
        "median_paragraph_chars": str(median_paragraph),
    }


def _collect_language_sample(
    lib: Path, segments: list[dict[str, Any]]
) -> tuple[list[str], int, int]:
    """按章节索引 frontmatter 的 `源文件` 抽样原文行。

    预算 = min(500, 被抽段正文总行数 × 5%)（协议第 4 级抽查，蒸馏触发词授权）；
    预算均摊到每个被抽段，从正文前 1/3 处取连续行。返回 (样本行, 预算, 实用行数)。
    """
    sources: list[tuple[Path | None, list[str]]] = []
    total_lines = 0
    for seg in segments:
        src_rel = (seg.get("meta") or {}).get("源文件")
        path = _safe_path(lib, str(src_rel)) if src_rel else None
        content_lines: list[str] = []
        if path and path.exists():
            try:
                content_lines = _strip_frontmatter_lines(
                    read_text_auto(str(path)).splitlines()
                )
            except Exception as e:
                logger.warning("读取段文件失败 %s: %s", path, e)
                path = None
        total_lines += len(content_lines)
        sources.append((path, content_lines))

    if total_lines == 0:
        return [], 0, 0
    # 预算 = min(500, 被抽段正文总行数 × 5%)，下限 20 行（超小书全量取，诚实边界如实记录）
    budget = min(
        _MAX_LANGUAGE_SAMPLE_LINES,
        max(20, int(total_lines * _LANGUAGE_SAMPLE_RATIO)),
        total_lines,
    )
    per_file = max(1, budget // len(sources))

    sample: list[str] = []
    used = 0
    for path, content_lines in sources:
        if path is None or used >= budget or not content_lines:
            continue
        take = min(per_file, budget - used, len(content_lines))
        start = max(0, (len(content_lines) - take) // 3)
        sample.extend(content_lines[start : start + take])
        used += take
    return sample, budget, used


def _prefixed_chapter_indexes(chapter_dir: Path, key: str) -> list[Path]:
    """字面量前缀匹配章节索引，避免 glob 元字符。"""
    if not chapter_dir.is_dir():
        return []
    return sorted(
        p for p in chapter_dir.iterdir()
        if p.is_file() and p.name.startswith(key + "-") and p.name.endswith("-idx.md")
    )


def _sample_segments(paths: list[Path]) -> tuple[list[Path], int]:
    total = len(paths)
    if total <= _MAX_LIGHTWEIGHT_SEGMENTS:
        return list(paths), total

    scored: list[tuple[Path, int]] = []
    for p in paths:
        score = 0
        try:
            meta, _ = parse_frontmatter(read_text_auto(str(p)))
        except Exception:
            meta = {}
        tags = " ".join(meta.get("包含元素") or [])
        if "金句" in tags or "名台词" in tags:
            score += 3
        if "爽点" in tags or "名场面" in tags:
            score += 2
        scored.append((p, score))

    scored.sort(key=lambda x: (-x[1], x[0].name))
    # v4.6.5 修复：硬上限 _MAX_LIGHTWEIGHT_SEGMENTS。此前先取 top-10 再无条件
    # 追加首末段，上限实际可到 12，违反协议"抽样 8~10 段"。首末段保底入选，
    # 超限时置换得分最低的非锚点候选；金句密集段（优先级①）在其余名额中生效。
    score_map = dict(scored)
    selected: list[Path] = []
    seen: set[Path] = set()
    for p, _ in scored[:_MAX_LIGHTWEIGHT_SEGMENTS]:
        selected.append(p)
        seen.add(p)
    anchors = [paths[0], paths[-1]]
    for anchor in anchors:
        if anchor in seen:
            continue
        if len(selected) < _MAX_LIGHTWEIGHT_SEGMENTS:
            selected.append(anchor)
            seen.add(anchor)
            continue
        victim_idx = None
        victim_score = None
        for i, p in enumerate(selected):
            if p in anchors:
                continue
            s = score_map.get(p, 0)
            if victim_score is None or s <= victim_score:
                victim_score = s
                victim_idx = i
        if victim_idx is not None:
            seen.discard(selected[victim_idx])
            selected[victim_idx] = anchor
            seen.add(anchor)
    return sorted(selected), total


def _collect_material(lib: Path, novel: str, author: str) -> dict[str, Any]:
    key = book_key(novel, author)
    novel_path = _safe_path(lib, f"小说索引/{key}.md")
    chapter_dir = _safe_path(lib, "章节索引")

    novel_meta: dict[str, Any] = {}
    novel_sections: dict[str, dict[str, Any]] = {}
    if novel_path and novel_path.exists():
        meta, body = parse_frontmatter(read_text_auto(str(novel_path)))
        novel_meta = meta
        novel_sections = _extract_sections(body)
    else:
        logger.warning("未找到小说索引: %s", novel_path)

    chapter_paths = _prefixed_chapter_indexes(chapter_dir, key) if chapter_dir else []

    sampled, total = _sample_segments(chapter_paths)
    segments: list[dict[str, Any]] = []
    for p in sampled:
        try:
            meta, body = parse_frontmatter(read_text_auto(str(p)))
            segments.append({
                "path": p,
                "meta": meta,
                "sections": _extract_sections(body),
            })
        except Exception as e:
            logger.warning("读取章节索引失败 %s: %s", p, e)

    return {
        "key": key,
        "novel_meta": novel_meta,
        "novel_sections": novel_sections,
        "segments": segments,
        "total_segments": total,
        "sampled_segments": len(segments),
    }


def _collect_material_with_language(
    lib: Path, novel: str, author: str
) -> dict[str, Any]:
    """_collect_material + 原文抽样量化语言指纹（v4.6.6）。"""
    material = _collect_material(lib, novel, author)
    sample, budget, used = _collect_language_sample(lib, material["segments"])
    material["language_metrics"] = _compute_language_metrics(sample)
    material["language_sample_lines"] = used
    material["language_budget"] = budget
    return material


def _card_fields(material: dict[str, Any], novel: str, author: str) -> dict[str, Any]:
    """把索引素材整理成风格卡各维字段（md 渲染与 json 产物的唯一共同事实源）。

    C7-2 抽出此函数以避免「md 一套逻辑、json 另一套逻辑」的双源漂移：两条产出
    路径都只消费这里的字段，渲染层的截断/占位仍由各自负责。
    """
    meta = material["novel_meta"]
    novel_sections = material["novel_sections"]
    segments = material["segments"]
    total = material["total_segments"]
    sampled = material["sampled_segments"]

    novel_name = meta.get("小说名称") or f"《{novel}》"
    author_name = meta.get("作者") or author
    index_range = meta.get("章节范围") or "未知"

    anchors: list[str] = []
    narrative: list[str] = []
    plot: list[str] = []
    character_raw: list[str] = []
    motif: list[str] = []

    for seg in segments:
        for title, sec in seg["sections"].items():
            subs = sec.get("subs", {})
            is_chapter = "章" in title or title.startswith("第")
            if is_chapter:
                anchors.extend(_bullet_lines(subs.get("金句/名场面", "")))
                narrative.extend(_bullet_lines(subs.get("创作手法分析", "")))
                plot.extend(_bullet_lines(subs.get("爽点/看点", "")))
                motif.append(_first_line(subs.get("剧情概要", "")))
            elif title == "本段总结":
                narrative.extend(_bullet_lines(subs.get("情绪弧线", "")))
                plot.extend(_bullet_lines(subs.get("可迁移的创作手法", "")))
            elif title == "本段概况":
                motif.append(_first_line(sec.get("body", "")))

    highlights = novel_sections.get("全书亮点速查", {}).get("subs", {})
    plot.extend(_bullet_lines(highlights.get("爽点密集区", "")))
    motif.extend(_bullet_lines(highlights.get("名场面", "")))
    anchors.extend(_bullet_lines(highlights.get("金句梗", "")))

    if "人物关系链" in novel_sections:
        for line in novel_sections["人物关系链"]["body"].splitlines():
            text = _table_row_to_text(line)
            if text:
                character_raw.append(text)

    anchors = _clean(anchors)
    narrative = _clean(narrative)
    plot = _clean(plot)
    character = _clean(character_raw)
    motif = _clean(motif)

    return {
        "作品": novel_name,
        "作者": author_name,
        "章节范围": index_range,
        "总段数": total,
        "抽样段数": sampled,
        "叙事模型": narrative,
        "金句锚点": anchors,
        "母题意象": motif,
        "人物塑造": character,
        "情节技法": plot,
        "量化语言指纹": material.get("language_metrics") or {},
        "抽样行数": material.get("language_sample_lines") or 0,
        "抽样预算": material.get("language_budget") or 0,
    }


def _honesty_lines(fields: dict[str, Any]) -> list[str]:
    """⑨诚实边界：md 与 json 共用同一份措辞。"""
    used_lines = fields["抽样行数"]
    budget_lines = fields["抽样预算"]
    return [
        f"- 基于章节索引抽样 {fields['抽样段数']}/{fields['总段数']} 段",
        f"- 小说索引覆盖范围：{fields['章节范围']}",
        (
            f"- 量化语言指纹基于原文抽查 {used_lines} 行"
            f"（预算 min(500, 被抽段总行数×5%)={budget_lines} 行），样本外风格可能漂移"
            if used_lines
            else "- 本卡未做原文语言抽查；量化指标需编排器按第 4 级抽查原文补测"
            "（≤500 行/次，累计 ≤被抽段总行数 5%）"
        ),
        "- 完整文风蒸馏需配置 INDEX_STYLE_SKILL 并显式调用外部 skill",
    ]


def _render_card(material: dict[str, Any], novel: str, author: str) -> str:
    fields = _card_fields(material, novel, author)
    novel_name = fields["作品"]
    author_name = fields["作者"]
    index_range = fields["章节范围"]
    total = fields["总段数"]
    sampled = fields["抽样段数"]
    narrative = fields["叙事模型"]
    anchors = fields["金句锚点"]
    motif = fields["母题意象"]
    character = fields["人物塑造"]
    plot = fields["情节技法"]

    today = date.today().isoformat()
    metrics = fields["量化语言指纹"]
    lines = [
        "---",
        f"作品: {novel_name}",
        f"作者: {author_name}",
        f"蒸馏日期: {today}",
        f"基于索引范围: {index_range}",
        f"抽样: {sampled}/{total} 段",
        "蒸馏方式: 轻量蒸馏",
        "完整版: 未生成（需配置 INDEX_STYLE_SKILL 调用外部 skill）",
        "---",
        "",
        f"# {novel_name} 轻量风格卡（素材底稿）",
        "",
        "> 本卡由脚本从索引小节机械抽取 + 原文抽样统计生成，是编排器写最终风格卡的**素材底稿**，不是风格定性。",
        "> 最终风格卡 v2（九节）须由编排器（AI）按 `references/索引内容.md` 第九节重写：",
        "> ①一句话定性（宏观 medium 式，≤40 字）②量化语言指纹（复核本卡实测值）",
        "> ③~⑥叙事模型/母题意象/人物塑造/情节技法（每维「观察 + 可执行规则」，过技法过滤三准则）",
        "> ⑦组合公式 ⑧不可写（Never 句式）⑨诚实边界；金句锚点挂在②，每轴 ≤2 句且每句 ≤15 字并标注来源，",
        "> 底稿中的长金句与情节概要不得原样带入。",
        "",
        "## 量化语言指纹（脚本实测）",
    ]
    if metrics:
        lines.extend(f"- {k}: {v}" for k, v in metrics.items())
    else:
        lines.append("- 未做原文抽查（章节索引缺源文件或样本不足），由编排器按第 4 级人工补测")

    lines.extend(["", "## 叙事模型"])
    lines.extend(f"- {x}" for x in (narrative[:8] or ["索引中未收录足够叙事模型描述"]))

    lines.extend(["", "## 金句锚点素材"])
    lines.extend(f"- {x}" for x in (_truncate(a, 60) for a in (anchors[:6] or ["索引中未收录足够金句/名场面"])))

    lines.extend(["", "## 母题意象"])
    lines.extend(f"- {x}" for x in (_truncate(m, 120) for m in (motif[:8] or ["索引中未收录足够母题意象"])))

    lines.extend(["", "## 人物塑造"])
    lines.extend(f"- {x}" for x in (character[:6] or ["索引中未收录足够人物关系描述"]))

    lines.extend(["", "## 情节技法"])
    lines.extend(f"- {x}" for x in (plot[:8] or ["索引中未收录足够情节技法"]))

    lines.extend(["", "## 组合公式（模板，编排器填充后随创作稿输出）"])
    lines.extend(
        ["```text"]
        + [f"[{label}] {value}" for label, value in _COMBO_FORMULA_SLOTS]
        + ["```"]
    )

    lines.extend(["", "## 不可写"])
    lines.extend([f"- {x}" for x in _NEVER_ITEMS])

    lines.extend(["", "## 诚实边界"])
    lines.extend(_honesty_lines(fields))

    return "\n".join(lines) + "\n"


def _build_card_payload(
    fields: dict[str, Any],
    lib: Path,
    *,
    completeness: str,
    external_entry: str | None = None,
    today: str | None = None,
) -> dict[str, Any]:
    """轻量素材底稿的结构化版本（创作侧机读，契约见 schemas/style-card.schema.json）。

    与 md 保持同一截断口径（叙事 ≤8、金句 ≤6、母题 ≤8、人物 ≤6、情节 ≤8），但
    数据不足时给**空数组**而不是「索引中未收录…」这类人读占位句。
    """
    def _clean_items(items: list[str]) -> list[str]:
        return [x for x in items if not any(m in x for m in _INCOMPLETE_MARKERS)]

    honesty = [ln.lstrip("- ").strip() for ln in _honesty_lines(fields)]
    source: dict[str, Any] = {"库": str(lib)}
    if external_entry:
        source["外部技能入口"] = external_entry
    return {
        "作品": fields["作品"],
        "作者": fields["作者"],
        "蒸馏日期": today or date.today().isoformat(),
        "基于索引范围": fields["章节范围"],
        "抽样": {"已抽样段数": fields["抽样段数"], "总段数": fields["总段数"]},
        "完整性": completeness,
        "来源": source,
        "量化语言指纹": dict(fields["量化语言指纹"]),
        "叙事模型": _clean_items(fields["叙事模型"][:8]),
        "金句锚点": _clean_items([_truncate(a, 60) for a in fields["金句锚点"][:6]]),
        "母题意象": _clean_items([_truncate(m, 120) for m in fields["母题意象"][:8]]),
        "人物塑造": _clean_items(fields["人物塑造"][:6]),
        "情节技法": _clean_items(fields["情节技法"][:8]),
        "组合公式": {label: value for label, value in _COMBO_FORMULA_SLOTS},
        "不可写": list(_NEVER_ITEMS),
        "诚实边界": honesty,
    }


def _card_payload_from_markdown(
    md_text: str,
    lib: Path,
    novel: str,
    author: str,
    *,
    external_entry: str | None = None,
) -> dict[str, Any]:
    """把外部文风技能产出的风格卡 Markdown 反解成同一份结构化字段（C7-2）。

    只认本技能 v2 九节的版面（小节标题前缀匹配 + `- ` 条目 + frontmatter 键），
    版面外内容一律忽略：创作侧要的是稳定可读的字段，而不是把任意文本塞进 json。
    """
    meta, body = parse_frontmatter(md_text)
    sections = _extract_sections(body)

    def _section(prefix: str) -> tuple[list[str], dict[str, str]]:
        for title, sec in sections.items():
            if title.startswith(prefix):
                return _bullet_lines(sec.get("body", "")), dict(sec.get("subs", {}))
        return [], {}

    def _section_body(prefix: str) -> str:
        for title, sec in sections.items():
            if title.startswith(prefix):
                return sec.get("body", "")
        return ""

    def _items(prefix: str) -> list[str]:
        bullets, subs = _section(prefix)
        if not bullets:
            for sub_body in subs.values():
                bullets.extend(_bullet_lines(sub_body))
        return _clean(bullets)

    metric_lines, _ = _section("量化语言指纹")
    metrics: dict[str, str] = {}
    for line in metric_lines:
        key, sep, value = line.partition(":")
        if sep and key.strip() and value.strip():
            metrics[key.strip()] = value.strip()

    formula: dict[str, str] = {}
    for line in _section_body("组合公式").splitlines():
        m = re.match(r"^\[([^\]]+)\]\s*(.*)$", line.strip())
        if m:
            formula[m.group(1).strip()] = m.group(2).strip()

    sampled = total = 0
    m = re.search(r"(\d+)\s*/\s*(\d+)", str(meta.get("抽样", "")))
    if m:
        sampled, total = int(m.group(1)), int(m.group(2))

    distill_mode = str(meta.get("蒸馏方式", ""))
    full_version = str(meta.get("完整版", ""))
    completeness = "轻量" if ("轻量" in distill_mode or "未生成" in full_version) else "完整"
    raw_date = str(meta.get("蒸馏日期", "")).strip()
    today = raw_date if re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw_date) else date.today().isoformat()

    source: dict[str, Any] = {"库": str(lib)}
    if external_entry:
        source["外部技能入口"] = external_entry
    sampling: dict[str, Any] = {"已抽样段数": sampled, "总段数": total}
    if not m:
        sampling["说明"] = "外部产物未标注抽样，记 0/0"
    payload = {
        "作品": str(meta.get("作品") or f"《{novel}》"),
        "作者": str(meta.get("作者") or author),
        "蒸馏日期": today,
        "基于索引范围": str(meta.get("基于索引范围") or "未知"),
        "抽样": sampling,
        "完整性": completeness,
        "来源": source,
        "量化语言指纹": metrics,
        "叙事模型": _items("叙事模型"),
        "金句锚点": _items("金句锚点"),
        "母题意象": _items("母题意象"),
        "人物塑造": _items("人物塑造"),
        "情节技法": _items("情节技法"),
        "组合公式": formula or {label: value for label, value in _COMBO_FORMULA_SLOTS},
        "不可写": _items("不可写") or list(_NEVER_ITEMS),
        "诚实边界": _items("诚实边界") or ["由外部文风技能产物解析，未经本技能复核"],
    }
    return payload


def _write_card_json(
    payload: dict[str, Any],
    out_file: Path,
    log: Any,
) -> str | None:
    """写结构化风格卡；先按 schema 校验，不合格也要写（md 才是人读权威）但显式告警。"""
    try:
        errors = style_card_schema.validate_style_card(payload)
    except style_card_schema.StyleCardSchemaError as e:
        errors = []
        log.warning("style-card.json 跳过 schema 校验: %s", e)
    if errors:
        log.warning(
            "style-card.json 不符合风格卡契约，共 %d 处（创作侧请以此为准修正蒸馏器）: %s",
            len(errors),
            "；".join(errors[:3]),
        )
    try:
        out_file.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    except Exception as e:
        log.error("style-card.json 写入失败: %s", e)
        return None
    return str(out_file)


def _card_dir(lib: Path, key: str, output_dir: str | None) -> Path:
    """风格卡落盘目录：默认索引库同级 `风格卡/<书名-作者>`，--output-dir 可覆盖。"""
    if output_dir:
        return Path(output_dir).resolve()
    return lib.parent / "风格卡" / key


def _run_lightweight(
    lib: Path,
    novel: str,
    author: str,
    output_dir: str | None,
    log: Any,
) -> dict[str, Any]:
    try:
        key = book_key(novel, author)
    except ValueError as e:
        return {"mode": "error", "error": str(e)}

    material = _collect_material_with_language(lib, novel, author)
    if material["total_segments"] == 0:
        msg = (
            f"《{novel}》- {author} 未找到任何章节索引（{key}-*-idx.md），"
            "无法轻量蒸馏；请先建库，或提供原文给外部完整蒸馏 skill"
        )
        log.error("%s", msg)
        return {"mode": "error", "error": msg}

    card_dir = _card_dir(lib, key, output_dir)
    card_dir.mkdir(parents=True, exist_ok=True)
    out_file = card_dir / _STYLE_CARD_MD_NAME
    card = _render_card(material, novel, author)
    out_file.write_text(card, encoding="utf-8")
    json_path = _write_card_json(
        _build_card_payload(_card_fields(material, novel, author), lib, completeness="轻量"),
        card_dir / _STYLE_CARD_JSON_NAME,
        log,
    )
    log.info("轻量风格卡素材底稿已生成（最终七维卡须由编排器重写）: %s", out_file)
    result: dict[str, Any] = {"mode": "lightweight", "path": str(out_file), "card": card}
    if json_path:
        result["json_path"] = json_path
    return result


def _error_tail(text: str, limit: int = 400) -> str:
    """外部技能 stderr 摘要：去堆栈、脱敏、只留尾部，面向人而非面向日志机器。

    外部 skill 可能把密钥回显在报错里（配置类报错极常见），这里只保留规则名与
    值位置的提示，绝不把整段原始 stderr 透给使用者，也不外泄 Python 堆栈。
    """
    lines = [ln.rstrip() for ln in (text or "").splitlines() if ln.strip()]
    kept: list[str] = []
    skip_frames = False
    for ln in lines:
        stripped = ln.strip()
        if stripped.startswith("Traceback (most recent call last)"):
            skip_frames = True
            continue
        if skip_frames:
            # 堆栈帧（`  File "..."` / `    x = y`）整段丢弃，只留最后的异常摘要行
            if stripped.startswith('File "') or ln.startswith((" ", "\t")):
                continue
            skip_frames = False
        kept.append(
            re.sub(
                r"(?i)\b(api[_-]?key|apikey|token|secret|password|passwd|authorization|access[_-]?key)\b"
                r"(\s*[:=]\s*)(\S+)",
                r"\1\2<已隐去>",
                stripped,
            )
        )
    tail = " | ".join(kept)
    return tail[-limit:] if len(tail) > limit else tail


def _locate_produced_card(
    card_dir: Path | None,
    stdout: str,
    *,
    since: float | None = None,
) -> Path | None:
    """外部技能产出的卡片路径：优先约定目录，其次 stdout 打印的真实文件。

    ``since`` 是本次调用开始的时刻：目录里可能残留上一次轻量蒸馏的 style-card.md，
    只认**本次新写**的文件，否则会把旧卡当成本次外部技能的产物报给创作侧。
    """
    candidates: list[Path] = []
    if card_dir is not None:
        candidates.append(card_dir / _STYLE_CARD_MD_NAME)
        candidates.extend(sorted(card_dir.glob("style-card*.md")))
    for line in (stdout or "").splitlines():
        s = line.strip().strip('"')
        if s.lower().endswith(".md") and Path(s).is_file():
            candidates.insert(0, Path(s))
    for cand in candidates:
        if not cand.is_file():
            continue
        if since is not None and cand.stat().st_mtime < since:
            continue
        return cand
    return None


def _run_cmd(
    cmd: list[str],
    log: Any,
    *,
    card_dir: Path | None = None,
    lib: Path | None = None,
    novel: str = "",
    author: str = "",
    external_entry: str | None = None,
) -> dict[str, Any]:
    log.info("执行外部文风技能: %s", " ".join(cmd))
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    started = time.time() - 1.0  # 容 1s 文件系统时间粒度，只认本次新写的产物
    try:
        result = subprocess.run(
            cmd,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
        )
    except Exception as e:
        log.error("外部文风技能启动失败: %s", e)
        return {"mode": "external", "error": str(e), "command": " ".join(cmd)}

    if result.returncode != 0:
        err = _error_tail(result.stderr) or f"外部文风技能未输出错误信息（退出码 {result.returncode}）"
        log.error("外部文风技能失败: %s", err)
        return {
            "mode": "external",
            "error": err,
            "command": " ".join(cmd),
            "returncode": result.returncode,
        }

    log.info("外部文风技能执行完成")
    out: dict[str, Any] = {"mode": "external", "returncode": result.returncode, "command": " ".join(cmd)}
    card_path = _locate_produced_card(card_dir, result.stdout, since=started)
    if card_path is not None and lib is not None:
        out["path"] = str(card_path)
        out["card"] = card_path.read_text(encoding="utf-8", errors="replace")
        json_path = _ensure_card_json_sidecar(
            card_path, lib, novel, author, external_entry=external_entry, log=log
        )
        if json_path:
            out["json_path"] = json_path
    return out


def _ensure_card_json_sidecar(
    card_path: Path,
    lib: Path,
    novel: str,
    author: str,
    *,
    external_entry: str | None,
    log: Any,
) -> str | None:
    """外部产物旁补齐 style-card.json（同目录已有则尊重外部技能自己的产物，不覆盖）。"""
    json_file = card_path.with_name(_STYLE_CARD_JSON_NAME)
    if json_file.exists():
        log.info("沿用外部技能自带的结构化风格卡: %s", json_file)
        return str(json_file)
    try:
        payload = _card_payload_from_markdown(
            card_path.read_text(encoding="utf-8", errors="replace"),
            lib,
            novel,
            author,
            external_entry=external_entry,
        )
    except Exception as e:
        log.warning("外部风格卡解析为结构化字段失败，跳过 style-card.json: %s", e)
        return None
    return _write_card_json(payload, json_file, log)


def _resolve_skill_entry(skill_dir: Path) -> tuple[Path | None, str]:
    """目录式外部 skill 的入口解析：style-entry → scripts/run_style.py → run.py。

    返回 (入口脚本或 None, 人话报错)。style-entry 声明了却指错文件时单独报错，
    避免悄悄回落到别的入口，让用户以为配好了其实跑的是另一份代码。
    """
    skill_md = skill_dir / "SKILL.md"
    if skill_md.is_file():
        try:
            meta, _ = parse_frontmatter(read_text_auto(str(skill_md)))
        except Exception as e:
            log_entry = get_logger("style_distillation")
            log_entry.warning("外部 skill 的 SKILL.md 解析失败: %s", e)
            meta = {}
        declared = meta.get("style-entry", "")
        if isinstance(declared, list):
            declared = declared[0] if declared else ""
        declared = str(declared or "").strip()
        if declared:
            resolved = resolve_under(str(skill_dir), declared)
            entry = Path(resolved) if resolved else None
            if entry is None or not entry.is_file():
                return None, (
                    f"外部文风技能 {skill_dir} 的 SKILL.md 写了 style-entry: {declared}，"
                    "但该文件不存在（或不在该技能目录内）。"
                    "请把 style-entry 改成目录内真实存在的脚本，例如 scripts/run_style.py。"
                )
            return entry, ""

    for relative in _STYLE_ENTRY_CANDIDATES:
        entry = skill_dir / relative
        if entry.is_file():
            return entry, ""

    return None, (
        f"无法在外部文风技能目录 {skill_dir} 找到入口脚本。"
        "已依次查找：SKILL.md 的 style-entry 键、"
        f"{'、'.join(_STYLE_ENTRY_CANDIDATES)}，均未命中。"
        "请在该目录的 SKILL.md 里写明 style-entry: 相对路径，"
        "或提供 scripts/run_style.py（需支持 --lib/--novel/--author/--output-dir）。"
    )


def _run_external(
    lib: Path,
    novel: str,
    author: str,
    style_skill: str,
    log: Any,
    output_dir: str | None = None,
) -> dict[str, Any]:
    skill_path = Path(style_skill)
    if skill_path.is_file() and skill_path.suffix == ".py":
        cmd = [
            sys.executable,
            str(skill_path.resolve()),
            "--lib", str(lib),
            "--novel", novel,
            "--author", author,
        ]
        return _run_cmd(cmd, log)

    if skill_path.is_dir():
        entry, complaint = _resolve_skill_entry(skill_path)
        if entry is None:
            log.error("%s", complaint)
            return {"mode": "external", "error": complaint, "skill": str(skill_path)}
        try:
            card_dir = _card_dir(lib, book_key(novel, author), output_dir)
        except ValueError as e:
            return {"mode": "external", "error": str(e), "skill": str(skill_path)}
        card_dir.mkdir(parents=True, exist_ok=True)
        cmd = [
            sys.executable,
            str(entry.resolve()),
            "--lib", str(lib),
            "--novel", novel,
            "--author", author,
            "--output-dir", str(card_dir),
        ]
        return _run_cmd(
            cmd,
            log,
            card_dir=card_dir,
            lib=lib,
            novel=novel,
            author=author,
            external_entry=str(entry.resolve()),
        )

    cmd = shlex.split(style_skill)
    return _run_cmd(cmd, log)


def run_style_distillation(
    lib_dir: str,
    novel: str,
    author: str,
    *,
    style_skill: str | None = None,
    output_dir: str | None = None,
    logger: Any | None = None,
) -> dict[str, Any]:
    log = logger or get_logger(__name__)
    lib = Path(lib_dir).resolve()
    if not lib.is_dir():
        return {"mode": "error", "error": f"索引库不存在或不是目录: {lib}"}

    if style_skill is None:
        style_skill = os.environ.get("INDEX_STYLE_SKILL", "").strip() or None

    if style_skill:
        return _run_external(lib, novel, author, style_skill, log, output_dir=output_dir)
    return _run_lightweight(lib, novel, author, output_dir, log)


def main() -> int:
    parser = argparse.ArgumentParser(description="文风蒸馏接口")
    parser.add_argument("--lib", required=True, help="索引库根目录")
    parser.add_argument("--novel", required=True, help="小说名称")
    parser.add_argument("--author", required=True, help="作者名称")
    parser.add_argument("--style-skill", default=None, help="外部文风技能：技能目录/.py 入口/命令")
    parser.add_argument("--output-dir", default=None, help="风格卡输出目录（默认在索引库同级）")
    args = parser.parse_args()

    result = run_style_distillation(
        args.lib,
        args.novel,
        args.author,
        style_skill=args.style_skill,
        output_dir=args.output_dir,
        logger=logger,
    )
    if result.get("error"):
        print(result["error"], file=sys.stderr)
        return 1
    if result.get("path"):
        print(result["path"])
    card = result.get("card")
    if card:
        print(card)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
