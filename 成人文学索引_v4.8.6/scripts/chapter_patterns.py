#!/usr/bin/env python3
"""
章节模式共享模块 v4.1（单源真相）

章节标题正则与章名规范化的唯一来源。split_segments.py 与
build_segment_index.py 均从此 import，禁止各自复制（此前双份定义，
改一处漏一处）。修改章节格式识别规则时只改本文件。

v4.1 变更（章节层级）：
- 新增 title_level / filter_volume_titles：章卷混合小说（网文常态「第一卷 + 第一章」）
  此前把卷标题当一章混入分段（章名退化「第N节」、段范围「第1节~第3章」错乱）。
  现在：卷级标题（第X卷/集、Book/Volume）与章级并存时，卷标题仅作分组标记，
  不进入章节列表；纯卷级章节（只有卷没有章）时卷即章节单位，正常入列。
- CH_NAME_RE / normalize_ch_name 补「卷集」单位：纯卷级书段范围命名「第1卷~第5卷」。
- CHAPTER_RE 英文分支 `\\s+` → `\\s*`：支持无空格「Chapter2」式标题。
"""
import re

# 章节标题（含无编号：序章/楔子/番外等）
# v4.5 审查修复（P1-1）：无编号词分支加前瞻约束——词后必须跟 空白/标点/行尾，
# 否则正文行（如「番外内容」「序章写完」）会被误判为章节标题导致静默错段
# （实测：正文一行「番外内容」→ 2 段错切成 3 段、段范围变「第1章~第番外」）
CHAPTER_RE = re.compile(
    r"^第[一二三四五六七八九十百零万千0-9]+[章节回卷话集][^\n]{0,40}"
    r"|^(?:Chapter|Book|Volume|Episode|Part)\s*\d+[^\n]{0,40}"
    r"|^\d+[\.\、．]\s[^\n]{0,40}"
    r"|^(?:序章|楔子|终章|番外|间章|尾声|后记)(?=[ \t：:，。．]|$)[^\n]{0,40}",
    re.M,
)
# 从标题提取章级名（优先章级：序章/楔子/第X章/第X回/「一章」式省略写法；「第一卷 序章」→「序章」）
# v3.9 修复：补 Book/Volume/Episode/Part 英文格式——此前 CHAPTER_RE 支持四种英文格式
# 但 CH_NAME_RE 只认 Chapter，其余格式退化命名「第N节」（N=行号），段文件名/章节范围不可读
# v4.1 修复：补「卷集」单位——纯卷级章节（只有「第一卷/第二卷」无章）此前同样退化命名
CH_NAME_RE = re.compile(
    r"第?[一二三四五六七八九十百零万千0-9]+[章回话节卷集]|序章|楔子|终章|番外|间章|尾声|后记|(?:Chapter|Book|Volume|Episode|Part)\s*\d+",
    re.I,
)

# 卷级标题开头（中文：第X卷/第X集；英文：Book/Volume）——用于层级过滤
_VOLUME_PREFIX_RE = re.compile(r"^第[一二三四五六七八九十百零万千0-9]+[卷集]|^(?:Book|Volume)\s*\d+", re.I)
# 章级单位（标题中出现即视为章级标题，即使带卷前缀如「第1卷·第3章」）
_CH_UNIT_RE = re.compile(r"[章回话节]|^(?:Chapter|Episode|Part)\s*\d+", re.I)

CN_DIGITS = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
             "六": 6, "七": 7, "八": 8, "九": 9}


