#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""语义检索脚本 v4.6（ADR-0004）

加载本地语义索引，对自然语言查询做向量相似度召回。

用法：
  python semantic_search.py "查询语句" --lib <索引库> [--top-k 5] [--threshold 0.5]
  python semantic_search.py "查询语句" --lib <索引库> --json
"""
import argparse
import json
import os
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

from _logging import add_log_format_argument, configure_logging, get_logger

DEFAULT_MODEL = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
INDEX_DIR_NAME = ".semantic_index"
MANIFEST_NAME = "manifest.json"

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
    logger.info("✅ 模型加载完成")
    return model


def load_semantic_index(
    semantic_dir: str,
) -> tuple[dict[str, Any], Any, list[dict[str, Any]]]:
    """加载语义索引，返回 (manifest, embeddings, chunks)。"""
    import numpy as np

    manifest_path = os.path.join(semantic_dir, MANIFEST_NAME)
    if not os.path.exists(manifest_path):
        raise FileNotFoundError(
            f"未找到语义索引清单: {manifest_path}，请先运行 semantic_index.py --build"
        )

    with open(manifest_path, encoding="utf-8") as f:
        manifest = json.load(f)

    all_embeddings: list[Any] = []
    all_chunks: list[dict[str, Any]] = []

    for entry in manifest.get("segments", []):
        npz_path = os.path.join(semantic_dir, entry["npz"])
        if not os.path.exists(npz_path):
            logger.warning("⚠️  缺失 npz 文件: %s，跳过", npz_path)
            continue
        try:
            data = np.load(npz_path, allow_pickle=False)
            embeddings = data["embeddings"]
            chunks = json.loads(_npz_str(data["chunks"]))
        except Exception as exc:
            logger.warning(
                "⚠️  加载 %s 失败: %s，跳过",
                entry.get("npz", "<unknown>"),
                exc,
            )
            continue
        all_embeddings.append(embeddings)
        all_chunks.extend(chunks)

    if not all_embeddings:
        raise ValueError("语义索引为空或所有 npz 加载失败")

    embeddings = np.vstack(all_embeddings)
    return manifest, embeddings, all_chunks


def search(
    query: str,
    model,
    embeddings: Any,
    chunks: list[dict[str, Any]],
    top_k: int,
    threshold: float,
) -> list[dict[str, Any]]:
    """向量检索，返回 top-k 命中（含 score）。"""
    import numpy as np

    query_vec = model.encode(
        [query],
        show_progress_bar=False,
        normalize_embeddings=True,
    )
    scores = np.dot(embeddings, query_vec[0])
    idxs = np.argsort(scores)[::-1]

    results: list[dict[str, Any]] = []
    for idx in idxs[:top_k]:
        score = float(scores[idx])
        if score < threshold:
            break
        chunk = chunks[idx]
        results.append({**chunk, "score": score})
    return results


def _format_result(index: int, r: dict[str, Any]) -> str:
    lines = [
        f"{index}. 相似度 {r['score']:.3f} | 《{r['novel']}》（{r['author']}）{r['chapter_range']}",
        f"   章节标题：{r.get('chapter_title', '')}",
        f"   段文件：{r['segment_path']}",
    ]
    if r.get("segment_line_start") is not None and r.get("segment_line_end") is not None:
        lines.append(
            f"   段文件内行号：{r['segment_line_start']} ~ {r['segment_line_end']}"
        )
    lines.append(
        f"   索引文件：{r['index_path']}（索引行 {r['start_line']} ~ {r['end_line']}）"
    )
    lines.append(f"   摘要：{r['summary']}")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="小说语义检索（可选模块，ADR-0004）"
    )
    parser.add_argument("query", help="自然语言查询")
    parser.add_argument(
        "--lib",
        default=INDEX_LIB_DIR,
        help="索引库根目录（默认 INDEX_LIB_DIR/.env 或技能目录/索引库）",
    )
    parser.add_argument(
        "--top-k",
        type=int,
        default=5,
        help="返回结果数（默认 5）",
    )
    parser.add_argument(
        "--threshold",
        type=float,
        default=0.5,
        help="相似度阈值（默认 0.5）",
    )
    parser.add_argument(
        "--model",
        default=None,
        help="覆盖默认/环境变量中的 embedding 模型",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="以 JSON 格式输出结果",
    )
    add_log_format_argument(parser)
    args = parser.parse_args()
    configure_logging(fmt=args.log_format)

    semantic_dir = os.path.join(args.lib, INDEX_DIR_NAME)
    model_name = args.model or os.environ.get("INDEX_EMBEDDING_MODEL") or DEFAULT_MODEL

    try:
        model = load_embedding_model(model_name)
    except MissingDependencyError as exc:
        logger.error("❌ %s", exc)
        return 1

    try:
        manifest, embeddings, chunks = load_semantic_index(semantic_dir)
    except (FileNotFoundError, ValueError) as exc:
        logger.error("❌ %s", exc)
        return 1

    indexed_model = manifest.get("model")
    if indexed_model and indexed_model != model_name:
        logger.warning(
            "⚠️  当前模型 %s 与索引构建模型 %s 不一致，建议统一后再检索",
            model_name,
            indexed_model,
        )

    t0 = time.time()
    results = search(
        args.query,
        model,
        embeddings,
        chunks,
        top_k=args.top_k,
        threshold=args.threshold,
    )
    elapsed = time.time() - t0

    if args.json:
        payload = {
            "query": args.query,
            "model": model_name,
            "indexed_model": indexed_model,
            "top_k": args.top_k,
            "threshold": args.threshold,
            "result_count": len(results),
            "elapsed_ms": round(elapsed * 1000, 2),
            "results": results,
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(
            f"「{args.query}」语义召回命中结果（模型：{model_name}，"
            f"top-k={args.top_k}，阈值={args.threshold}，耗时 {elapsed * 1000:.0f}ms）：\n"
        )
        if not results:
            print("未找到命中项。可尝试降低 --threshold 或检查索引是否已构建。")
        else:
            for i, r in enumerate(results, 1):
                print(_format_result(i, r))
                print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
