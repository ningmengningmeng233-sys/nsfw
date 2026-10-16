# -*- coding: utf-8 -*-
"""source-manifest.json 转录交付清单的处理与校验工具（W03）。

提供：
1. 产生符合 source-manifest.schema.json 的清单；
2. 校验清单结构、txt 相对路径文件存在性及 SHA-256 内容完整性；
3. 供索引端（成人文学索引/知识库转录）判定三种输入状态：新条目、重转更新、无变化；
4. 拒收文件丢失、哈希篡改或未完成状态的交付件。
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "1.0.0"


def sha256_file(path: Path) -> str:
    """计算文件的 SHA-256 十六进制字符串。"""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(65536):
            h.update(chunk)
    return h.hexdigest()


def create_manifest_item(
    *,
    source_id: str,
    platform: str,
    source_url: str,
    txt_path: str,
    sha256: str,
    status: str = "completed",
    title: str | None = None,
    part_index: int = 1,
    evidence_path: str | None = None,
    audio_duration_seconds: float | None = None,
    has_summary: bool = False,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """生成单个清单条目字典。"""
    item: dict[str, Any] = {
        "source_id": str(source_id),
        "platform": platform,
        "source_url": source_url,
        "part_index": int(part_index),
        "txt_path": txt_path.replace("\\", "/"),
        "sha256": sha256.lower(),
        "status": status,
    }
    if title is not None:
        item["title"] = title
    if evidence_path is not None:
        item["evidence_path"] = evidence_path.replace("\\", "/")
    if audio_duration_seconds is not None:
        item["audio_duration_seconds"] = float(audio_duration_seconds)
    item["has_summary"] = bool(has_summary)
    if extra:
        item.update(extra)
    return item


def build_manifest(
    *,
    generator: str,
    items: list[dict[str, Any]],
    batch_id: str | None = None,
    generated_at: str | None = None,
) -> dict[str, Any]:
    """生成完整 source-manifest.json 结构。"""
    if not items:
        raise ValueError("清单条目 items 不能为空")
    manifest: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "generator": generator,
        "generated_at": generated_at or datetime.now(timezone.utc).isoformat(),
        "items": items,
    }
    if batch_id:
        manifest["batch_id"] = batch_id
    return manifest


def validate_manifest_file(manifest_path: Path | str) -> tuple[bool, list[str]]:
    """校验清单本身及其引用的实体文件。

    返回 (ok: bool, errors: list[str])。
    检查项：
    - JSON 格式与必填顶层字段
    - 各 item 必填字段
    - txt_path 指向的文件必须真实存在
    - 文件实测 SHA-256 必须与声明的 sha256 完全一致
    """
    path = Path(manifest_path).resolve()
    if not path.is_file():
        return False, [f"清单文件不存在：{path}"]

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        return False, [f"清单文件解析失败：{exc}"]

    errors: list[str] = []
    if not isinstance(data, dict):
        return False, ["清单根必须是 JSON 对象"]

    for req in ("schema_version", "generator", "generated_at", "items"):
        if req not in data:
            errors.append(f"缺少顶层必填字段: {req}")

    items = data.get("items")
    if not isinstance(items, list) or len(items) == 0:
        errors.append("items 必须是非空列表")
        return False, errors

    base_dir = path.parent
    seen_ids: set[str] = set()

    for idx, item in enumerate(items):
        prefix = f"items[{idx}]"
        if not isinstance(item, dict):
            errors.append(f"{prefix} 必须是对象")
            continue

        for f in ("source_id", "platform", "source_url", "txt_path", "sha256", "status"):
            if f not in item:
                errors.append(f"{prefix} 缺少必填字段: {f}")

        sid = item.get("source_id")
        if sid:
            if sid in seen_ids:
                errors.append(f"{prefix} source_id '{sid}' 重复")
            seen_ids.add(sid)

        txt_rel = item.get("txt_path")
        declared_sha = item.get("sha256", "")
        if txt_rel:
            txt_file = (base_dir / txt_rel).resolve()
            if not txt_file.is_file():
                errors.append(f"{prefix} 引用的文本文件不存在: {txt_rel}")
            else:
                real_sha = sha256_file(txt_file)
                if real_sha.lower() != declared_sha.lower():
                    errors.append(
                        f"{prefix} 文件哈希不匹配: 声明 {declared_sha} != 实际 {real_sha} ({txt_rel})"
                    )

    return len(errors) == 0, errors


def classify_for_ingest(
    manifest_path: Path | str,
    known_sources: dict[str, str] | None = None,
) -> dict[str, Any]:
    """供索引端摄取时分诊条目。

    known_sources: {source_id: 现有库记录中的 sha256}
    返回分诊结果：
      - new: 新条目列表（库中不存在）
      - updated: 已存在但哈希变化的重转条目列表
      - unchanged: 已存在且哈希无变化的条目列表
      - ignored: 状态非 completed 被跳过的条目列表
      - errors: 校验失败原因列表
    """
    ok, errors = validate_manifest_file(manifest_path)
    if not ok:
        return {"ok": False, "errors": errors, "new": [], "updated": [], "unchanged": [], "ignored": []}

    path = Path(manifest_path).resolve()
    data = json.loads(path.read_text(encoding="utf-8"))
    known = known_sources or {}

    res: dict[str, Any] = {
        "ok": True,
        "errors": [],
        "new": [],
        "updated": [],
        "unchanged": [],
        "ignored": [],
    }

    base_dir = path.parent
    for item in data.get("items", []):
        sid = item["source_id"]
        status = item.get("status")
        item_copy = dict(item)
        item_copy["resolved_abs_path"] = str((base_dir / item["txt_path"]).resolve())

        if status != "completed":
            res["ignored"].append(item_copy)
            continue

        if sid not in known:
            res["new"].append(item_copy)
        elif known[sid].lower() != item["sha256"].lower():
            res["updated"].append(item_copy)
        else:
            res["unchanged"].append(item_copy)

    return res