def cn2ar(s):
    """中文数字 → 阿拉伯（支持到万级；网文千章是常态，万级兜底，亿级不存在不处理）。
    '一章'→1 '十二'→12 '三十五'→35 '一百零三'→103 '一千二百三十'→1230 '一万零五'→10005
    v4.5 修复：补千/万分支（此前只到百——千章级小说段文件名/导航/最后索引章节全错乱）。
    混合格式（中文+阿拉伯，如「一千零821」）按连续数字串累加 → 1821——
    此前静默忽略阿拉伯数字返回 1000，段文件名/导航全错（P1-2）。"""
    if s.isdigit():
        return int(s)
    total, num = 0, 0
    for ch in s:
        if ch in CN_DIGITS:
            num = CN_DIGITS[ch]
        elif ch.isdigit():
            num = num * 10 + int(ch)  # 混合格式：阿拉伯数字按连续串累加（P1-2）
        elif ch == "十":
            total += num * 10 if num else 10
            num = 0
        elif ch == "百":
            total += num * 100 if num else 100
            num = 0
        elif ch == "千":
            total += num * 1000 if num else 1000
            num = 0
        elif ch == "万":
            # 万级进位：'三万二千'→32000；'十万'→(10+0)*10000=100000
            total = (total + num) * 10000
            num = 0
    return total + num


def normalize_ch_name(raw):
    """章名规范化，与索引库既有命名对齐：'一章'→'第1章'、'第一章'→'第1章'、'序章'→'第序章'、
    'Chapter 2'/'Book 2'/'Volume 3' 等英文格式 → '第2章'。
    v4.1 分步重写：
    - 优先章级单位（章/回/话/节），允许卷前缀（'第1卷·第3章' → '第3章'）
    - 无编号词（序章/楔子等）→ '第序章'（'第一卷 序章' → '第序章'，保持既有命名）
    - 仅当整标题只有卷/集级单位（'第一卷'/'第2集 冒险'）→ '第1卷'/'第2集'（纯卷级章节命名）
    - 英文格式统一 → '第X章'"""
    m = re.search(
        r"(?:第(?:[一二三四五六七八九十百零万千0-9]+[卷集][·、\s-]*)?)?([一二三四五六七八九十百零万千0-9]+)([章回话节])",
        raw,
    )
    if m:
        return f"第{cn2ar(m.group(1))}{m.group(2)}"
    m2 = re.search(r"(序章|楔子|终章|番外|间章|尾声|后记)", raw)
    if m2:
        return f"第{m2.group(1)}"
    m3 = re.search(r"第?([一二三四五六七八九十百零万千0-9]+)([卷集])", raw)
    if m3:
        return f"第{cn2ar(m3.group(1))}{m3.group(2)}"
    m4 = re.search(r"(?:Chapter|Book|Volume|Episode|Part)\s*(\d+)", raw, re.I)
    return f"第{m4.group(1)}章" if m4 else raw


def title_level(title):
    """标题层级：'volume'（卷/集级）/ 'chapter'（章级）/ None（非章节标题）。
    v4.1 新增。卷级 = 第X卷/第X集 开头（且标题其余部分无章级单位）或英文 Book/Volume；
    「第1卷·第3章」式卷前缀章标题 → 章级。"""
    t = title.strip()
    if not CHAPTER_RE.match(t):
        return None
    if _VOLUME_PREFIX_RE.match(t):
        rest = _VOLUME_PREFIX_RE.sub("", t, count=1)
        if _CH_UNIT_RE.search(rest):
            return "chapter"  # 卷前缀 + 章级单位（第1卷·第3章）→ 章级
        return "volume"
    return "chapter"


def filter_volume_titles(titles):
    """章卷混合过滤（v4.1）：卷级与章级并存时，剔除卷级标题（卷仅作分组标记，
    不入章节列表）；全书只有卷级标题时保留（卷即章节单位）。
    titles 为标题字符串列表或 (行号, 标题, 章名) 元组列表，元素类型前后一致。"""
    if not titles:
        return titles
    is_vol = [title_level(t[1] if isinstance(t, tuple) else t) == "volume" for t in titles]
    if all(is_vol) or not any(is_vol):
        return titles  # 纯卷级（卷即章）或纯章级，无需过滤
    if isinstance(titles[0], tuple):
        return [t for t, v in zip(titles, is_vol) if not v]
    return [t for t, v in zip(titles, is_vol) if not v]


def extract_chapters(body):
    """从正文（不含 frontmatter）提取章节标题列表（含空白处理）。
    v4.1：章卷混合时卷标题不入列表（与 split_segments 口径一致）。"""
    titles = [m.group(0).strip() for m in CHAPTER_RE.finditer(body)]
    return filter_volume_titles(titles)
