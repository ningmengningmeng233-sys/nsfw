#!/usr/bin/env python3
"""
段文件拆分脚本 v4.0（P0-3 拆段脚本化）

源文件 → 索引库段文件，替代编排器手工拆段：
- 有章节：默认每 5 章一段（首尾段可不足；序章/楔子并入首段）
- 无章节：目标约 1 万字符一段，段落边界切分（\n\n 之间为一段，切点落段落结束），末段允许为剩余短段
- 段文件带最小 frontmatter（小说名称/作者/章节范围/对应索引/分段方式）
- 自动校验：每段非空、无重叠遗漏（各段拼接行数 ≈ 原文正文行数）
- 幂等：目标段文件已存在 → 跳过（--force 覆盖）
- --append 追更增量：源文件更新后只处理新增章节（不足 5 章并入末段，否则新建段），
  已有段文件保持原样 → 直调引擎断点只重跑末段，其余段跳过（v3.9 新增）
- 章节模式单源真相：chapter_patterns.py（v3.9 抽取，不再各自复制正则）

v4 修复：
- --volume 卷号前缀：分卷源文件切分时传（如 --volume 第5卷），段文件名与章节范围
  带卷号（第5卷第86章~第90章），防止多卷章号重名（卷内独立编号）导致同名段文件
  冲突静默丢段
- 幂等跳过前做内容比对：同名文件内容不同 → 计入冲突并报错退出（不静默跳过，
  防分卷/追更场景丢段）
- 自动校验落地：段区间连续性 + 每段非空 + 字符数守恒（此前 docstring 声称的
  「自动校验」未实现）

用法:
  python split_segments.py "<源文件>" --novel 小说名 --author 作者 [--lib <索引库>] [--volume 第X卷] [--chapters-per-seg 5] [--chars-per-seg 10000]
  python split_segments.py "<更新后的源文件>" --novel 小说名 --author 作者 --append   # 追更
"""
import argparse
import datetime as dt
import os
import re
import shutil
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

# 章节模式单源真相（v3.9：抽到 chapter_patterns.py，此处不再各自复制）
from chapter_patterns import CHAPTER_RE, normalize_ch_name, filter_volume_titles, title_level
# 共享日志工具
from _logging import add_log_format_argument, configure_logging, get_logger
# 共享文件工具（v4.1：编码回退链 + 严格 frontmatter 解析，单源真相）
from _common import norm_name, parse_frontmatter, read_text_auto

logger = get_logger(__name__)


def find_chapter_lines(body_lines):
    """返回 [(行号 1-based, 标题, 章名), ...]
    v4.1：章卷混合（卷标题+章标题并存）时卷标题不入列——此前「第一卷 + 第一章」
    式网文会把卷标题当一章，5 章/段分组错位、段范围退化为「第1节~第3章」。
    章名直接对完整标题规范化（normalize_ch_name 内部优先章级单位，支持
    「第1卷·第3章」式前缀）。"""
    out = []
    for i, line in enumerate(body_lines, 1):
        m = CHAPTER_RE.match(line.strip())
        if m:
            title = m.group(0).strip()
            nm = normalize_ch_name(title)
            # 合法的裸标题（如“第1章”）规范化后可能与原文完全相同，
            # 不能因此退化成按行号命名的“第N节”。
            normalized = nm if nm != title or title_level(title) == "chapter" else f"第{i}节"
            out.append((i, title, normalized))
    return filter_volume_titles(out)


def make_seg_content(fname, frontmatter, body):
    """构造段文件完整内容（frontmatter + 正文）。"""
    fm = (
        "---\n"
        f"小说名称: 《{frontmatter['novel']}》\n"
        f"作者: {frontmatter['author']}\n"
        f"章节范围: {frontmatter['chapter_range']}\n"
        f"对应索引: {fname.replace('.md', '-idx.md')}\n"
        f"分段方式: {frontmatter['split_mode']}\n"
        "---\n"
    )
    return fm + body


