#!/usr/bin/env python3
"""
小说索引自动生成 v3.32（P1-7）

扫描该小说的全部章节索引 frontmatter → 自动生成小说索引骨架：
- 导航表（编号 | 章节范围 | 索引文件 | 段文件）
- 包含元素聚合（组名 + 出现段数）
- 总章节数累加 + 「最后索引章节」推导

「全书概况 / 主线时间线 / 亮点速查」为文字性内容，由编排器补写（文件内留占位）。

用法:
  python build_novel_index.py --novel 影流之主-佚名 [--lib <索引库>] [--force]
  python build_novel_index.py --all [--lib <索引库>]    # 扫描全部小说
"""
import argparse
import datetime
import glob
import os
import re
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

SKILL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(SKILL_DIR, ".env"))
except Exception:
    pass

INDEX_LIB_DIR = os.environ.get("INDEX_LIB_DIR", "").strip() or os.path.join(SKILL_DIR, "索引库")

from _common import parse_frontmatter  # noqa: E402 共享严格 frontmatter 解析（单源真相）
from _logging import add_log_format_argument, configure_logging, get_logger

logger = get_logger(__name__)


def read_frontmatter(path):
    """读取文件 frontmatter（v4.1：改用 _common.parse_frontmatter，正文含 `---` 行不再误判；
    `- ` 列表行收集为包含元素）。返回 meta 字典。"""
    with open(path, "r", encoding="utf-8") as f:
        text = f.read()
    meta, _ = parse_frontmatter(text)
    return meta


def parse_novel_arg(s, lib):
    """'影流之主-佚名' → ('影流之主', '佚名')。
    v4.1：小说名/作者名本身含 '-' 时（如小说「斗罗-冰火」作者「魔厨」，
    段文件名为 斗罗-冰火-魔厨-001-idx.md），rsplit 会把作者猜错。改为在
    索引库中按文件名前缀反推：尝试所有拆分点，取第一个能在 章节索引/ 下
    匹配到索引文件的前缀（前缀最长优先，小说名尽量完整）；匹配不到时
    回退原 rsplit 行为（仍按最后一段为作者）。"""
    parts = s.split("-")
    if len(parts) < 2:
        return s, ""
    for i in range(len(parts) - 1, 0, -1):
        prefix = "-".join(parts[:i])
        author = "-".join(parts[i:])
        if glob.glob(os.path.join(lib, "章节索引",
                                  glob.escape(prefix) + "-" + glob.escape(author) + "-*-idx.md")):
            return prefix, author
    return parts[0], "-".join(parts[1:])


def last_chapter_of(ch_range):
    """章节范围 '第1章~第5章' / '第序章~第1章' → 末章名 '第5章'。"""
    if "~" in ch_range:
        return ch_range.split("~")[-1].strip()
    return ch_range.strip()


def seg_no_of(p):
    """从索引文件名提取末尾 3 位段编号（排序键）。提取不到 → 排最后。"""
    m = re.search(r"-(\d{3})-idx\.md$", os.path.basename(p))
    return int(m.group(1)) if m else 10**9


def extract_section(old_text, title, next_title=None):
    """提取旧小说索引的某小节正文（不含标题行）。
    v4.5（拷问定稿 Q12）：--force 刷新导航时保留编排器已补写的概况/亮点——
    小节含「（编排器补写）」占位符 → 返回 None（不保留，用新占位）；
    否则返回小节正文（供复用）。
    v4.6 修复（P3-6）：占位判定收窄为「（编排器补写）」专一匹配——此前额外判
    `"XXX" in sec`，编排器补写内容含「XXX」字样（如角色代号）会被误判未补写，
    --force 时静默丢补写内容。"""
    if next_title:
        m = re.search(rf"^## {title}\n(.*?)(?=^## {next_title}\n|\Z)", old_text, re.M | re.S)
    else:
        m = re.search(rf"^## {title}\n(.*?)(?=\Z)", old_text, re.M | re.S)
    if not m:
        return None
    sec = m.group(1).rstrip()
    if "（编排器补写）" in sec:
        return None
    return sec


