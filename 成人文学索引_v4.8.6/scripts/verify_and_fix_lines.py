#!/usr/bin/env python3
"""
行号自动校准 v4.0（直调模式验证兜底，ADR-0001）

LLM 直调生成的行号偶有偏差（实测约 1/3 金句错位，区间/短语引用约 6% 章外）。
两类校准：
  1. 金句引用 `"原文"（行NNN，简述）`：
     取引号内原文做去标点归一化，在对应段文件中定位真实行号 → 修正
     找不到原文 → 保留原标注并标记「行号待核」
  2. 区间/短语引用 `短语（行N）` / `短语（行N~M）`（v3.9 新增）：
     取括号前短语做文本指纹回查段文件；**唯一命中**且修正后不越界 → 修正
     （区间保持原长度）；未命中 → 标记「区间待核」；多命中/模糊 → 跳过不改

配置：INDEX_LIB_DIR（.env）或 --lib 参数。
用法:
  python verify_and_fix_lines.py "<索引文件或目录>"... [--lib <索引库根目录>]
"""
import argparse
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

from _logging import add_log_format_argument, configure_logging, get_logger  # noqa: E402

logger = get_logger(__name__)

# 行号标注模式: "原文"（行NNN，简述）；逗号可省略（兼容 LLM 输出变体 "原文"（行N））
# v3.9 修复：兼容中文弯引号“原文”——中文模型常输出弯引号，此前只认直引号导致整段金句漏校准
# v4 修复：引号内长度下限 8 → 4 字符——短金句（如「莫欺少年穷」5 字）此前完全不进校准
# 盲区，错行号静默；4 字符以上引号+行号引用基本可确认是金句
# v4.1 修复：括号兼容英文 (行N) 与 )——LLM 输出英文括号时此前整条金句漏校准
# （区间引用 RANGE_PATTERN 的字符类本就兼容两种括号，金句反而不一致）
PATTERN = re.compile(r'([“"]([^“”"\n]{4,60})[”"])\s*[(（]行\s*(\d+)(?:\s*[,，]([^）)]*))?[)）]')
# v3.9 区间/短语引用模式: 短语（行N）或 短语（行N~M）；尾随（,、分隔的多行号等）单列为组4
# v3.9 修复：组1 排除右括号「）」——金句格式 “…”（行N，简述）的结尾「”）」会被
# 误截成短语导致探针过短跳过（每段必跳 1 条）
RANGE_PATTERN = re.compile(
    r"([^\s（）：\n，。；、）]{2,20}?)（行\s*(\d+)(?:\s*[~～\-—]\s*(\d+))?(?:\s*[,，、]([^）]*))?）"
)


def norm(s):
    """去标点/空白归一化（中文引号、英文引号、标点差异均不影响匹配）"""
    return re.sub(r"[\W_]+", "", s)


def find_real_line(seg_lines, quote, claimed):
    """定位金句真实行号。返回：
    None → 未命中（保持并标记待核）；
    -1 → 多命中且原行号不在命中集（跳过不改，防改错，与区间校准策略一致）；
    否则 → 真实行号（唯一命中，或原行号本身在命中集中则保持）。
    v4.6 修复（P3-2）：共享前缀多命中时扩展探针重试（15→25→35 字符）——
    此前固定前 15 字符，编号密集文本（「编号11-0」vs「编号11-1」式）多命中
    直接跳过，漏修正。扩展后仍多命中才跳过（不污染原则不变）。"""
    probe = norm(quote[:15])
    if not probe:
        return None
    hits = [i for i, line in enumerate(seg_lines, 1) if probe in norm(line)]
    if not hits:
        return None
    if claimed in hits:
        return claimed
    if len(hits) > 1:
        for width in (25, 35):
            probe2 = norm(quote[:width])
            if len(probe2) <= len(probe):
                continue
            hits2 = [i for i, line in enumerate(seg_lines, 1) if probe2 in norm(line)]
            if len(hits2) == 1:
                return hits2[0]
            if len(hits2) > 1:
                probe, hits = probe2, hits2  # 前缀仍共享，继续扩大
        return -1
    return hits[0]


