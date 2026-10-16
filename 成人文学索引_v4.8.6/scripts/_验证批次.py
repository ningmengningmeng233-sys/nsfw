# -*- coding: utf-8 -*-
"""验证批次 v4.0：索引库结构完整性校验
检查：三目录存在 / 段文件数=章节索引数 / 编号 001~N 连续 / frontmatter 字段 / 源文件回指 / 章节范围一致 / 行号抽查
v4：脚本移入 scripts/（数据目录保持纯净）——默认索引库改为 技能目录/索引库；
     --lib 参数优先（INDEX_LIB_DIR 外置时验证真实索引库）。
用法: python scripts/_验证批次.py [--lib <索引库根目录>]"""
import argparse
import os, re, sys
from collections import defaultdict

sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # Windows GBK 控制台兼容
sys.stderr.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _common import parse_frontmatter, resolve_under  # v4.5 修复(审查):统一复用 _common
from chapter_patterns import CHAPTER_RE  # 章节识别单源真相，避免只支持“第X章”

_argp = argparse.ArgumentParser()
_argp.add_argument("--lib", help="索引库根目录（默认 <技能目录>/索引库）")
_lib_args, _ = _argp.parse_known_args()
SKILL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LIB = _lib_args.lib or os.path.join(SKILL_DIR, "索引库")
SRC = os.path.join(LIB, "小说原文")
NOV = os.path.join(LIB, "小说索引")
IDX = os.path.join(LIB, "章节索引")

errors = []
ok = lambda msg: print(f"  ✓ {msg}")
bad = lambda msg: errors.append(msg) or print(f"  ✗ {msg}")

print("=== 验证批次 v4.6：索引库结构完整性 ===")

# 1. 三目录存在
print("[1] 目录结构")
for d, name in [(SRC, "小说原文"), (NOV, "小说索引"), (IDX, "章节索引")]:
    if os.path.isdir(d): ok(f"{name}/ 存在")
    else: bad(f"{name}/ 缺失")

# 2. 小说原文：每本书一个子目录，段文件平铺
print("[2] 小说原文")
book_dirs = [d for d in os.listdir(SRC) if os.path.isdir(os.path.join(SRC, d))] if os.path.isdir(SRC) else []
ok(f"小说数: {len(book_dirs)} ({book_dirs})")
seg_files = {}
for bd in book_dirs:
    # v3.9：只统计规范段文件（-NNN.md 结尾），与直调引擎目录扫描口径一致——
    # 此前目录里的说明文件（如 使用说明.md）会被误计为段文件导致数量校验失败
    fs = sorted([f for f in os.listdir(os.path.join(SRC, bd))
                 if re.search(r"-\d{3}\.md$", f)])
    seg_files[bd] = fs
    ok(f"{bd}: {len(fs)} 个段文件")
total_segs = sum(len(v) for v in seg_files.values())

# 3. 章节索引：编号连续 + 数量对应
# v4.6 修复(P2-1)：按书分组检查编号连续性——此前全库粒度，多书混放时
# A 书缺段但 B 书补齐编号会漏报（实测确认）。分组键 = frontmatter 小说名称+作者
# （文件名前缀不可靠：小说名/作者可能含「-」，且章节范围各异）
print("[3] 章节索引")
idx_files = sorted([f for f in os.listdir(IDX) if f.endswith("-idx.md")]) if os.path.isdir(IDX) else []
ok(f"章节索引文件数: {len(idx_files)} (段文件 {total_segs})")
if len(idx_files) != total_segs:
    bad(f"数量不对应: 索引 {len(idx_files)} vs 段文件 {total_segs}")
groups = defaultdict(list)
for fn in idx_files:
    m = re.search(r"-(\d{3})-idx\.md$", fn)
    if not m:
        bad(f"文件名编号异常: {fn}")
        continue
    try:
        with open(os.path.join(IDX, fn), encoding="utf-8-sig") as fh:
            fm, _ = parse_frontmatter(fh.read())
    except (OSError, UnicodeError) as e:
        bad(f"{fn}: 索引文件不可读: {e}")
        continue
    key = (fm.get("小说名称", "?"), fm.get("作者", "?"))
    groups[key].append(int(m.group(1)))
for (novel, author), nos in sorted(groups.items()):
    nos.sort()
    missing = [n for n in range(nos[0], nos[-1] + 1) if n not in nos]
    if missing:
        bad(f"{novel}-{author}: 编号不连续，缺: {missing}")
    else:
        ok(f"{novel}-{author}: 编号连续 {nos[0]:03d}~{nos[-1]:03d}（{len(nos)} 个）")

