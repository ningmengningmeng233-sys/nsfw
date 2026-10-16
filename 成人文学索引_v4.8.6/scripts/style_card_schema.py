#!/usr/bin/env python3
"""风格卡 JSON Schema 的本地校验器（C7-2，零第三方依赖）。

为什么自己写：本项目运行环境只保证 requests/python-dotenv/PyYAML，不允许为了
校验一张风格卡再引入 jsonschema。这里实现的是 draft-07 的一个**够用子集**：
type / required / properties / items / additionalProperties / enum / pattern /
minLength / minimum。规则一律从 schema 文件读取，绝不把字段清单硬编码进 Python
——契约改了，校验行为跟着契约走。

schema 文件（schemas/style-card.schema.json）与技能分离存放（跨仓库交接契约），
查找顺序：
1. 环境变量 ``INDEX_STYLE_CARD_SCHEMA`` 指定的文件；
2. 技能仓库同级的 ``schemas/style-card.schema.json``（当前工作区的契约目录）；
3. 技能仓库内的 ``schemas/style-card.schema.json``（发行包可自行内置一份）。
都找不到时抛 :class:`StyleCardSchemaError`，报错只说人话（缺哪个路径），不外泄堆栈。
"""
from __future__ import annotations

import json
import os
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

SCHEMA_BASENAME = "style-card.schema.json"
_REPO_ROOT = Path(__file__).resolve().parent.parent

_TYPE_CHECKS = {
    "object": lambda v: isinstance(v, dict),
    "array": lambda v: isinstance(v, list),
    "string": lambda v: isinstance(v, str),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "boolean": lambda v: isinstance(v, bool),
    "null": lambda v: v is None,
}


class StyleCardSchemaError(RuntimeError):
    """schema 文件缺失或不可解析（不是数据不合格）。"""


def schema_candidates() -> list[str]:
    out: list[str] = []
    env = os.environ.get("INDEX_STYLE_CARD_SCHEMA", "").strip()
    if env:
        out.append(env)
    out.append(str(_REPO_ROOT.parent / "schemas" / SCHEMA_BASENAME))
    out.append(str(_REPO_ROOT / "schemas" / SCHEMA_BASENAME))
    return out


def resolve_schema_path() -> str:
    """返回首个存在的 schema 路径；都不存在时返回首选候选（供报错展示）。"""
    candidates = schema_candidates()
    for path in candidates:
        if Path(path).is_file():
            return path
    return candidates[0] if candidates else SCHEMA_BASENAME


@lru_cache(maxsize=8)
def load_schema(path: str) -> dict[str, Any]:
    file = Path(path)
    if not file.is_file():
        raise StyleCardSchemaError(
            "未找到风格卡 schema 文件 "
            f"{SCHEMA_BASENAME}，无法校验 style-card.json。"
            "请设置 INDEX_STYLE_CARD_SCHEMA 指向该文件，"
            f"或把它放到 {os.path.dirname(str(path))}{os.sep}。"
        )
    try:
        data = json.loads(file.read_text(encoding="utf-8"))
    except Exception as e:  # 损坏的 schema 也不该把堆栈甩给用户
        raise StyleCardSchemaError(f"schema 文件无法解析: {file} ({e})") from e
    if not isinstance(data, dict):
        raise StyleCardSchemaError(f"schema 文件内容不是 JSON 对象: {file}")
    return data


def default_schema() -> dict[str, Any]:
    return load_schema(resolve_schema_path())


def _type_name(expected: str) -> str:
    return {
        "object": "对象",
        "array": "数组",
        "string": "字符串",
        "integer": "整数",
        "number": "数字",
        "boolean": "布尔",
        "null": "空",
    }.get(expected, expected)


def _check_type(value: Any, expected: str, where: str, errors: list[str]) -> bool:
    check = _TYPE_CHECKS.get(expected)
    if check is None:  # 未知关键字：跳过而不是误报
        return True
    if not check(value):
        got = type(value).__name__
        errors.append(f"{where}: 应为{_type_name(expected)}，实际是 {got}")
        return False
    return True