_STAGER = None  # main() 里启用；被其它模块/测试直接调用时保持旧的直写语义


class Stager:
    """v4.6.2 安全增量：先全部写暂存件 → 校验 → 隔离被取代件 → 原子替换。

    任何一步失败都清掉暂存件，活动段集与章节索引保持原样；被取代的段与其
    章节索引一起移到 ``<lib>/.superseded/<时间戳>/`` 下，可恢复、不删除。

    IDX-01：追更（--append）与分卷（--volume）是增量模式（incremental=True）：
    本次只新增段或原位更新末段，既有在用段全部是保留段，提交时严禁把未覆盖的
    既有段移入 .superseded（此前终态集合只含本次文件，提交即清空数据集）。
    全量重切（--force）仍走全量语义：不在终态集合里的旧段视为被取代并隔离。
    """

    SUFFIX = ".new-part"

    def __init__(self, lib, seg_dir, prefix, incremental=False):
        self.lib = str(lib)
        self.seg_dir = str(seg_dir)
        self.prefix = prefix
        self.incremental = bool(incremental)
        self.staged = {}      # 最终文件名 -> 暂存件路径
        self.kept = set()     # 内容未变、无需重写的既有文件
        self.temps = []

    def write(self, fname, content, force=False):
        out = os.path.join(self.seg_dir, fname)
        if os.path.exists(out):
            with open(out, "r", encoding="utf-8") as f:
                if f.read() == content:
                    self.kept.add(fname)
                    return "skipped"
            if not force:
                return "conflicted"
        tmp = out + self.SUFFIX
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(content)
        self.temps.append(tmp)
        self.staged[fname] = tmp
        return "written"

    def final_names(self):
        return set(self.staged) | self.kept

    def unowned_files(self):
        """段目录里不属于本书命名规范的文件（陌生件）——必须挡住替换。"""
        pat = re.compile(re.escape(self.prefix) + r"-.*-\d{3}\.md$")
        return sorted(p for p in os.listdir(self.seg_dir)
                      if p.endswith(".md") and not pat.match(p))

    def _index_of(self, seg_name):
        return os.path.join(self.lib, "章节索引", seg_name.replace(".md", "-idx.md"))

    def commit(self):
        for fname, tmp in sorted(self.staged.items()):
            with open(tmp, encoding="utf-8") as f:
                body = f.read()
            meta = parse_frontmatter(body)[0] if body.startswith("---") else ({}, "")
            if isinstance(meta, tuple):
                meta = meta[0]
            if not meta or not str(meta.get("小说名称", "")).strip() \
                    or not str(meta.get("章节范围", "")).strip():
                raise RuntimeError(f"暂存段校验未过：{fname}（frontmatter 缺小说名称或章节范围）")
        unowned = self.unowned_files()
        if unowned:
            raise RuntimeError(f"UNOWNED_FILE_IN_SEG_DIR：{unowned[:5]}；拒绝在陌生文件在场的情况下替换")
        keep = self.final_names()
        if self.incremental:
            # IDX-01 增量模式：本次未覆盖的既有在用段全部是保留段，
            # 终态集合扩大为“本次文件 + 全部既有段”，隔离扫描自然无命中。
            keep = keep | {p for p in os.listdir(self.seg_dir) if p.endswith(".md")}
        stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        moved = []
        for cur in sorted(os.listdir(self.seg_dir)):
            if not cur.endswith(".md") or cur in keep or cur.endswith(self.SUFFIX):
                continue
            qdir = os.path.join(self.lib, ".superseded", stamp, "小说原文", self.prefix)
            os.makedirs(qdir, exist_ok=True)
            src = os.path.join(self.seg_dir, cur)
            dst = os.path.join(qdir, cur)
            shutil.move(src, dst)
            moved.append(cur)
            idx = self._index_of(cur)
            if os.path.isfile(idx):
                qidx = os.path.join(self.lib, ".superseded", stamp, "章节索引")
                os.makedirs(qidx, exist_ok=True)
                shutil.move(idx, os.path.join(qidx, os.path.basename(idx)))
        for fname, tmp in sorted(self.staged.items()):
            shutil.move(tmp, os.path.join(self.seg_dir, fname))
            if tmp in self.temps:
                self.temps.remove(tmp)
        return moved

    def abort(self):
        for t in list(self.temps):
            try:
                os.remove(t)
            except OSError:
                pass
        self.temps.clear()