# 4. frontmatter 必填字段 + 源文件回指 + 章节范围一致
print("[4] frontmatter 与回指")
required = ["小说名称", "作者", "索引类型", "索引序号", "章节范围", "包含元素", "源文件", "创建日期"]
fm_bad = 0; src_bad = 0; range_bad = 0
for fn in idx_files:
    p = os.path.join(IDX, fn)
    try:
        with open(p, encoding="utf-8-sig") as f:
            t = f.read()
    except (OSError, UnicodeError) as e:
        fm_bad += 1
        bad(f"{fn}: 索引文件不可读: {e}")
        continue
    fm, _ = parse_frontmatter(t)  # v4.5 修复(审查):统一复用 _common 严格解析(单源真相)
    if not fm:
        fm_bad += 1; bad(f"{fn}: frontmatter 解析失败(格式不合法)"); continue
    for k in required:
        if k not in fm:
            fm_bad += 1; bad(f"{fn}: 缺字段 {k}")
    src = fm.get("源文件", "")
    src_path = resolve_under(LIB, src) if src else None
    if src and (src_path is None or not os.path.exists(src_path)):
        src_bad += 1; bad(f"{fn}: 源文件不存在或越出索引库 {src}")
    # 章节范围与段文件名一致性（编号相同则应指向同名段文件）
    m = re.search(r"-(\d{3})-idx\.md$", fn)
    if m and src:
        seg_name = os.path.basename(src)
        if m.group(1) not in seg_name:
            range_bad += 1; bad(f"{fn}: 段文件名 {seg_name} 编号不一致")
if not fm_bad: ok("frontmatter 全部可解析且字段齐全")
if not src_bad: ok("源文件回指全部有效")
if not range_bad: ok("索引编号与段文件编号一一对应")

# 5. 小说索引
print("[5] 小说索引")
nov_files = [f for f in os.listdir(NOV) if f.endswith(".md")] if os.path.isdir(NOV) else []
ok(f"小说索引文件: {len(nov_files)} ({nov_files})")
if nov_files:
    nov_bad = 0
    for fn in nov_files:
        try:
            with open(os.path.join(NOV, fn), encoding="utf-8-sig") as f:
                t = f.read()
        except (OSError, UnicodeError) as e:
            nov_bad += 1
            bad(f"{fn}: 小说索引不可读: {e}")
            continue
        fm, _ = parse_frontmatter(t)  # 统一复用 _common 严格解析
        if not fm:
            nov_bad += 1; bad(f"{fn}: frontmatter 解析失败(格式不合法)"); continue
        if fm.get("索引类型") == "小说索引":
            ok(f"{fn}: frontmatter 完整")
        else:
            nov_bad += 1; bad(f"{fn}: 小说索引 frontmatter 异常")
    if not nov_bad: ok("小说索引 frontmatter 全部合法")

# 6. 行号抽查（随机 3 个索引：章节范围起章标题应出现在段文件内）
print("[6] 行号抽查")
if idx_files:
    samples = idx_files[:min(3, len(idx_files))]
    for fn in samples:
        try:
            with open(os.path.join(IDX, fn), encoding="utf-8-sig") as f:
                t = f.read()
        except (OSError, UnicodeError) as e:
            bad(f"{fn}: 索引文件不可读: {e}")
            continue
        fm, _ = parse_frontmatter(t)  # 统一复用 _common 严格解析
        src_rel = fm.get("源文件") if fm else None
        if not src_rel:
            bad(f"{fn}: 缺少源文件字段，跳过行号抽查")
            continue
        src = resolve_under(LIB, src_rel)
        if src is None or not os.path.exists(src):
            bad(f"{fn}: 源文件不存在，跳过行号抽查: {src_rel}")
            continue
        try:
            with open(src, encoding="utf-8-sig") as f:
                seg_txt = f.read()
        except (OSError, UnicodeError) as e:
            bad(f"{fn}: 源文件不可读: {e}")
            continue
        # 找段文件内首个受支持的章节标题，复用 chapter_patterns 单源真相。
        split_mode = str(fm.get("分段方式", ""))
        if "字数" in split_mode:
            ok(f"{fn}: 按字数切分（无章节标题，合法）")
        else:
            title = next((line.strip() for line in seg_txt.splitlines()
                          if CHAPTER_RE.match(line.strip())), None)
            if title: ok(f"{fn}: 段文件内首个章节标题 '{title}'")
            else: bad(f"{fn}: 段文件内找不到受支持的章节标题")
        # 每章小节标题中的行号上限应 <= 段文件总行数；允许 ~ 两侧无空格。
        for mm in re.finditer(r"行号：\s*(\d+)\s*[~～\-—]\s*(\d+)", t):
            if int(mm.group(2)) > len(seg_txt.splitlines()):
                bad(f"{fn}: 行号 {mm.group(2)} 超出段文件行数 {len(seg_txt.splitlines())}")

print("\n=== 结果 ===")
if errors:
    print(f"失败项: {len(errors)}")
    for e in errors: print("  -", e)
    sys.exit(1)
print("全部通过 ✓")