def process_file(idx_path, args=None):
    seg_name = os.path.basename(idx_path).replace("-idx.md", "")
    # --lib 优先（修复 v3.32 及之前 --lib 不生效的问题）
    lib = (args.lib if args and getattr(args, "lib", None) else INDEX_LIB_DIR)
    seg_dir = os.path.join(lib, "小说原文")
    # v4 修复：glob.escape——小说名含 `[`（如「[综漫]」）时此前 glob 匹配不到段文件，校准静默空转
    seg_candidates = glob.glob(os.path.join(seg_dir, "**", glob.escape(seg_name) + ".md"), recursive=True)
    if not seg_candidates:
        logger.warning(f"⚠️ {os.path.basename(idx_path)}: 找不到对应段文件 {seg_name}")
        return 0, 0
    with open(seg_candidates[0], encoding="utf-8") as f:
        seg_lines = f.read().splitlines()
    with open(idx_path, encoding="utf-8") as f:
        content = f.read()

    fixed, kept, missing, skipped = 0, 0, 0, 0

    def repl(m):
        nonlocal fixed, kept, missing, skipped
        quote, claimed = m.group(2), int(m.group(3))
        tail = f"，{m.group(4)}" if m.group(4) else ""
        real = find_real_line(seg_lines, quote, claimed)
        if real == -1:
            skipped += 1
            # 多命中不污染（保持原样）；但原行号越界属必然错误，加「行号待核」提示
            if claimed > len(seg_lines) or claimed < 1:
                return f'{m.group(1)}（行{m.group(3)}{tail}[行号待核]）'
            return m.group(0)
        if real is None:
            missing += 1
            # 保留原引号风格；补回右括号
            return f'{m.group(1)}（行{m.group(3)}{tail}[行号待核]）'
        if real != claimed:
            fixed += 1
            # 保留原引号风格；补回右括号（v3.9 修复：此前修正分支丢失 「）」导致格式未闭合）
            return f'{m.group(1)}（行{real}{tail}）'
        kept += 1
        return m.group(0)

    new_content = PATTERN.sub(repl, content)
    rf, rm, rs = 0, 0, 0
    if not (args and getattr(args, "no_range", False)):
        new_content, rf, rm, rs = fix_range_refs(seg_lines, new_content)
    if new_content != content:
        with open(idx_path, "w", encoding="utf-8") as f:
            f.write(new_content)
    logger.info(f"  {os.path.basename(idx_path)}: 修正 {fixed} 条, 正确 {kept} 条, 待核 {missing} 条, 多命中跳过 {skipped} 条"
                f" | 区间: 修正 {rf} 待核 {rm} 跳过 {rs}")
    return fixed, missing


def fix_range_refs(seg_lines, content, max_lines=None):
    """v3.9 区间/短语引用校准：短语（行N~M）→ 短语文本指纹回查段文件。
    原则：只做高置信修正，不污染原文——未命中/多命中一律保持原样（不标记）。
    规则：短语回查**唯一命中**且修正后不越界 → 修正（区间保持原长度）；
    短语太短（<6）或含引号（金句格式已处理）→ 跳过；
    多行号引用（行N、行M 顿号/逗号分隔）→ 跳过。
    返回 (new_content, fixed, missing, skipped)，missing 仅统计不改写。"""
    if max_lines is None:
        max_lines = len(seg_lines)
    fixed = missing = skipped = 0

    def repl(m):
        nonlocal fixed, missing, skipped
        phrase, n1, n2 = m.group(1), int(m.group(2)), int(m.group(3)) if m.group(3) else None
        # 金句格式（引号开头或引号结尾，含右弯引号”）已被 PATTERN 处理，跳过；短语太短不可靠
        if '"' in phrase or "“" in phrase or "”" in phrase:
            skipped += 1
            return m.group(0)
        # 多行号引用（行N、行M）跳过
        tail = m.group(4) or ""
        if re.search(r"行\s*\d+", tail):
            skipped += 1
            return m.group(0)
        probe = norm(phrase)
        if len(probe) < 6:
            skipped += 1
            return m.group(0)
        hits = [i for i, line in enumerate(seg_lines, 1) if probe in norm(line)]
        if len(hits) != 1:
            if not hits:
                missing += 1  # 未命中：保持原样，仅统计
            else:
                skipped += 1  # 模糊匹配，不自动改
            return m.group(0)
        real = hits[0]
        if real != n1:
            # 修正：区间保持原长度；修正后必须不越界
            end = (real + (n2 - n1)) if n2 else real
            if end <= max_lines:
                fixed += 1
                rng = f"行{real}~{end}" if n2 else f"行{real}"
                # v4.1 修复：分隔逗号在捕获组外（正则 `[,，、]` 不捕获），此前修正后
                # 「行9~11区间引用」丢逗号，与金句修正分支（显式补「，」）格式不一致
                return f"{m.group(1)}（{rng}{('，' + tail) if tail else ''}）"
            skipped += 1
        return m.group(0)

    new_content = RANGE_PATTERN.sub(repl, content)
    return new_content, fixed, missing, skipped