def write_seg_if_needed(seg_dir, fname, frontmatter, body, force):
    """写段文件；返回 'written' / 'skipped' / 'conflicted'。
    v4：幂等跳过前先比对内容——同名文件内容相同 → 跳过；内容不同 → 不覆盖，
    返回 conflicted（调用方汇总报错，防止分卷章号重名/源文件已变更时静默丢段）。"""
    content = make_seg_content(fname, frontmatter, body)
    if _STAGER is not None:
        return _STAGER.write(fname, content, force)
    out = os.path.join(seg_dir, fname)
    if os.path.exists(out) and not force:
        with open(out, "r", encoding="utf-8") as f:
            existing = f.read()
        if existing == content:
            return "skipped"
        return "conflicted"
    with open(out, "w", encoding="utf-8") as f:
        f.write(content)
    return "written"


def split_by_chapters(body_lines, chapters, per_seg, novel, author, seg_dir, force, volume=""):
    """按每 per_seg 章一段切分。

    多卷文件共用同一段号序列：本次生成的目标段不存在时从目录最大编号后接续；
    即使使用 ``--force`` 也不能让新卷重新从 001 开始，否则会破坏导航和追更锚点。
    同一卷的既有目标文件则保留其原编号，允许安全覆盖。
    """
    if per_seg <= 0:
        raise ValueError("chapters-per-seg 必须是正整数")
    base_no = 1
    segs = list_existing_segs(seg_dir)
    existing_by_prefix = {}
    allocated_numbers = {no for no, _ in segs}
    next_no = (max(allocated_numbers) + 1) if allocated_numbers else 1
    if volume:
        for no, name in segs:
            prefix = re.sub(r"-\d{3}\.md$", "", name)
            if prefix in existing_by_prefix and existing_by_prefix[prefix] != no:
                raise ValueError(f"校验失败: 同一段前缀存在多个编号: {prefix}")
            existing_by_prefix[prefix] = no

    created = 0
    conflicts = []
    prev_end = 0
    for idx in range(0, len(chapters), per_seg):
        grp = chapters[idx : idx + per_seg]
        start_line = grp[0][0]
        end_line = chapters[idx + per_seg][0] - 1 if idx + per_seg < len(chapters) else len(body_lines)
        start_name = grp[0][2]  # 已规范化：第序章 / 第1章
        last_name = grp[-1][2]
        ch_range = start_name if start_name == last_name else f"{start_name}~{last_name}"
        if volume:
            ch_range = f"{volume}{ch_range}"
        # 校验：段区间必须连续（首段允许源文件开头有卷首语等非章节内容）
        if prev_end and start_line != prev_end + 1:
            raise ValueError(f"校验失败: 段起始行 {start_line} 与上一段结束行 {prev_end} 不连续（重叠/遗漏）")
        prev_end = end_line
        prefix = f"{novel}-{author}-{ch_range}"
        if volume and prefix in existing_by_prefix:
            seg_no = existing_by_prefix[prefix]
        elif volume:
            seg_no = next_no
            next_no += 1
        else:
            seg_no = base_no + idx // per_seg
        fname = f"{prefix}-{seg_no:03d}.md"
        body = "\n".join(body_lines[start_line - 1 : end_line])
        if not body.strip():
            raise ValueError(f"校验失败: {fname} 段内容为空（行 {start_line}~{end_line}）")
        rc = write_seg_if_needed(seg_dir, fname, {
            "novel": novel, "author": author,
            "chapter_range": ch_range,
            "split_mode": "按章节切分",
        }, body, force)
        if rc == "skipped":
            logger.info(f"  ⏭️  {fname}: 已存在且内容一致（跳过）")
        elif rc == "conflicted":
            conflicts.append(fname)
            logger.warning(f"  ⚠️  {fname}: 已存在但内容不同（可能卷号重名/源文件已变更，未覆盖）")
        else:
            logger.info(f"  ✅ {fname}: 行 {start_line}~{end_line}（{grp[0][1]} → {grp[-1][1]}）")
            created += 1
    # 覆盖校验：末段 end_line 强制为 len(body_lines)，prev_end 恒等于原文行数；
    # 卷首语（首章标题行之前）不计入任何段，属设计语义。此处的防御性校验仅
    # 防止未来改动破坏「末段覆盖到文件尾」的不变量。
    assert prev_end == len(body_lines), f"校验失败: 各段覆盖到行 {prev_end}，原文共 {len(body_lines)} 行（漏段）"
    return created, conflicts


