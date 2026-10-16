#!/usr/bin/env python3
"""
全库行号引用体检 v4.0（ADR-0002 治理复测工具）

扫描索引库全部章节索引，检测行号引用两类错误（与 ADR-0002 全库体检口径一致）：
1. 越界引用（行号 > 段文件总行数，必然错误）
2. 章外引用（行号不在所属章小节「（行号：a ~ b）」范围内，疑似错误）
   章外再按偏差分档：≤10 轻微 / 11~50 中等 / >50 大偏差（跨段串用/丢位）

金句与区间引用正则直接复用 verify_and_fix_lines.py（单源真相）。

用法:
  python check_line_refs.py [--lib <索引库根目录>] [--detail]
"""
import argparse
import glob
import os
import re
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

SKILL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(SKILL_DIR, "scripts"))
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(SKILL_DIR, ".env"))
except Exception:
    pass

INDEX_LIB_DIR = os.environ.get("INDEX_LIB_DIR", "").strip() or os.path.join(SKILL_DIR, "索引库")

from verify_and_fix_lines import PATTERN, RANGE_PATTERN  # noqa: E402 复用行号正则（单源真相）
from _logging import add_log_format_argument, configure_logging, get_logger
from _common import resolve_under

logger = get_logger(__name__)

# 章小节标题: ## 第X章 章节名（行号：a ~ b）或（行号：a）
CH_RANGE_RE = re.compile(r"^##\s+(\S+.*?)（行号：\s*(\d+)(?:\s*[~～\-—]\s*(\d+))?）", re.M)


def load_seg_lines(lib, idx_path):
    """从索引 frontmatter 的源文件字段定位段文件。"""
    try:
        with open(idx_path, encoding="utf-8-sig") as f:
            text = f.read()
    except (OSError, UnicodeError) as e:
        return None, f"索引文件不可读: {e}"
    m = re.search(r"^源文件:\s*(\S+)$", text, re.M)
    if not m:
        return None, "frontmatter 缺源文件字段"
    src = m.group(1).strip()
    seg_path = resolve_under(lib, src)
    if seg_path is None:
        return None, f"源文件路径越出索引库: {src}"
    if not os.path.exists(seg_path):
        return None, f"段文件不存在: {src}"
    try:
        with open(seg_path, encoding="utf-8-sig") as f:
            return f.read().splitlines(), None
    except (OSError, UnicodeError) as e:
        return None, f"段文件不可读: {e}"


def parse_ch_ranges(content):
    """解析章小节行号范围。返回 [(章名, 起始, 结束)]，结束缺失时取起始。"""
    out = []
    for m in CH_RANGE_RE.finditer(content):
        a = int(m.group(2))
        b = int(m.group(3)) if m.group(3) else a
        out.append((m.group(1).strip(), a, b))
    return out


def classify(n, ranges):
    """行号 n 相对章范围：'in' 章内；其余一律视为章外（含章间空隙，调用方统一统计）。"""
    for name, a, b in ranges:
        if a <= n <= b:
            return "in"
    return "gap"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--lib", default=INDEX_LIB_DIR, help="索引库根目录")
    parser.add_argument("--detail", action="store_true", help="逐条列出问题引用")
    add_log_format_argument(parser)
    args = parser.parse_args()
    configure_logging(fmt=args.log_format)

    idx_dir = os.path.join(args.lib, "章节索引")
    # v4：--lib 为用户输入，glob.escape 防目录名含 `[` 时匹配不到
    files = sorted(glob.glob(os.path.join(glob.escape(idx_dir), "*-idx.md")))
    if not files:
        logger.error(f"❌ 索引库无章节索引: {idx_dir}")
        return 1

    total = out_of_range = ch_out = 0
    errors = 0
    buckets = {"minor": 0, "mid": 0, "big": 0}
    dirty_files = set()
    details = []

    for p in files:
        seg_lines, err = load_seg_lines(args.lib, p)
        try:
            with open(p, encoding="utf-8-sig") as f:
                content = f.read()
        except (OSError, UnicodeError) as e:
            errors += 1
            dirty_files.add(os.path.basename(p))
            logger.warning(f"⚠️ {os.path.basename(p)}: 索引文件不可读: {e}（跳过）")
            continue
        if err:
            errors += 1
            dirty_files.add(os.path.basename(p))
            logger.warning(f"⚠️ {os.path.basename(p)}: {err}（跳过）")
            continue
        max_line = len(seg_lines)
        ranges = parse_ch_ranges(content)
        if not ranges:
            # v4.2 修复：无章节小说（按字数切分）索引无「## 第X章（行号：a ~ b）」小节——
            # 此前章外判定把合法行号全标「章外」误报；无章范围时只查越界（必然错误）
            logger.info(f"  ℹ️ {os.path.basename(p)}: 无章小节行号范围（按字数切分？），仅查越界")
        refs = []
        for m in PATTERN.finditer(content):
            n = int(m.group(3))
            refs.append((n, n, f"金句「{m.group(2)[:20]}…」"))
        for m in RANGE_PATTERN.finditer(content):
            phrase = m.group(1)
            if '"' in phrase or "“" in phrase or "”" in phrase:
                continue  # 金句已由 PATTERN 统计，避免双重计数
            start = int(m.group(2))
            end = int(m.group(3)) if m.group(3) else start
            label = f"区间「{phrase}…」" if m.group(3) else f"短语「{phrase}…」"
            refs.append((start, end, label))

        for start, end, label in refs:
            total += 1
            if start < 1 or end < 1 or start > max_line or end > max_line or end < start:
                out_of_range += 1
                dirty_files.add(os.path.basename(p))
                bad_line = end if end > max_line or end < 1 else start
                kind = f"越界(> {max_line})" if 1 <= bad_line <= max_line else f"越界(范围 1~{max_line})"
                if end < start:
                    kind = "区间倒序"
                details.append((os.path.basename(p), bad_line, kind, label))
                continue
            if ranges:
                outside = [n for n in {start, end} if classify(n, ranges) != "in"]
                if outside:
                    ch_out += 1
                    dirty_files.add(os.path.basename(p))
                    dev = min(min(abs(n - a), abs(n - b)) for n in outside for _, a, b in ranges)
                    bkt = "minor" if dev <= 10 else ("mid" if dev <= 50 else "big")
                    buckets[bkt] += 1
                    details.append((os.path.basename(p), outside[0], f"章外(偏差{dev})", label))

    logger.info(f"📊 体检 {len(files)} 个索引, {total} 处行号引用")
    logger.info(f"  ✅ 正常: {total - out_of_range - ch_out} 处")
    logger.error(f"  ❌ 越界（必然错误）: {out_of_range} 处")
    logger.warning(f"  ⚠️ 章外（疑似错误）: {ch_out} 处"
                   f"（轻微≤10: {buckets['minor']} / 中等11~50: {buckets['mid']} / 大偏差>50: {buckets['big']}）")
    logger.info(f"  🗂 涉及索引文件: {len(dirty_files)} / {len(files)}")

    if args.detail:
        for fn, n, kind, label in details[:100]:
            logger.info(f"    - {fn}: 行 {n} {kind} {label}")
        if len(details) > 100:
            logger.info(f"    … 其余 {len(details) - 100} 条略（--detail 仅列前 100）")

    return 1 if (errors or out_of_range or ch_out) else 0


if __name__ == "__main__":
    sys.exit(main())
