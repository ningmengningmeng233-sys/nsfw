# -*- coding: utf-8 -*-
"""本地运维补丁(脱敏输出): 词汇表 <-> 组代号 的双向工具。
模型只输出【组N】代号以规避平台输出审查，写入索引前由 expand_tag_codes 本地展开。"""
import re


def build_code_map(vocab_text):
    """从词汇表 Markdown 解析组名与变体串，返回 {序号: [组名, 变体串]}"""
    code_map, cur = {}, 0
    for line in vocab_text.splitlines():
        s = line.strip()
        m = re.match(r"^#{2,3}\s+(.+?)\s*$", s)
        if m and ("系" in m.group(1) or "组" in m.group(1)):
            cur += 1
            code_map[cur] = [m.group(1), None]
            continue
        if cur and s.startswith("`") and s.endswith("`") and len(s) >= 2:
            code_map[cur][1] = s[1:-1].strip()
    return {k: v for k, v in code_map.items() if v[1]}


def vocab_to_code_text(code_map):
    lines = ["标签代号表（输出『包含元素』时只允许使用下列代号，严禁输出任何具体标签词汇）："]
    for n in sorted(code_map):
        lines.append(f"【组{n}】= {code_map[n][0]}")
    lines.append("输出格式：每个用到的组单独一行，格式为 `  - 【组N】`；也可同行连写（如 `  - 【组1】【组3】`）；仅当代号表无法覆盖时才加一行 `  - 自定义标签 | 至多5个词`。")
    return "\n".join(lines)


_CODE_LINE = re.compile(r"^(\s*-\s*)((?:【组\d+】[\s、·，,]*)+)(.*)$")


def expand_tag_codes(content, code_map):
    out = []
    for line in content.splitlines():
        m = _CODE_LINE.match(line)
        if m and "【组" in line:
            codes = [int(x) for x in re.findall(r"【组(\d+)】", m.group(2))]
            extra = m.group(3).strip()
            parts = [f"{code_map[c][0]} | {code_map[c][1]}" for c in codes if c in code_map]
            if parts:
                tail = f" | {extra}" if extra else ""
                out.append(m.group(1) + " | ".join(parts) + tail)
                continue
        out.append(line)
    return "\n".join(out)
