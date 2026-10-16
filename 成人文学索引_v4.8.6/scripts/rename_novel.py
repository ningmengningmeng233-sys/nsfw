#!/usr/bin/env python3
"""重命名一本书的小说名/作者，并同步更新段文件、索引、导航、断点、风格卡与语义索引。

v4.6.3 重写（修复此前重命名后书库损坏的问题）：
- 段文件同步改名并更新 frontmatter 的 `对应索引`；
- 章节索引 frontmatter 的 `源文件` 指向新段文件；
- 小说导航不再手改正文，而是改名完成后用 build_novel_index.py --force 重新生成；
- 所有匹配使用字面量前缀（iterdir + startswith），不把书名拼进 glob；
- 断点记录经 IndexDB.rename_prefix 更新；
- 风格卡目录同步改名；该书旧语义索引失效并提示重建。
"""
import argparse
import json
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

SKILL_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SKILL_DIR / "scripts"))

from _common import book_key, norm_name, prefixed_files
from _logging import add_log_format_argument, configure_logging, get_logger
from build_novel_index import build_novel_index
from index_db import IndexDB


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="重命名一本书的小说名/作者，并同步更新所有文件与断点记录")
    parser.add_argument("--old-novel", required=True, help="原小说名称")
    parser.add_argument("--old-author", required=True, help="原作者名称")
    parser.add_argument("--new-novel", required=True, help="新小说名称")
    parser.add_argument("--new-author", required=True, help="新作者名称")
    parser.add_argument("--lib", required=True, help="索引库根目录")
    parser.add_argument("--yes", action="store_true", help="跳过二次确认直接重命名")
    add_log_format_argument(parser)
    return parser.parse_args()



def _update_frontmatter(text: str, *, novel: str, author: str, old_key: str, new_key: str,
                        update_source: bool = False, update_link: bool = False) -> str:
    """更新 frontmatter 字段，正文原样保留。"""
    if not text.startswith("---"):
        return text
    lines = text.splitlines()
    end = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), None)
    if end is None:
        return text

    new_lines = []
    for line in lines[1:end]:
        stripped = line.strip()
        if stripped.startswith("小说名称:"):
            new_lines.append(f"小说名称: 《{novel}》")
        elif stripped.startswith("作者:"):
            new_lines.append(f"作者: {author}")
        elif update_source and stripped.startswith("源文件:"):
            _, value = stripped.split(":", 1)
            value = value.strip().strip("'\"")
            new_lines.append(f"源文件: {value.replace(old_key, new_key)}")
        elif update_link and stripped.startswith("对应索引:"):
            _, value = stripped.split(":", 1)
            value = value.strip().strip("'\"")
            new_lines.append(f"对应索引: {value.replace(old_key, new_key)}")
        else:
            new_lines.append(line)

    body = "\n".join(lines[end + 1:])
    fm = "---\n" + "\n".join(new_lines) + "\n---"
    return fm + ("\n" + body if body else "")


def _prune_semantic(lib: Path, old_key: str, deleted_files: list[Path]) -> int:
    """删除该书旧语义缓存文件，并从 manifest 中移除对应条目。"""
    manifest_path = lib / ".semantic_index" / "manifest.json"
    removed = 0
    if manifest_path.exists():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            segments = manifest.get("segments", [])
            kept = [
                s for s in segments
                if not (str(s.get("source_index", "")).startswith(old_key + "-")
                        or str(s.get("npz", "")).startswith(old_key + "-"))
            ]
            removed = len(segments) - len(kept)
            if removed:
                manifest["segments"] = kept
                manifest_path.write_text(
                    json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
                )
        except (OSError, json.JSONDecodeError) as e:
            print(f"⚠️ 更新语义索引清单失败: {e}")
    for p in deleted_files:
        try:
            if p.exists():
                p.unlink()
        except OSError as e:
            print(f"⚠️ 删除旧语义缓存失败 {p}: {e}")
    return removed


def _confirm(old_key: str, new_key: str, moves: list[tuple[Path, Path]]) -> bool:
    print(f"即将把 {old_key} 重命名为 {new_key}：")
    for src, dst in moves:
        print(f"  {src} -> {dst}")
    print("  小说导航将重新生成；旧语义索引将失效，需要 semantic build --force 重建")
    answer = input("确认重命名？(yes/no): ").strip().lower()
    return answer in ("yes", "y")