def build_novel_index(novel, author, lib, force):
    prefix = f"{novel}-{author}" if author else novel
    # 按末尾段编号排序（v3.9 修复：basename 字典序会把「第10章~第14章-003」排在
    # 「第1章~第5章-001」前，导致导航乱序、最后索引章节推导错误）
    # v4 修复：glob.escape 前缀——小说名含 `[`（如「[综漫]」类网文标题）时，
    # `[` 会被 glob 当字符类通配符导致匹配不到（此前静默报「未找到」）
    idx_files = sorted(
        glob.glob(os.path.join(lib, "章节索引", glob.escape(prefix) + "-*-idx.md")),
        key=seg_no_of,
    )
    if not idx_files:
        logger.error(f"❌ 未找到 {prefix} 的章节索引（{lib}/章节索引/）")
        return 1

    rows, tag_counts, total_chapters, last_idx = [], {}, 0, ""
    finished_count = 0  # v4.5 修复(审查 Bug2)：聚合「是否完结」，不再硬编码连载中
    for p in idx_files:
        meta = read_frontmatter(p)
        seg_name = os.path.basename(p).replace("-idx.md", "")
        rows.append((os.path.basename(p), meta.get("章节范围", "?"), seg_name + ".md"))
        if "已完结" in meta.get("是否完结", ""):
            finished_count += 1
        try:
            total_chapters += int(meta.get("总章节数", 0))
        except (TypeError, ValueError):
            pass
        tags = meta.get("包含元素", [])
        if isinstance(tags, str):
            tags = [t for t in tags.splitlines() if t.strip()]
        for tag in tags:
            tag = tag.strip()
            if tag:
                tag_counts[tag] = tag_counts.get(tag, 0) + 1
        rng = meta.get("章节范围", "")
        if rng:
            last_idx = last_chapter_of(rng)

    today = datetime.date.today().isoformat()
    out_path = os.path.join(lib, "小说索引", f"{prefix}.md")
    if os.path.exists(out_path) and not force:
        logger.info("%s 已存在（--force 覆盖）", out_path)
        return 0
    # v4.5（拷问定稿 Q12）：--force 刷新导航时保留编排器已补写的概况/亮点，
    # 只刷新动态行（章节数/索引段数/导航/最后索引章节）——此前 force 重写
    # 会把补写内容静默覆盖回「（编排器补写）」占位
    old_overview = old_highlights = old_foreshadow = old_characters = None
    if os.path.exists(out_path):
        with open(out_path, encoding="utf-8") as f:
            old_text = f.read()
        old_overview = extract_section(old_text, "全书概况", "章节索引导航")
        old_highlights = extract_section(old_text, "全书亮点速查", "伏笔全局追踪表")
        old_foreshadow = extract_section(old_text, "伏笔全局追踪表", "人物关系链")
        old_characters = extract_section(old_text, "人物关系链")

    # 「是否完结」聚合(保守):全部段已完结 → 已完结;任一段连载中/未填 → 连载中(防假完结)
    finished_flag = "已完结" if idx_files and finished_count == len(idx_files) else "连载中"

    # 概况/亮点/聚合表正文:已补写(无占位)则保留并刷新动态行,否则用新占位
    if old_overview:
        overview_body = re.sub(r"- 章节数：[^\n]*", f"- 章节数：{total_chapters}", old_overview)
        overview_body = re.sub(r"- 索引段数：[^\n]*", f"- 索引段数：{len(rows)} 段", overview_body)
    else:
        overview_body = (f"- 章节数：{total_chapters}\n- 索引段数：{len(rows)} 段\n"
                         "- 主角：（编排器补写）\n- 主线：（编排器补写）")
    highlights_body = old_highlights or (
        "### 爽点密集区\n- （编排器补写）\n\n### 名场面\n- （编排器补写）\n\n### 金句梗\n- （编排器补写）")
    # v4.5 审查新增:伏笔全局追踪表/人物关系链(编排器跨段汇总补写;置于文件尾,
    # extract_section 按小节标题提取,force 刷新时已补写内容不丢)
    foreshadow_body = old_foreshadow or (
        "| 伏笔 | 埋设章节 | 状态 | 回收章节 |\n"
        "|------|----------|------|----------|\n"
        "| （编排器补写） | | | |")
    characters_body = old_characters or (
        "| 人物 | 阵营/身份 | 关键关系 | 主要出场段 |\n"
        "|------|-----------|----------|------------|\n"
        "| （编排器补写） | | | |")

    lines = [
        "---",
        f"小说名称: 《{novel}》",
        f"作者: {author}",
        "索引类型: 小说索引",
        "章节范围: 全书",
        f"总章节数: {total_chapters}",
        f"是否完结: {finished_flag}",
        "包含元素:",
    ]
    for tag in sorted(tag_counts, key=lambda t: -tag_counts[t]):
        lines.append(f"  - {tag}")
    lines += [
        f"创建日期: {today}",
        f"最后更新: {today}",
        "状态: 完成",
        f"最后索引章节: {last_idx}",
        "---",
        "",
        f"# 《{novel}》创意索引（小说索引）",
        "",
        "> 全书级导航索引：一部小说一个文件，平铺于 `小说索引/`。",
        "> 由 `scripts/build_novel_index.py` 自动生成骨架；概况/亮点为文字性内容，由编排器补写。",
        "",
        "## 全书概况",
        "",
        overview_body,
        "",
        "## 章节索引导航",
        "",
        "| 编号 | 章节范围 | 索引文件 | 段文件 |",
        "|------|----------|----------|--------|",
    ]
    for i, (idx_name, rng, seg_name) in enumerate(rows, 1):
        lines.append(f"| {i:03d} | {rng} | {idx_name} | {seg_name} |")
    lines += [
        "",
        "## 全书亮点速查",
        "",
        highlights_body,
        "",
        "## 伏笔全局追踪表",
        "",
        foreshadow_body,
        "",
        "## 人物关系链",
        "",
        characters_body,
        "",
    ]

    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    logger.info(f"✅ 已生成: {out_path}（{len(rows)} 段 | 总章节 {total_chapters} | 标签 {len(tag_counts)} 组）")
    return 0


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--novel", help="小说名-作者（如 影流之主-佚名）")
    parser.add_argument("--all", action="store_true", help="扫描索引库全部小说")
    parser.add_argument("--lib", default=INDEX_LIB_DIR, help="索引库根目录")
    parser.add_argument("--force", action="store_true", help="覆盖已存在的小说索引")
    add_log_format_argument(parser)
    args = parser.parse_args()
    configure_logging(fmt=args.log_format)

    if args.all:
        # v3.9 修复：此前把索引文件名（去掉 -idx.md）当小说名，每个索引文件被
        # 误认为一部小说（-003 被当作者）→ --all 从未生效
        # 正确做法：从索引 frontmatter 提取小说名称/作者（段文件 frontmatter 存的是
        # 规范化名，与文件名前缀一致）
        novels = set()
        for p in glob.glob(os.path.join(args.lib, "章节索引", "*-idx.md")):
            meta = read_frontmatter(p)
            n = meta.get("小说名称", "").strip("《》")
            a = meta.get("作者", "")
            if n:
                novels.add((n, a))
            else:
                logger.warning(f"⚠️ 跳过 {os.path.basename(p)}（frontmatter 缺小说名称/作者）")
        if not novels:
            logger.warning("索引库中无有效章节索引（frontmatter 缺小说名称）")
            return 1
        rc = 0
        for novel, author in sorted(novels):
            rc |= build_novel_index(novel, author, args.lib, args.force)
        return rc
    if args.novel:
        novel, author = parse_novel_arg(args.novel, args.lib)
        return build_novel_index(novel, author, args.lib, args.force)
    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
