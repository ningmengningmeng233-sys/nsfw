from pathlib import Path

# -*- coding: utf-8 -*-
"""共享文件工具 v4.1（单源真相）：norm_name / read_text_auto / parse_frontmatter。

split_segments.py 与 build_segment_index.py 此前各自复制同一份
read_text_auto + parse_frontmatter，改一处漏一处——v4.1 抽取到本文件。
修改编码回退链或 frontmatter 解析规则时只改本文件。
"""
import os
import re

# frontmatter 行格式：键值行 / 列表项 / 注释 / 空行（防正文 `---` 误判的严格性校验）
_FM_ITEM_RE = re.compile(r"^[^:#][^:]{0,40}:\s*.*$|^-\s+.*$|^#.*$|^\s*$")
_LIST_KEYS = {"包含元素"}


def book_key(novel, author):
    """由小说名/作者生成索引库内统一标识 `<novel>-<author>`。

    拒绝空值、`.`/`..` 和首尾连字符，防止路径逃逸或匹配到库根目录。
    """
    key = f"{norm_name(novel)}-{norm_name(author)}"
    if not key or key in (".", "..") or key.startswith("-") or key.endswith("-"):
        raise ValueError(f"小说名/作者规范化后不是合法书名标识: {key!r}")
    return key


def norm_name(name):
    """文件名规范化：去《》、空白与 Windows 非法字符，仅保留中英文/数字/连字符。

    注意：`[` `]` 属于合法文件名字符且网文书名常见，本函数保留它们；因此
    任何针对书名的匹配都必须用**字面量前缀匹配**（iterdir + startswith）或
    glob.escape，禁止把未转义书名直接拼进 glob 模式。
    """
    return re.sub(r'[\\/:*?"<>|\s《》]', "", name or "")


def resolve_under(root, relative):
    """将索引库内相对路径安全解析为绝对路径。

    返回 ``None`` 表示路径为空、跨盘符或试图逃出索引库根目录；调用方应将其
    视为校验失败，而不是继续读取宿主文件系统的任意路径。
    """
    if not relative:
        return None
    root_abs = os.path.abspath(root)
    candidate = os.path.abspath(os.path.join(root_abs, str(relative).replace("\\", os.sep)))
    try:
        common = os.path.commonpath([os.path.normcase(root_abs), os.path.normcase(candidate)])
    except ValueError:
        return None
    return candidate if common == os.path.normcase(root_abs) else None


def read_text_auto(path):
    """读取文本文件：UTF-8（含 BOM）优先，回退 GB18030/GBK。

    网文 txt 常见 GBK 编码；UTF-8 BOM 若不剥离会让首个章节标题变成
    ``\\ufeff第一章``，章节正则无法匹配并静默丢掉第一章，因此首选
    ``utf-8-sig``。
    """
    last_err = None
    for enc in ("utf-8-sig", "gb18030", "gbk"):
        try:
            with open(path, "r", encoding=enc) as f:
                return f.read()
        except UnicodeDecodeError as e:
            last_err = e
    raise last_err


def parse_frontmatter(text):
    """严格 frontmatter 解析。返回 (meta, body)。

    v4.1 严格化（此前 `text.find("\\n---", 3)` 只找第一处 `---` 行——源文件为 md 且
    正文含 Markdown 分隔线时，frontmatter 被误判结束、正文被吞）：
    - 仅当文本以 `---` 开头且其后存在**独立 `---` 行**闭合
    - frontmatter 区每行必须是 键值 / 列表项 / 注释 / 空行 格式，否则视为无 frontmatter
    - 连续 `- ` 列表项归入上一 key（列表值）；键值行可含 `《》` 等全角字符
    解析失败一律返回 ( {}, text )，绝不吞正文。
    """
    if text.startswith("﻿"):
        text = text[1:]
    if not text.startswith("---"):
        return {}, text
    lines = text.splitlines()
    end = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), None)
    if end is None:
        return {}, text
    fm_lines = lines[1:end]
    # lstrip 后校验：兼容 LLM 生成的缩进列表项（`  - 标签`）
    if not all(_FM_ITEM_RE.match(ln.lstrip()) for ln in fm_lines):
        return {}, text
    meta, cur_key = {}, None
    for ln in fm_lines:
        s = ln.strip()
        if not s or s.startswith("#"):
            continue
        if s.startswith("- "):
            item = s[2:].strip()
            if cur_key in _LIST_KEYS and item:
                if not isinstance(meta.get(cur_key), list):
                    meta[cur_key] = []
                meta[cur_key].append(item)
            continue
        if ":" in ln:
            k, v = ln.split(":", 1)
            cur_key = k.strip()
            meta[cur_key] = v.strip().strip("'\"")
    return meta, "\n".join(lines[end + 1:])

def prefixed_files(directory: Path, prefix: str, suffix: str | None = None) -> list[Path]:
    """字面量前缀匹配，替代 glob（书名含 [ 等元字符时 glob 会失效）。"""
    if not directory.is_dir():
        return []
    out = []
    for p in directory.iterdir():
        if not p.is_file() or not p.name.startswith(prefix + "-"):
            continue
        if suffix is None or p.name.endswith(suffix):
            out.append(p)
    return sorted(out)