def main() -> int:
    args = _parse_args()
    configure_logging(fmt=args.log_format)
    logger = get_logger("rename_novel")

    try:
        old_key = book_key(args.old_novel, args.old_author)
        new_key = book_key(args.new_novel, args.new_author)
    except ValueError as e:
        logger.error("%s", e)
        return 1

    new_novel = norm_name(args.new_novel)
    new_author = norm_name(args.new_author)

    lib = Path(args.lib).resolve()
    if not lib.exists():
        logger.error("索引库不存在: %s", lib)
        return 1
    if old_key == new_key:
        logger.error("新旧书名/作者完全相同")
        return 1

    # 收集将发生的变更（只读）
    moves: list[tuple[Path, Path]] = []
    raw_dir = lib / "小说原文" / old_key
    if raw_dir.is_dir():
        for p in prefixed_files(raw_dir, old_key, ".md"):
            if p.name.endswith("-idx.md"):
                continue
            moves.append((p, raw_dir / (new_key + p.name[len(old_key):])))
        moves.append((raw_dir, lib / "小说原文" / new_key))

    idx_dir = lib / "章节索引"
    for p in prefixed_files(idx_dir, old_key, "-idx.md"):
        moves.append((p, idx_dir / (new_key + p.name[len(old_key):])))

    old_nav = lib / "小说索引" / f"{old_key}.md"
    has_nav = old_nav.exists()

    style_old = lib.parent / "风格卡" / old_key
    style_new = lib.parent / "风格卡" / new_key
    has_style = style_old.exists()
    style_conflict = has_style and style_new.exists()

    semantic_old = prefixed_files(lib / ".semantic_index", old_key, ".npz")

    if not moves and not has_nav and not has_style and not semantic_old:
        logger.warning("未找到 %s 的相关文件", old_key)
        return 0

    if not args.yes and not _confirm(old_key, new_key, moves):
        logger.info("用户取消重命名")
        return 0

    failed = 0

    # 1) 段文件：改名 + 更新 frontmatter
    for src, dst in [m for m in moves if m[0].parent == raw_dir]:
        try:
            text = src.read_text(encoding="utf-8")
            text = _update_frontmatter(
                text, novel=new_novel, author=new_author,
                old_key=old_key, new_key=new_key, update_link=True,
            )
            dst.write_text(text, encoding="utf-8")
            if src != dst:
                src.unlink()
            logger.info("已更新段文件: %s -> %s", src, dst)
        except OSError as e:
            failed += 1
            logger.error("段文件处理失败 %s: %s", src, e)

    # 2) 段目录改名（文件已改名后执行）
    try:
        if raw_dir.exists():
            raw_dir.rename(lib / "小说原文" / new_key)
            logger.info("已重命名目录: %s -> %s", raw_dir, lib / "小说原文" / new_key)
    except OSError as e:
        failed += 1
        logger.error("段目录重命名失败 %s: %s", raw_dir, e)

    # 3) 章节索引：改名 + 更新源文件回指
    for src, dst in [m for m in moves if m[0].parent == idx_dir]:
        try:
            text = src.read_text(encoding="utf-8")
            text = _update_frontmatter(
                text, novel=new_novel, author=new_author,
                old_key=old_key, new_key=new_key, update_source=True,
            )
            dst.write_text(text, encoding="utf-8")
            if src != dst:
                src.unlink()
            logger.info("已更新章节索引: %s -> %s", src, dst)
        except OSError as e:
            failed += 1
            logger.error("章节索引处理失败 %s: %s", src, e)

    # 4) 旧导航删除（稍后重新生成新导航）
    if has_nav:
        try:
            old_nav.unlink()
            logger.info("已删除旧导航: %s", old_nav)
        except OSError as e:
            failed += 1
            logger.error("旧导航删除失败 %s: %s", old_nav, e)

    # 5) 风格卡目录改名
    if has_style:
        if style_conflict:
            logger.warning("目标风格卡目录已存在，保留旧目录不动，请手工合并: %s", style_old)
        else:
            try:
                style_new.parent.mkdir(parents=True, exist_ok=True)
                style_old.rename(style_new)
                logger.info("已重命名风格卡目录: %s -> %s", style_old, style_new)
            except OSError as e:
                failed += 1
                logger.error("风格卡目录重命名失败 %s: %s", style_old, e)

    # 6) 断点库路径更新
    try:
        with IndexDB(str(lib)) as db:
            updated = db.rename_prefix(old_key, new_key)
            logger.info("已更新 %d 条断点记录路径", updated)
    except Exception as e:
        failed += 1
        logger.error("更新断点库失败: %s", e)

    # 7) 旧语义缓存清理（改名后必须重建语义索引）
    if semantic_old:
        removed = _prune_semantic(lib, old_key, semantic_old)
        logger.warning("已移除 %d 条旧语义索引条目，请运行 semantic build --force 重建", removed)

    # 8) 重新生成小说导航（frontmatter 与正文一起更新）
    if not failed:
        rc = build_novel_index(new_novel, new_author, str(lib), force=True)
        if rc != 0:
            failed += 1
            logger.error("新导航生成失败")
    elif has_nav:
        logger.error("由于前面步骤失败，未生成新导航；请修复后运行 build_novel_index.py --novel %s --lib %s --force", new_key, lib)

    if failed:
        logger.error("%s -> %s 重命名完成但有 %d 个失败项", old_key, new_key, failed)
        return 1
    logger.info("%s 重命名为 %s 完成", old_key, new_key)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
