import logging
import os
import re
from pathlib import Path

logger = logging.getLogger(__name__)

_BUILTIN_VOCAB_NAME = "包含元素标签词汇表.md"
_CUSTOM_VOCAB_NAME = "tags.custom.yaml"

_GROUP_RE = re.compile(r"^###\s+(.+)$|^##\s+(.+)$|^#\s+(.+)$")
_TAG_LINE_RE = re.compile(r"^`([^`]+)`")


def _builtin_vocab_path() -> str:
    skill_dir = Path(__file__).resolve().parent.parent
    return str(skill_dir / "templates" / _BUILTIN_VOCAB_NAME)


def parse_builtin_vocab(text: str) -> dict[str, list[str]]:
    """Parse the built-in markdown vocabulary into groups.

    Groups are keyed by their Markdown heading text or inline group title.
    A group line like ``装逼打脸 | 打脸 | ...`` is also supported.
    """
    groups: dict[str, list[str]] = {}
    current = None
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        m = _GROUP_RE.match(line)
        if m:
            current = m.group(1) or m.group(2) or m.group(3)
            groups[current] = []
            continue
        if current is None:
            continue
        inline = _TAG_LINE_RE.match(line)
        if inline:
            groups[current].extend(t.strip() for t in inline.group(1).split("|") if t.strip())
        elif "|" in line and not line.startswith("#"):
            groups[current].extend(t.strip() for t in line.split("|") if t.strip())
    return groups


def load_custom_tags(path: str | os.PathLike) -> dict[str, list[str]]:
    """Load tags.custom.yaml.

    Expected format::

        - group: 爽点 · 装逼打脸系
          tags: [装逼打脸, 打脸, 扮猪吃虎]
        - group: 世界观元素 · 赛博朋克
          tags: [赛博朋克, 赛博, 义体, 黑客]

    Custom groups override built-in groups with the same name.
    """
    import yaml

    try:
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or []
    except FileNotFoundError:
        return {}
    except yaml.YAMLError as e:
        logger.warning("tags.custom.yaml 解析失败，将只使用内置词汇表: %s", e)
        return {}

    groups: dict[str, list[str]] = {}
    if isinstance(data, dict):
        for name, tags in data.items():
            groups[name] = [str(t).strip() for t in tags if str(t).strip()]
    elif isinstance(data, list):
        for item in data:
            if not isinstance(item, dict):
                continue
            name = item.get("group") or item.get("name")
            tags = item.get("tags") or item.get("values") or []
            if name and isinstance(tags, list):
                groups[str(name)] = [str(t).strip() for t in tags if str(t).strip()]
    return groups


def merge_vocab(builtin: dict[str, list[str]], custom: dict[str, list[str]]) -> dict[str, list[str]]:
    """Merge vocabulary groups. Custom groups override builtin groups with matching names."""
    merged = dict(builtin)
    for name, tags in custom.items():
        if name in merged:
            logger.debug("标签组 '%s' 被 custom 覆盖", name)
        merged[name] = tags
    return merged


def format_vocab(groups: dict[str, list[str]]) -> str:
    """Format merged groups back to a prompt-friendly Markdown text."""
    lines = ["# 包含元素·标签词汇表", ""]
    for name, tags in groups.items():
        if name.startswith("#"):
            lines.append(name)
        else:
            lines.append(f"## {name}")
        if tags:
            lines.append("`" + " | ".join(tags) + "`")
        lines.append("")
    return "\n".join(lines)


def load_tag_vocabulary(lib_dir: str | os.PathLike | None = None) -> str:
    """Load merged tag vocabulary.

    Reads the built-in vocabulary and an optional ``tags.custom.yaml`` from
    the index library root. Returns Markdown text suitable for inclusion in
    the LLM prompt.
    """
    builtin_path = _builtin_vocab_path()
    try:
        with open(builtin_path, "r", encoding="utf-8") as f:
            builtin_text = f.read()
    except OSError as e:
        logger.error("无法读取内置标签词汇表 %s: %s", builtin_path, e)
        return ""

    builtin = parse_builtin_vocab(builtin_text)
    custom: dict[str, list[str]] = {}
    if lib_dir:
        custom_path = Path(lib_dir) / _CUSTOM_VOCAB_NAME
        if custom_path.exists():
            logger.info("加载自定义标签扩展: %s", custom_path)
            custom = load_custom_tags(custom_path)

    merged = merge_vocab(builtin, custom)
    return format_vocab(merged)
