#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""语义索引构建脚本 v4.6（ADR-0004）

基于章节索引文件生成向量索引，为自然语言语义召回提供本地 embedding 存储。

- 输入：<索引库>/章节索引/*-idx.md
- 输出：<索引库>/.semantic_index/
    - <索引文件名>.npz（embeddings + chunks JSON + source_hash + model + created_at）
    - manifest.json（版本、模型、构建时间、段清单）
- 默认关闭；需 INDEX_ENABLE_SEMANTIC=1 或 --build 触发
- 依赖 sentence-transformers + numpy（可选依赖，未安装给出友好提示）

用法：
  INDEX_ENABLE_SEMANTIC=1 python semantic_index.py [--lib <索引库>] [--force] [--batch-size 32]
  python semantic_index.py --build --lib <索引库> [--force]
"""
import argparse
import hashlib
import json
import os
import re
import sys
import time
from typing import Any

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

SKILL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(SKILL_DIR, "scripts"))

try:
    from dotenv import load_dotenv

    load_dotenv(os.path.join(SKILL_DIR, ".env"))
except Exception:
    pass

from _common import parse_frontmatter, read_text_auto, resolve_under
from _logging import add_log_format_argument, configure_logging, get_logger

DEFAULT_MODEL = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
INDEX_DIR_NAME = ".semantic_index"
MANIFEST_NAME = "manifest.json"
INDEX_VERSION = "1.0"

# 章节小节标题：## 第X章 ...
_CHAPTER_HEADING_RE = re.compile(r"^##\s+第\s*[\d零一二三四五六七八九十百千]+\s*章.*$")
_CHAPTER_NUM_RE = re.compile(r"第\s*([\d零一二三四五六七八九十百千]+)\s*章")
# 段文件内行号范围，如（行号：1 ~ 85）或 (行号:1~85)
_LINE_RANGE_RE = re.compile(r"[（(]行号[：:]\s*(\d+)\s*[~～]\s*(\d+)\s*[）)]")

INDEX_LIB_DIR = (
    os.environ.get("INDEX_LIB_DIR", "").strip()
    or os.path.join(SKILL_DIR, "索引库")
)

logger = get_logger(__name__)


class MissingDependencyError(RuntimeError):
    """可选依赖缺失时的友好错误。"""


def _friendly_missing_message() -> str:
    return (
        "未安装语义检索可选依赖。请执行：\n"
        '  pip install "novel-index[semantic]"\n'
        "或手动安装：\n"
        "  pip install numpy>=1.24 sentence-transformers>=2.5\n"
        "首次使用会下载默认模型（约 400MB+），请确保网络与磁盘空间充足。"
    )


def _sha256_file(path: str) -> str:
    """计算文件 SHA-256（流式读取，适合索引文件大小）。"""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def _npz_str(value: Any) -> str:
    """从 npz 标量或 0-d 数组中提取字符串。"""
    if hasattr(value, "item"):
        value = value.item()
    return str(value)


def load_embedding_model(model_name: str):
    """延迟加载 sentence-transformers；缺失时抛出 MissingDependencyError。"""
    try:
        import numpy  # noqa: F401
    except ImportError as exc:
        raise MissingDependencyError(_friendly_missing_message()) from exc
    try:
        from sentence_transformers import SentenceTransformer
    except ImportError as exc:
        raise MissingDependencyError(_friendly_missing_message()) from exc

    logger.info("🔄 加载 embedding 模型: %s ...", model_name)
    try:
        model = SentenceTransformer(model_name)
    except Exception as exc:
        raise RuntimeError(f"加载模型 {model_name} 失败：{exc}") from exc
    dim = model.get_sentence_embedding_dimension()
    logger.info("✅ 模型加载完成，输出维度: %d", dim)
    return model


def _extract_summary(text: str, max_len: int = 300) -> str:
    """从章节块中提取摘要：优先取「剧情概要」小节，否则取正文前 max_len 字。"""
    lines = text.splitlines()
    in_summary = False
    parts: list[str] = []
    for line in lines:
        if line.strip() == "### 剧情概要":
            in_summary = True
            continue
        if in_summary:
            stripped = line.strip()
            if not stripped:
                continue
            if line.startswith("### ") or line.startswith("## "):
                break
            parts.append(stripped)
    summary = " ".join(parts)
    if not summary.strip():
        summary = text.replace("\n", " ").strip()
    if len(summary) > max_len:
        summary = summary[:max_len].rstrip() + "…"
    return summary


def parse_chapter_blocks(index_path: str, lib_dir: str) -> list[dict[str, Any]]:
    """解析单个章节索引文件，返回章节块列表（每个块含元数据与文本）。"""
    text = read_text_auto(index_path)
    meta, body = parse_frontmatter(text)
    if not meta.get("小说名称") or not meta.get("作者"):
        logger.warning(
            "  ⚠️  %s: frontmatter 缺少小说名称/作者，跳过",
            os.path.basename(index_path),
        )
        return []

    novel = meta["小说名称"].strip("《》")
    author = meta["作者"].strip()
    segment_path = str(meta.get("源文件", "")).replace("\\", "/").strip()
    if segment_path:
        resolved = resolve_under(lib_dir, segment_path)
        if resolved is None:
            logger.warning(
                "  ⚠️  %s: 源文件路径越出索引库: %s",
                os.path.basename(index_path),
                segment_path,
            )

    # 原始文件行号 = body 行号 + frontmatter 行数
    all_lines = text.splitlines()
    body_lines = body.splitlines()
    base_line = len(all_lines) - len(body_lines)

    chunks: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None

    for i, line in enumerate(body_lines, 1):
        if not _CHAPTER_HEADING_RE.match(line):
            continue

        if current is not None:
            current["end_line"] = i - 1 + base_line
            start_body = current["start_line"] - base_line - 1
            end_body = current["end_line"] - base_line
            current["text"] = "\n".join(body_lines[start_body:end_body])
            current["summary"] = _extract_summary(current["text"])
            chunks.append(current)

        title = line.lstrip("#").strip()
        chap_match = _CHAPTER_NUM_RE.search(title)
        chapter_range = f"第{chap_match.group(1)}章" if chap_match else title
        line_match = _LINE_RANGE_RE.search(title)
        seg_start = int(line_match.group(1)) if line_match else None
        seg_end = int(line_match.group(2)) if line_match else None

        try:
            index_rel = os.path.relpath(index_path, lib_dir).replace("\\", "/")
        except ValueError:
            index_rel = os.path.basename(index_path)
        current = {
            "novel": novel,
            "author": author,
            "segment_path": segment_path,
            "index_path": index_rel,
            "chapter_range": chapter_range,
            "chapter_title": title,
            "start_line": i + base_line,
            "end_line": len(all_lines),  # 临时值，后续修正
            "segment_line_start": seg_start,
            "segment_line_end": seg_end,
        }

    if current is not None:
        current["end_line"] = len(all_lines)
        start_body = current["start_line"] - base_line - 1
        current["text"] = "\n".join(body_lines[start_body:])
        current["summary"] = _extract_summary(current["text"])
        chunks.append(current)

    return chunks


def _npz_path_for(index_path: str, semantic_dir: str) -> str:
    base = os.path.basename(index_path).replace(".md", "")
    return os.path.join(semantic_dir, base + ".npz")


def _is_up_to_date(npz_path: str, source_hash: str, model_name: str) -> bool:
    """检查已有 npz 是否与源文件 hash、模型一致。"""
    try:
        import numpy as np

        data = np.load(npz_path, allow_pickle=False)
        stored_hash = _npz_str(data["source_hash"])
        stored_model = _npz_str(data["model"])
        return stored_hash == source_hash and stored_model == model_name
    except Exception:
        return False


def _load_manifest(semantic_dir: str) -> dict[str, Any]:
    path = os.path.join(semantic_dir, MANIFEST_NAME)
    if not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def build_semantic_index(
    lib_dir: str,
    model,
    model_name: str,
    force: bool = False,
    batch_size: int = 32,
) -> int:
    """构建语义索引，返回总章节块数。"""
    import numpy as np

    index_dir = os.path.join(lib_dir, "章节索引")
    semantic_dir = os.path.join(lib_dir, INDEX_DIR_NAME)
    os.makedirs(semantic_dir, exist_ok=True)

    if not os.path.isdir(index_dir):
        raise FileNotFoundError(f"章节索引目录不存在: {index_dir}")

    index_files = sorted(f for f in os.listdir(index_dir) if f.endswith("-idx.md"))
    if not index_files:
        logger.warning("⚠️  %s 下未找到 -idx.md 文件", index_dir)
        return 0

    logger.info("📚 发现 %d 个章节索引文件", len(index_files))

    old_manifest = _load_manifest(semantic_dir)
    old_segments = {
        s["source_index"]: s
        for s in old_manifest.get("segments", [])
        if "source_index" in s
    }
    new_segments: list[dict[str, Any]] = []

    total_updated = 0
    for idx_file in index_files:
        index_path = os.path.join(index_dir, idx_file)
        npz_path = _npz_path_for(index_path, semantic_dir)
        source_hash = _sha256_file(index_path)
        try:
            source_index = os.path.relpath(index_path, lib_dir).replace("\\", "/")
        except ValueError:
            source_index = os.path.basename(index_path)

        if not force and _is_up_to_date(npz_path, source_hash, model_name):
            logger.info("  ⏭️  %s: 语义索引已是最新（跳过）", idx_file)
            entry = old_segments.get(source_index)
            if entry:
                new_segments.append(entry)
            else:
                new_segments.append(
                    {
                        "npz": os.path.basename(npz_path),
                        "source_index": source_index,
                        "source_hash": source_hash,
                        "chunks": 0,
                    }
                )
            continue

        chunks = parse_chapter_blocks(index_path, lib_dir)
        if not chunks:
            logger.warning("  ⚠️  %s: 未解析到章节块", idx_file)
            continue

        texts = [c["text"] for c in chunks]
        logger.info(
            "  🧮 %s: 编码 %d 个章节块（模型: %s, 批次: %d）...",
            idx_file,
            len(texts),
            model_name,
            batch_size,
        )
        embeddings = model.encode(
            texts,
            batch_size=batch_size,
            show_progress_bar=False,
            normalize_embeddings=True,
        )

        build_time = time.strftime("%Y-%m-%dT%H:%M:%S")
        np.savez_compressed(
            npz_path,
            embeddings=embeddings,
            chunks=json.dumps(chunks, ensure_ascii=False, default=str),
            source_hash=source_hash,
            model=model_name,
            created_at=build_time,
        )
        logger.info(
            "  ✅ 已写入 %s（%d 块）",
            os.path.basename(npz_path),
            len(chunks),
        )
        new_segments.append(
            {
                "npz": os.path.basename(npz_path),
                "source_index": source_index,
                "source_hash": source_hash,
                "chunks": len(chunks),
            }
        )
        total_updated += len(chunks)

    if total_updated == 0:
        logger.info("ℹ️  没有需要更新的语义索引块")
    else:
        logger.info("🔄 本次更新 %d 个章节块", total_updated)

    manifest = {
        "version": INDEX_VERSION,
        "model": model_name,
        "build_time": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "segment_count": len(new_segments),
        "chunk_count": sum(s.get("chunks", 0) for s in new_segments),
        "segments": new_segments,
    }
    manifest_path = os.path.join(semantic_dir, MANIFEST_NAME)
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)
    logger.info("📝 已写入清单: %s", manifest_path)

    return manifest["chunk_count"]


def main() -> int:
    parser = argparse.ArgumentParser(
        description="构建小说语义索引（可选模块，ADR-0004）"
    )
    parser.add_argument(
        "--lib",
        default=INDEX_LIB_DIR,
        help="索引库根目录（默认 INDEX_LIB_DIR/.env 或技能目录/索引库）",
    )
    parser.add_argument(
        "--build",
        action="store_true",
        help="强制执行构建（否则需环境变量 INDEX_ENABLE_SEMANTIC=1）",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="强制重建全部语义索引",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=32,
        help="embedding 编码批次大小（默认 32）",
    )
    parser.add_argument(
        "--model",
        default=None,
        help="覆盖默认/环境变量中的 embedding 模型",
    )
    add_log_format_argument(parser)
    args = parser.parse_args()
    configure_logging(fmt=args.log_format)

    enabled_env = os.environ.get("INDEX_ENABLE_SEMANTIC", "").strip()
    if enabled_env != "1" and not args.build:
        logger.info(
            "ℹ️  语义索引默认关闭。设置 INDEX_ENABLE_SEMANTIC=1 或传入 --build 以构建。"
        )
        return 0

    model_name = args.model or os.environ.get("INDEX_EMBEDDING_MODEL") or DEFAULT_MODEL

    try:
        model = load_embedding_model(model_name)
    except MissingDependencyError as exc:
        logger.error("❌ %s", exc)
        return 1

    try:
        chunk_count = build_semantic_index(
            args.lib,
            model,
            model_name,
            force=args.force,
            batch_size=args.batch_size,
        )
    except Exception as exc:
        logger.exception("❌ 语义索引构建失败: %s", exc)
        return 1

    logger.info("✅ 语义索引构建完成，共 %d 个章节块", chunk_count)
    return 0


if __name__ == "__main__":
    sys.exit(main())