def _validate(value: Any, schema: dict[str, Any], where: str, errors: list[str]) -> None:
    expected = schema.get("type")
    if isinstance(expected, list):
        if not any(_TYPE_CHECKS.get(t, lambda _v: True)(value) for t in expected):
            errors.append(f"{where}: 类型不符，期望其一为 {'/'.join(expected)}")
            return
    elif isinstance(expected, str):
        if not _check_type(value, expected, where, errors):
            return

    if "enum" in schema and value not in schema["enum"]:
        allowed = "、".join(str(x) for x in schema["enum"])
        errors.append(f"{where}: 取值 {value!r} 不在允许列表（{allowed}）")

    if isinstance(value, str):
        pattern = schema.get("pattern")
        if pattern and not re.search(pattern, value):
            errors.append(f"{where}: 值 {value!r} 不符合格式要求 {pattern}")
        min_length = schema.get("minLength")
        if isinstance(min_length, int) and len(value) < min_length:
            errors.append(f"{where}: 值不得为空或少于 {min_length} 字")

    if isinstance(value, (int, float)) and not isinstance(value, bool):
        minimum = schema.get("minimum")
        if isinstance(minimum, (int, float)) and value < minimum:
            errors.append(f"{where}: 值 {value} 不得小于 {minimum}")

    if isinstance(value, dict):
        for key in schema.get("required", []):
            if key not in value:
                errors.append(f"{where}: 缺少必填字段「{key}」")
        props = schema.get("properties", {})
        add = schema.get("additionalProperties", True)
        for key, sub in value.items():
            child = f"{where}.{key}" if not where.endswith("$") else f"$.{key}"
            if key in props:
                _validate(sub, props[key], child, errors)
            elif add is False:
                errors.append(f"{where}: 出现契约外字段「{key}」")
            elif isinstance(add, dict):
                _validate(sub, add, child, errors)

    if isinstance(value, list):
        items = schema.get("items")
        if isinstance(items, dict):
            for i, item in enumerate(value):
                _validate(item, items, f"{where}[{i}]", errors)
        min_items = schema.get("minItems")
        if isinstance(min_items, int) and len(value) < min_items:
            errors.append(f"{where}: 条目数不得少于 {min_items}")


def validate(instance: Any, schema: dict[str, Any] | None = None) -> list[str]:
    """按 schema 校验数据，返回人话错误列表（空列表 = 合格）。"""
    schema = schema or default_schema()
    errors: list[str] = []
    _validate(instance, schema, "$", errors)
    return errors


def validate_style_card(payload: Any, schema_path: str | None = None) -> list[str]:
    """校验一张风格卡数据。schema 文件缺失时抛 StyleCardSchemaError。"""
    return validate(payload, load_schema(schema_path) if schema_path else default_schema())


def main(argv: list[str] | None = None) -> int:
    import argparse
    import sys

    parser = argparse.ArgumentParser(description="校验 style-card.json 是否符合契约")
    parser.add_argument("card", help="style-card.json 路径")
    parser.add_argument("--schema", default=None, help="schema 文件路径（默认自动查找）")
    args = parser.parse_args(argv)

    try:
        payload = json.loads(Path(args.card).read_text(encoding="utf-8"))
        errors = validate_style_card(payload, args.schema)
    except StyleCardSchemaError as e:
        print(e, file=sys.stderr)
        return 2
    except Exception as e:
        print(f"风格卡文件无法读取或解析: {args.card} ({e})", file=sys.stderr)
        return 2
    if errors:
        print(f"风格卡不符合契约，共 {len(errors)} 处：", file=sys.stderr)
        for line in errors:
            print(f"- {line}", file=sys.stderr)
        return 1
    print(f"OK {args.card}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
