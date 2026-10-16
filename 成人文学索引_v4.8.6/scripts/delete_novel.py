#!/usr/bin/env python3
"""删除一本书的所有索引产物、风格卡、语义缓存与断点记录。

v4.6.3 修复：
- 书名/作者先经 norm_name 规范化，再使用 iterdir + startswith 字面量匹配，
  不再把书名拼进 glob（书名含 `[` 等元字符时会误删其他书）。
- 同步删除 `<索引库>/../风格卡/<小说名>-<作者>/`，与 references/建立索引.md 3.3 一致。
- 同步清理该书在 .semantic_index 中的 npz 与 manifest 条目。
"""
import argparse
import json
import shutil
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

SKILL_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SKILL_DIR / "scripts"))

from _common import book_key, prefixed_files
from _logging import add_log_format_argument, configure_logging, get_logger
from index_db import IndexDB


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="删除一本书的所有索引产物、风格卡与断点记录")
    parser.add_argument("--novel", required=True, help="小说名称")
    parser.add_argument("--author", required=True, help="作者名称")
    parser.add_argument("--lib", required=True, help="索引库根目录")
    parser.add_argument("--yes", action="store_true", help="跳过二次确认直接删除")
    add_log_format_argument(parser)
    return parser.parse_args()



def _collect_paths(lib: Path, key: str) -> list[Path]:
    paths: list[Path] = []

    raw_dir = lib / "小说原文" / key
    if raw_dir.exists():
        paths.append(raw_dir)

    idx_dir = lib / "章节索引"
    paths.extend(prefixed_files(idx_dir, key, "-idx.md"))

    nav_file = lib / "小说索引" / f"{key}.md"
    if nav_file.exists():
        paths.append(nav_file)

    style_dir = lib.parent / "风格卡" / key
    if style_dir.exists():
        paths.append(style_dir)

    semantic_dir = lib / ".semantic_index"
    paths.extend(prefixed_files(semantic_dir, key, ".npz"))

    return sorted(paths)


def _prune_semantic_manifest(lib: Path, key: str) -> int:
    """移除 manifest 中属于该书的条目，返回移除条数。"""
    manifest_path = lib / ".semantic_index" / "manifest.json"
    if not manifest_path.exists():
        return 0
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return 0
    segments = manifest.get("segments", [])
    kept = [
        s for s in segments
        if not (str(s.get("source_index", "")).startswith(key + "-")
                or str(s.get("npz", "")).startswith(key + "-"))
    ]
    removed = len(segments) - len(kept)
    if removed:
        manifest["segments"] = kept
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return removed


def _confirm(novel: str, author: str, paths: list[Path]) -> bool:
    print(f"即将删除《{novel}》- {author} 的所有产物：")
    for p in paths:
        print(f"  - {p}")
    answer = input("确认删除？(yes/no): ").strip().lower()
    return answer in ("yes", "y")


def main() -> int:
    args = _parse_args()
    configure_logging(fmt=args.log_format)
    logger = get_logger("delete_novel")

    try:
        key = book_key(args.novel, args.author)
    except ValueError as e:
        logger.error("%s", e)
        return 1

    lib = Path(args.lib).resolve()
    if not lib.exists():
        logger.error("索引库不存在: %s", lib)
        return 1

    paths = _collect_paths(lib, key)
    if not paths:
        logger.warning("未找到《%s》- %s 的任何产物", args.novel, args.author)
        return 0

    if not args.yes and not _confirm(args.novel, args.author, paths):
        logger.info("用户取消删除")
        return 0

    failed = 0
    for p in paths:
        try:
            if p.is_dir():
                shutil.rmtree(p)
            else:
                p.unlink()
            logger.info("已删除: %s", p)
        except OSError as e:
            failed += 1
            logger.error("删除失败 %s: %s", p, e)

    try:
        removed = _prune_semantic_manifest(lib, key)
        if removed:
            logger.info("已从语义索引清单移除 %d 条记录", removed)
    except OSError as e:
        logger.warning("清理语义索引清单失败: %s", e)

    try:
        with IndexDB(str(lib)) as db:
            db_rows = db.delete_by_prefix(key)
            logger.info("已清理 %d 条断点记录", db_rows)
    except Exception as e:
        failed += 1
        logger.error("清理断点库失败: %s", e)

    if failed:
        logger.error("《%s》- %s 删除完成但有 %d 个失败项", args.novel, args.author, failed)
        return 1
    logger.info("《%s》- %s 删除完成", args.novel, args.author)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