# ---- v3.32 新增：金句引用一致性检查（P0-4，模板「引用一致性规则」的自动验证） ----
# 其他小节（剧情概要/爽点/伏笔）引用了行号原文 → 金句小节必须收录该行号。
# 只报告，不自动改（引用存在性检查，防「引用了却不收金句」复发）。
HEAD_RE = re.compile(r"^###\s+(.+)$")
RANGE_RE = re.compile(r"（行\s*(\d+)\s*[~～\-—]\s*(\d+)")
CH_RANGE_RE = re.compile(r"（行号：\s*(\d+)\s*[~～\-—]\s*(\d+)")


def split_sections(content):
    """按 ### 小节标题切分正文，返回 [(标题, 小节正文), ...]（不含标题行）。"""
    sections, cur_title, cur = [], None, []
    for line in content.splitlines():
        m = HEAD_RE.match(line)
        if m:
            if cur_title is not None:
                sections.append((cur_title, "\n".join(cur)))
            cur_title, cur = m.group(1), []
        elif cur_title is not None:
            cur.append(line)
    if cur_title is not None:
        sections.append((cur_title, "\n".join(cur)))
    return sections


def check_consistency(idx_path):
    """金句引用一致性：返回缺失行号数量（0 = 通过）。"""
    with open(idx_path, encoding="utf-8") as f:
        content = f.read()
    sections = split_sections(content)

    quote_secs = [s for t, s in sections if "金句" in t or "名场面" in t]
    other_secs = [(t, s) for t, s in sections if not ("金句" in t or "名场面" in t)]
    quote_nums = {int(m.group(3)) for s in quote_secs for m in PATTERN.finditer(s)}
    missing = sorted({
        int(m.group(3)) for t, s in other_secs for m in PATTERN.finditer(s)
        if int(m.group(3)) not in quote_nums
    })
    if missing:
        logger.warning(f"  ⚠️ [{os.path.basename(idx_path)}] 引用一致性: {len(missing)} 处引用未进金句小节（行号 {missing}）")
        return len(missing)
    return 0


def check_ranges(idx_path):
    """P1-6 check-only：章边界连续性 + 剧情概要块区间重叠检测（防缺陷 3 复发，不自动改）。
    只检测「剧情概要」小节内的相邻块区间——爽点/伏笔的引用式区间（行N~M，简述）不参与。"""
    with open(idx_path, encoding="utf-8") as f:
        content = f.read()
    issues = []
    ch_ranges = [(int(m.group(1)), int(m.group(2))) for m in CH_RANGE_RE.finditer(content)]
    for a, b in zip(ch_ranges, ch_ranges[1:]):
        if a[1] + 1 != b[0]:
            issues.append(f"章边界不连续: ({a[0]}~{a[1]}) → ({b[0]}~{b[1]})")

    # 按小节分割，只取「剧情概要」小节
    sections = split_sections(content)
    for title, sec in sections:
        if "剧情概要" not in title:
            continue
        ranges = [(int(m.group(1)), int(m.group(2))) for m in RANGE_RE.finditer(sec)]
        for a, b in zip(ranges, ranges[1:]):
            if b[0] <= a[1]:
                issues.append(f"概要区间重叠（{title}）: ({a[0]}~{a[1]}) ∩ ({b[0]}~{b[1]})")

    if issues:
        logger.warning(f"  ⚠️ [{os.path.basename(idx_path)}] 区间检查 {len(issues)} 项（check-only，不自动改）:")
        for i in issues[:10]:
            logger.warning(f"      - {i}")
    return len(issues)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("targets", nargs="+", help="索引文件或目录")
    parser.add_argument("--lib", default=INDEX_LIB_DIR, help="索引库根目录")
    parser.add_argument("--no-check", action="store_true", help="跳过一致性/区间检查（只做行号校准）")
    parser.add_argument("--no-range", action="store_true", help="跳过区间/短语引用校准（只做金句校准）")
    add_log_format_argument(parser)
    args = parser.parse_args()
    configure_logging(fmt=args.log_format)

    targets = []
    for a in args.targets:
        if os.path.isdir(a):
            targets.extend(glob.glob(os.path.join(a, "*-idx.md")))
        else:
            targets.append(a)

    total_fixed = total_missing = total_consistency = total_ranges = 0
    for t in sorted(set(targets)):
        f, m = process_file(t, args)
        total_fixed += f
        total_missing += m
        if not args.no_check:
            total_consistency += check_consistency(t)
            total_ranges += check_ranges(t)
    logger.info(f"\n📊 校准完成: 共修正 {total_fixed} 条行号, 待人工核 {total_missing} 条")
    if not args.no_check:
        logger.info(f"🔍 检查完成: 引用一致性 {total_consistency} 处缺失, 区间问题 {total_ranges} 项（均为 check-only）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