def split_by_chars(body_lines, chars_per_seg, novel, author, seg_dir, force):
    """无章节：按目标字符数切分，边界落在段落结束处。

    ``chars_per_seg`` 是目标下限；最后一段可以是剩余短段，这是不可避免的余量，
    不能为了满足下限而重复或丢弃原文。
    """
    if chars_per_seg <= 0:
        raise ValueError("chars-per-seg 必须是正整数")
    paras = []
    cur = []
    for line in body_lines:
        if line.strip() == "" and cur:
            paras.append("\n".join(cur))
            cur = []
        else:
            cur.append(line)
    if cur:
        paras.append("\n".join(cur))

    created = 0
    conflicts = []
    segs, buf, buf_len = [], [], 0
    for p in paras:
        buf.append(p)
        buf_len += len(p) + 1
        if buf_len >= chars_per_seg:
            segs.append("\n\n".join(buf))
            buf, buf_len = [], 0
    if buf:
        segs.append("\n\n".join(buf))

    # 校验：字符数守恒（段内与段间均以 \n\n 连接；段边界处断开的段落对不算段内连接，
    # 总连接数 = 段落数 - 段数。v4.1 修复：此前公式用 len(paras)-1（漏计段边界连接），
    # 任何切出 ≥2 段的场景都误报「段落丢失/重复」崩溃）
    expected = sum(len(p) for p in paras) + 2 * (len(paras) - len(segs))
    actual = sum(len(s) for s in segs)
    if actual != expected:
        raise ValueError(f"校验失败: 段文件拼接 {actual} 字符 ≠ 原文 {expected} 字符（段落丢失/重复）")
    if not segs or any(not s.strip() for s in segs):
        raise ValueError("校验失败: 存在空段")

    # 段名按实际累计字符计算。默认阈值沿用“万字”展示；自定义阈值使用
    # 精确字数展示，避免多个段都被错误命名为“第1万字~第1万字”。
    bounds, acc = [], 0
    for seg in segs:
        acc += len(seg)
        bounds.append(acc)
    total_chars = acc
    for i, seg in enumerate(segs, 1):
        if chars_per_seg == 10000:
            seg_start = (bounds[i - 2] if i > 1 else 0) // 10000 + 1
            seg_end = max(bounds[i - 1] // 10000, seg_start)
            chapter_range = f"第{seg_start}万字~第{seg_end}万字"
        else:
            seg_start = (bounds[i - 2] + 1) if i > 1 else 1
            seg_end = bounds[i - 1]
            chapter_range = f"第{seg_start}字~第{seg_end}字"
        fname = f"{novel}-{author}-{chapter_range}-{i:03d}.md"
        rc = write_seg_if_needed(seg_dir, fname, {
            "novel": novel, "author": author,
            "chapter_range": chapter_range,
            "split_mode": "按字数切分",
        }, seg, force)
        if rc == "skipped":
            logger.info(f"  ⏭️  {fname}: 已存在且内容一致（跳过）")
        elif rc == "conflicted":
            conflicts.append(fname)
            logger.warning(f"  ⚠️  {fname}: 已存在但内容不同（未覆盖）")
        else:
            logger.info(f"  ✅ {fname}: 段 {i}/{len(segs)}（约 {len(seg)} 字符）")
            created += 1
    logger.info(f"  ℹ️  原文约 {total_chars} 字符 → {len(segs)} 段（每段 ≥ {chars_per_seg} 字符按段落边界）")
    return created, conflicts


def list_existing_segs(seg_dir):
    """返回 [(编号, 文件名), ...] 按编号升序（不含 -idx 索引文件）。"""
    out = []
    for f in os.listdir(seg_dir):
        m = re.search(r"-(\d{3})\.md$", f)
        if m and not f.endswith("-idx.md"):
            out.append((int(m.group(1)), f))
    return sorted(out)


def append_new_chapters(body_lines, chapters, seg_dir, novel, author, per_seg, volume=""):
    """追更增量模式（--append）：只处理源文件中新增的章节（v3.9 新增）。
    1. 取现有最后一段的末章标题作锚点，在全书章节序列中定位（取最后出现位置防重名）
    2. 锚点之后的章节 = 新增：
       - 新增 ≥ per_seg 章 → 新建段（编号 = 现有最大 + 1）
       - 新增 < per_seg 章 → 并入末段（重写末段：frontmatter 章节范围更新 + 正文追加）
         末段内容变化 → 段文件 hash 失效 → 直调引擎只重跑该段（防行号漂移，其余段断点跳过）
    v4：--volume 卷号前缀与全量切分保持一致；新建段带幂等冲突检测。
    返回 (新建段数, 并入次数, 冲突列表)；锚点缺失返回 None（提示用 --force 全量重切）。"""
    segs = list_existing_segs(seg_dir)
    if not segs:
        logger.error("  ❌ --append 需要已有段文件（请先不带 --append 跑一次全量拆段）")
        return None
    last_no, last_fname = segs[-1]
    with open(os.path.join(seg_dir, last_fname), encoding="utf-8") as f:
        last_text = f.read()
    last_meta, last_body = parse_frontmatter(last_text)

    # 先用旧末段正文在更新后的正文中定位，避免同名番外/章节把锚点
    # 错配到新出现的同名标题。只有无法做正文锚定时才退回标题匹配，
    # 并明确要求用户用 --force 复核，不能静默猜测。
    source_body = "\n".join(body_lines)
    old_body = last_body.rstrip("\n")
    pos = None
    anchor = None
    if old_body:
        occurrences = []
        cursor = 0
        while True:
            found = source_body.find(old_body, cursor)
            if found < 0:
                break
            occurrences.append(found)
            cursor = found + 1
        if len(occurrences) == 0:
            logger.error("  ❌ 末段正文在更新后的源文件中找不到，源文件可能改写或不是同一本书；请用 --force 全量重切")
            return None
        if len(occurrences) > 1:
            logger.error(f"  ❌ 末段正文在更新后的源文件中有 {len(occurrences)} 个完全相同候选，无法安全增量；请用 --force 全量重切")
            return None
        old_start = occurrences[0]
        old_end = old_start + len(old_body)
        end_line = source_body[:old_end].count("\n") + 1
        candidates = [i for i, (line_no, _, _) in enumerate(chapters) if line_no <= end_line]
        if candidates:
            pos = candidates[-1]
            anchor = chapters[pos][1].strip()
    if pos is None:
        for m in CHAPTER_RE.finditer(last_body):
            anchor = m.group(0).strip()
        if not anchor:
            logger.error(f"  ❌ 末段 {last_fname} 内未识别到章节标题，无法增量；请用 --force 全量重切")
            return None
        matches = [i for i, (_, title, _) in enumerate(chapters) if title.strip() == anchor]
        if len(matches) != 1:
            logger.error(f"  ❌ 锚点章节「{anchor}」在源文件中有 {len(matches)} 个候选，无法安全增量；请用 --force 全量重切")
            return None
        pos = matches[0]

    new_chs = chapters[pos + 1:]
    if not new_chs:
        logger.info(f"  ℹ️  无新增章节（末段末章「{anchor}」已是全书最后一章）")
        return 0, 0, []

    created = merged = 0
    conflicts = []
    if len(new_chs) >= per_seg:
        for idx in range(0, len(new_chs), per_seg):
            grp = new_chs[idx: idx + per_seg]
            seg_no = last_no + idx // per_seg + 1
            start_line = grp[0][0]
            end_line = new_chs[idx + per_seg][0] - 1 if idx + per_seg < len(new_chs) else len(body_lines)
            start_name, last_name = grp[0][2], grp[-1][2]
            ch_range = start_name if start_name == last_name else f"{start_name}~{last_name}"
            if volume:
                ch_range = f"{volume}{ch_range}"
            fname = f"{novel}-{author}-{ch_range}-{seg_no:03d}.md"
            body = "\n".join(body_lines[start_line - 1: end_line])
            if not body.strip():
                raise ValueError(f"校验失败: {fname} 段内容为空（行 {start_line}~{end_line}）")
            rc = write_seg_if_needed(seg_dir, fname, {
                "novel": novel, "author": author,
                "chapter_range": ch_range,
                "split_mode": "按章节切分",
            }, body, force=False)
            if rc == "conflicted":
                conflicts.append(fname)
                logger.warning(f"  ⚠️  {fname}: 已存在但内容不同（未覆盖）")
            else:
                logger.info(f"  ✅ {fname}: 行 {start_line}~{end_line}（新增段）")
                created += 1
        return created, merged, conflicts

    # 新增不足 per_seg 章 → 并入末段（重写文件，hash 变化触发该段重跑）
    first_new = new_chs[0][0]
    merged_body = "\n".join(body_lines[first_new - 1:])
    new_body = last_body.rstrip("\n") + "\n" + merged_body
    old_first = None
    for m in CHAPTER_RE.finditer(last_body):
        old_first = normalize_ch_name(m.group(0).strip())
        break
    new_last = new_chs[-1][2]
    ch_range = f"{old_first}~{new_last}" if old_first and old_first != new_last else (old_first or new_last)
    if volume:
        ch_range = f"{volume}{ch_range}"
    content = make_seg_content(last_fname, {
        "novel": novel, "author": author,
        "chapter_range": ch_range,
        "split_mode": last_meta.get("分段方式") or "按章节切分",
    }, new_body)
    if _STAGER is not None:
        # IDX-01：并入末段必须走 Stager 暂存（原位更新、故意覆盖 → force=True），
        # 随事务统一校验与提交；直写会绕过校验且失败时无法回滚。
        rc = _STAGER.write(last_fname, content, force=True)
        if rc == "conflicted":  # force=True 下不应发生，防御性报错
            conflicts.append(last_fname)
            logger.warning(f"  ⚠️  {last_fname}: 并入末段未能暂存（未覆盖）")
            return created, merged, conflicts
    else:
        with open(os.path.join(seg_dir, last_fname), "w", encoding="utf-8") as f:
            f.write(content)
    logger.info(f"  ✅ {last_fname}: 并入 {len(new_chs)} 章（{new_chs[0][1]} → {new_chs[-1][1]}，章节范围 {ch_range}）")
    logger.info("     ℹ️  段文件名保持原样（防断点失效重跑全书），章节范围以 frontmatter 为准")
    return created, 1, conflicts


def main():
    parser = argparse.ArgumentParser(description="源文件 → 索引库段文件")
    parser.add_argument("source", help="源文件路径（.txt / .md）")
    parser.add_argument("--novel", required=True, help="小说名称（原始名，可含《》）")
    parser.add_argument("--author", required=True, help="作者")
    parser.add_argument("--lib", default=INDEX_LIB_DIR, help="索引库根目录（默认 .env INDEX_LIB_DIR）")
    parser.add_argument("--chapters-per-seg", type=int, default=5, help="每段章数（有章节时，默认 5）")
    parser.add_argument("--chars-per-seg", type=int, default=10000, help="每段字数下限（无章节时，默认 1 万）")
    parser.add_argument("--append", action="store_true", help="追更增量：只处理新增章节（并入末段或新建段）")
    parser.add_argument("--volume", default="", help="卷号前缀（分卷源文件切分时传，如 第5卷；段文件名与章节范围带卷号，防多卷章号重名冲突）")
    parser.add_argument("--force", action="store_true", help="覆盖已存在的段文件")
    add_log_format_argument(parser)
    args = parser.parse_args()
    configure_logging(fmt=args.log_format)

    if not os.path.isfile(args.source):
        logger.error(f"❌ 源文件不存在: {args.source}")
        return 1

    novel = norm_name(args.novel)
    author = norm_name(args.author)
    if not novel or not author:
        logger.error("❌ 小说名/作者不能为空（且需为合法文件名字符）")
        return 1

    seg_dir = os.path.join(args.lib, "小说原文", f"{novel}-{author}")
    os.makedirs(seg_dir, exist_ok=True)
    global _STAGER
    _STAGER = Stager(args.lib, seg_dir, f"{novel}-{author}",
                     incremental=bool(args.append or args.volume))

    try:
        text = read_text_auto(args.source)
    except (UnicodeDecodeError, OSError) as e:
        logger.error(f"❌ 无法读取源文件（不是 UTF-8/GB18030/GBK 编码？）: {e}")
        return 1
    _, body = parse_frontmatter(text)
    body_lines = body.splitlines()
    if not body_lines or not "".join(body_lines).strip():
        logger.error("❌ 源文件正文为空")
        return 1

    chapters = find_chapter_lines(body_lines)
    logger.info(f"📖 {args.novel}（{args.author}）: 正文 {len(body_lines)} 行, 识别到 {len(chapters)} 个章节标题")

    if args.append:
        if not chapters:
            logger.error("  ❌ --append 要求源文件有章节标题；无章节小说请用全量拆段")
            return 1
        res = append_new_chapters(body_lines, chapters, seg_dir, novel, author, args.chapters_per_seg, args.volume)
        if res is None:
            return 1
        created, merged, conflicts = res
        mode = f"增量追加（新建 {created} 段 / 并入末段 {merged} 次）"
    elif chapters:
        created, conflicts = split_by_chapters(body_lines, chapters, args.chapters_per_seg, novel, author, seg_dir, args.force, args.volume)
        mode = f"按章节切分（{args.chapters_per_seg} 章/段）"
    else:
        logger.warning("  ⚠️ 未识别到章节标题 → 按字数切分（段落边界）")
        created, conflicts = split_by_chars(body_lines, args.chars_per_seg, novel, author, seg_dir, args.force)
        mode = f"按字数切分（{args.chars_per_seg} 字符/段）"

    if conflicts:
        _STAGER.abort()
        logger.error(f"❌ {len(conflicts)} 个段文件同名但内容不同，未覆盖（可能是分卷章号重名或源文件已变更）:")
        for c in conflicts:
            logger.error(f"   - {c}")
        logger.error("  处理：分卷切分请加 --volume 第X卷；源文件变更请用 --force 覆盖（会触发对应段索引重跑）")
        return 1
    try:
        superseded = _STAGER.commit()
    except (RuntimeError, OSError) as e:
        _STAGER.abort()
        logger.error(f"❌ 拆段事务未提交，活动段集与章节索引保持原样：{e}")
        return 1
    if superseded:
        logger.info(f"♻️  被取代的段已可恢复隔离 {len(superseded)} 件（含其章节索引）："
                    f"{os.path.join(args.lib, '.superseded')}；未删除任何文件")
    if created == 0 and "增量" not in mode:
        logger.info("ℹ️  无新段生成（全部已存在，用 --force 可覆盖）")
    logger.info(f"✅ 拆段完成: {mode} | 输出: {seg_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
