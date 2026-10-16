#!/usr/bin/env python3
"""成人文学索引编排器：集中式入口，封装 SKILL.md 状态机。

子命令：
  init     初始化索引库目录与 .env 配置模板
  build    从源文件完整建库（拆段 → 生成索引 → 验证 → 导航）
  verify   对已生成的索引执行硬门禁验证
  resume   续跑失败的段（基于 SQLite 断点库）
  delete   删除一本书的所有产物
  status   输出索引库全局状态面板
  style    文风蒸馏（轻量风格卡或外部完整蒸馏）
  semantic 语义索引构建/检索（可选模块，ADR-0004）
"""
import argparse
import datetime
import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

SKILL_DIR = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = SKILL_DIR / "scripts"
VENDOR_DIR = SCRIPTS_DIR / "vendor"

sys.path.insert(0, str(SCRIPTS_DIR))


def load_source_manifest():
    """清单校验器从包内 `scripts/vendor/` 取，不再从作者柜根偷偷导入。

    原来这里写的是 `SKILL_DIR.parent / "schemas"`：在开发机上恰好能命中柜根共享内核，
    在收件机上必然落空，然后把「协议缺件」报成「加载清单校验器异常（拒收）」，
    用户会以为自己的清单坏了。工单 K 项要求独立包不得依赖柜子父目录，所以：
    校验器随包 vendor 一份（与主干逐字节同摘要），解析只认包内路径；
    缺件时给可行动的错（说清缺哪个文件），不放行也不误指用户清单。
    """
    import importlib

    if str(VENDOR_DIR) not in sys.path:
        sys.path.insert(0, str(VENDOR_DIR))
    try:
        return importlib.import_module("source_manifest")
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            f"包内缺清单校验器 scripts/vendor/source_manifest.py（不是你的清单坏了）：{exc}") from exc

try:
    from dotenv import load_dotenv
    load_dotenv(SKILL_DIR / ".env")
except Exception:
    pass

from _logging import add_log_format_argument, configure_logging, get_logger  # noqa: E402
from _common import parse_frontmatter, read_text_auto  # noqa: E402
import style_distillation  # noqa: E402

logger = get_logger("orchestrate")

DEFAULT_LIB = os.environ.get("INDEX_LIB_DIR", "").strip() or str(SKILL_DIR / "索引库")

DIRS = ("小说原文", "小说索引", "章节索引")


def _run(cmd: list[str], **kwargs) -> int:
    """Run a script and log the command. Returns exit code."""
    logger.info("执行: %s", " ".join(cmd))
    result = subprocess.run(cmd, check=False, **kwargs)
    if result.returncode != 0:
        logger.error("命令退出码 %d: %s", result.returncode, " ".join(cmd))
    return result.returncode


def _python(script: str) -> str:
    return str(SCRIPTS_DIR / script)


def _now() -> str:
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# ---------------------------------------------------------------------------
# Subcommands
# ---------------------------------------------------------------------------

def cmd_init(args: argparse.Namespace) -> int:
    """Initialize index library directories and .env template."""
    lib = Path(args.lib).resolve()
    lib.mkdir(parents=True, exist_ok=True)
    for d in DIRS:
        (lib / d).mkdir(exist_ok=True)

    env_file = SKILL_DIR / ".env"
    example = SKILL_DIR / ".env.example"
    if not env_file.exists() and example.exists():
        shutil.copy(example, env_file)
        logger.info("已复制 .env.example 到 .env，请编辑填入 API 配置")
    else:
        logger.info(".env 已存在，保留")

    logger.info("索引库初始化完成: %s", lib)
    return 0


def cmd_build(args: argparse.Namespace) -> int:
    """Full build: split -> index -> verify -> novel index."""
    lib = args.lib
    source = args.source
    novel = args.novel
    author = args.author
    volume = args.volume or ""
    chapters_per_seg = args.chapters_per_seg
    chars_per_seg = args.chars_per_seg
    force = args.force

    if not source or not novel or not author:
        logger.error("build 子命令需要 --source/--novel/--author")
        return 2

    # PREFLIGHT: ensure library exists
    for d in DIRS:
        os.makedirs(os.path.join(lib, d), exist_ok=True)

    # EXECUTE: split segments
    split_cmd = [
        sys.executable, _python("split_segments.py"),
        source, "--novel", novel, "--author", author, "--lib", lib,
        "--chapters-per-seg", str(chapters_per_seg),
        "--chars-per-seg", str(chars_per_seg),
    ]
    if volume:
        split_cmd.extend(["--volume", volume])
    if force:
        split_cmd.append("--force")
    if args.log_format:
        split_cmd.extend(["--log-format", args.log_format])

    rc = _run(split_cmd)
    if rc != 0:
        logger.error("拆段失败，停止后续流程")
        return rc

    # EXECUTE: build segment indices
    novel_author = f"{novel}-{author}"
    seg_dir = os.path.join(lib, "小说原文", novel_author)
    if not os.path.isdir(seg_dir):
        logger.error("拆段后未找到段目录: %s", seg_dir)
        return 1

    index_cmd = [
        sys.executable, _python("build_segment_index.py"),
        seg_dir, "--lib", lib,
        "--workers", str(args.workers),
    ]
    if force:
        index_cmd.append("--force")
    if args.skip_existing:
        index_cmd.append("--skip-existing")
    if args.log_format:
        index_cmd.extend(["--log-format", args.log_format])

    rc = _run(index_cmd)
    if rc != 0:
        logger.warning("部分段索引生成失败，将在 VERIFY 前尝试 resume")
        cmd_resume(args)

    # VERIFY
    rc = cmd_verify(args)
    if rc != 0:
        logger.error("验证未通过，状态为 NEEDS_REVIEW")
        return rc

    # PUBLISH: novel navigation index
    nav_cmd = [
        sys.executable, _python("build_novel_index.py"),
        "--novel", novel_author, "--lib", lib, "--force",
    ]
    if args.log_format:
        nav_cmd.extend(["--log-format", args.log_format])

    return _run(nav_cmd)


def cmd_import_manifest(args: argparse.Namespace) -> int:
    """按 source-manifest.json 规范批量导入/追更小说原文并登记断点状态。"""
    manifest_path = Path(args.manifest).resolve()
    if not manifest_path.is_file():
        logger.error("清单文件不存在（拒收）：%s", manifest_path)
        return 2

    # 清单校验器：包内 vendor 优先，缺件给可行动错误
    try:
        sm = load_source_manifest()
        ok, errors = sm.validate_manifest_file(manifest_path)
    except RuntimeError as exc:
        logger.error("拒收：%s", exc)
        return 3
    except Exception as exc:
        logger.error("清单校验器执行异常（拒收）：%s", type(exc).__name__)
        return 1

    if not ok:
        logger.error("清单校验失败（拒收）：%s", "；".join(errors))
        return 1

    lib = args.lib
    for d in DIRS:
        os.makedirs(os.path.join(lib, d), exist_ok=True)

    from index_db import IndexDB

    with IndexDB(lib) as db:
        known_sources = {sid: d["sha256"] for sid, d in db.get_ingested_sources().items()}

    classified = sm.classify_for_ingest(manifest_path, known_sources)
    new_items = classified["new"]
    updated_items = classified["updated"]
    unchanged_items = classified["unchanged"]
    ignored_items = classified["ignored"]

    total_pending = len(new_items) + len(updated_items)
    if total_pending == 0:
        logger.info("清单无需更新（无变化 %d 项，未完成跳过 %d 项）", len(unchanged_items), len(ignored_items))
        print(f"📋 清单无变化：共 {len(unchanged_items)} 个已完成项无需更新")
        return 0

    logger.info("开始处理清单：新条目 %d 项，重转更新 %d 项，无变化 %d 项",
                len(new_items), len(updated_items), len(unchanged_items))

    for item in (new_items + updated_items):
        source_id = item["source_id"]
        txt_path = item["resolved_abs_path"]
        is_update = item in updated_items

        novel = args.novel or item.get("title") or Path(txt_path).stem
        if novel.endswith("_原始记录"):
            novel = novel[:-5]
        author = args.author or item.get("author") or "未知作者"

        logger.info("正在导入 %s（%s - %s）[状态: %s]...",
                    source_id, novel, author, "重转更新" if is_update else "新条目")

        if getattr(args, "split_only", False):
            split_cmd = [
                sys.executable, _python("split_segments.py"),
                txt_path, "--novel", novel, "--author", author, "--lib", lib,
                "--chapters-per-seg", str(args.chapters_per_seg),
                "--chars-per-seg", str(args.chars_per_seg),
            ]
            if args.volume:
                split_cmd.extend(["--volume", args.volume])
            if is_update or args.force:
                split_cmd.append("--force")
            if args.log_format:
                split_cmd.extend(["--log-format", args.log_format])
            rc = _run(split_cmd)
            if rc != 0:
                logger.error("拆段失败，退出码: %d", rc)
                return rc
        else:
            build_args = argparse.Namespace(
                lib=lib,
                source=txt_path,
                novel=novel,
                author=author,
                volume=args.volume or "",
                chapters_per_seg=args.chapters_per_seg,
                chars_per_seg=args.chars_per_seg,
                workers=args.workers,
                force=is_update or args.force,
                skip_existing=not (is_update or args.force),
                log_format=args.log_format,
            )
            rc = cmd_build(build_args)
            if rc != 0:
                logger.error("条目 %s 建库失败，退出码: %d", source_id, rc)
                return rc

        with IndexDB(lib) as db:
            db.record_ingested_source(
                source_id=source_id,
                platform=item.get("platform", "unknown"),
                source_url=item.get("source_url", ""),
                txt_path=item["txt_path"],
                sha256=item["sha256"],
                status=item.get("status", "completed"),
                novel=novel,
                author=author,
            )
        logger.info("条目 %s 导入并登记完成", source_id)

    print(f"✅ 清单摄取完成：处理 {total_pending} 项（新 {len(new_items)}，更新 {len(updated_items)}），无变化 {len(unchanged_items)} 项")
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    """Run all verification gates."""
    lib = args.lib
    overall = 0

    logger.info("=== VERIFY: 结构完整性校验 ===")
    overall |= _run([sys.executable, _python("_验证批次.py"), "--lib", lib])

    logger.info("=== VERIFY: 行号校准与一致性检查 ===")
    idx_dir = os.path.join(lib, "章节索引")
    if os.path.isdir(idx_dir):
        overall |= _run([sys.executable, _python("verify_and_fix_lines.py"), idx_dir, "--lib", lib])

    logger.info("=== VERIFY: 行号引用体检 ===")
    overall |= _run([sys.executable, _python("check_line_refs.py"), "--lib", lib, "--detail"])

    if overall == 0:
        # v4.6.5：门禁返回码只反映子命令是否崩溃；⚠️ 待核/缺失警告均为 check-only，
        # 返回码仍为 0。收口时编排器必须按上方实际警告判定：存在待核/缺失 →
        # 标 NEEDS_REVIEW，不得照抄"通过"标 DONE。
        logger.info(
            "验证子命令全部返回 0（门禁通过）；如上方存在 ⚠️ 待人工核/引用缺失/区间"
            "警告，按 SKILL.md 收口为 NEEDS_REVIEW，不得标 DONE"
        )
    else:
        logger.error("验证发现异常，请查看上方输出")
    return overall


def cmd_resume(args: argparse.Namespace) -> int:
    """Resume failed/pending segments by querying the breakpoint DB."""
    lib = args.lib
    db_path = os.path.join(lib, ".index_state.db")
    if not os.path.exists(db_path):
        logger.warning("断点库不存在，无需续跑: %s", db_path)
        return 0

    try:
        conn = sqlite3.connect(db_path)
        rows = conn.execute(
            "SELECT segment_path FROM segment_tasks WHERE status != 'done'"
        ).fetchall()
        conn.close()
    except sqlite3.Error as e:
        logger.error("读取断点库失败: %s", e)
        return 1

    if not rows:
        logger.info("没有待续跑的段")
        return 0

    paths = [r[0] for r in rows]
    logger.info("发现 %d 个待续跑段", len(paths))

    # Batch to avoid too-long command line
    batch_size = 50
    overall = 0
    for i in range(0, len(paths), batch_size):
        batch = paths[i : i + batch_size]
        cmd = [
            sys.executable, _python("build_segment_index.py"),
            *batch, "--lib", lib, "--workers", str(args.workers),
        ]
        if args.log_format:
            cmd.extend(["--log-format", args.log_format])
        overall |= _run(cmd)

    return overall


def cmd_delete(args: argparse.Namespace) -> int:
    """Delete a novel via delete_novel.py."""
    if not args.novel or not args.author:
        logger.error("delete 子命令需要 --novel/--author")
        return 2
    cmd = [
        sys.executable, _python("delete_novel.py"),
        "--novel", args.novel, "--author", args.author, "--lib", args.lib,
    ]
    if args.yes:
        cmd.append("--yes")
    if args.log_format:
        cmd.extend(["--log-format", args.log_format])
    return _run(cmd)


def _parse_novel_author(segment_path: str) -> tuple[str, str]:
    """兜底：按段文件名猜 (novel, author)。仅在前端 frontmatter 读取失败时使用。"""
    base = Path(segment_path).stem
    # segment filename pattern: novel-author-range-NNN
    parts = base.rsplit("-", 1)
    if len(parts) == 2 and parts[1].isdigit():
        base = parts[0]
    # heuristic: split on '-' from the right, longest author candidate
    tokens = base.split("-")
    if len(tokens) >= 2:
        return tokens[0], "-".join(tokens[1:])
    return base, ""


def _book_from_segment_path(segment_path: str, cache: dict[str, tuple[str, str]]) -> tuple[str, str]:
    """优先从段文件 frontmatter 取小说名/作者（v4.6.3，书名含 `-` 不再被猜错）。"""
    if segment_path in cache:
        return cache[segment_path]
    try:
        meta, _ = parse_frontmatter(read_text_auto(segment_path))
        novel = str(meta.get("小说名称", "")).strip("《》").strip()
        author = str(meta.get("作者", "")).strip()
        if novel and author:
            cache[segment_path] = (novel, author)
            return novel, author
    except Exception:
        pass
    fallback = _parse_novel_author(segment_path)
    cache[segment_path] = fallback
    return fallback


def cmd_style(args: argparse.Namespace) -> int:
    """Run lightweight or external style distillation for a novel."""
    style_skill = args.style_skill or os.environ.get("INDEX_STYLE_SKILL", "").strip() or None
    result = style_distillation.run_style_distillation(
        args.lib,
        args.novel,
        args.author,
        style_skill=style_skill,
        output_dir=getattr(args, "output_dir", None) or None,
    )
    if result.get("error"):
        logger.error("文风蒸馏失败: %s", result["error"])
        return 1
    if result.get("mode") == "lightweight":
        if result.get("path"):
            logger.info("轻量风格卡素材底稿已生成（最终七维卡须由编排器按协议重写）: %s", result["path"])
        card = result.get("card")
        if card:
            print(card)
    else:
        if result.get("path"):
            logger.info("外部文风技能产物: %s", result["path"])
        else:
            logger.info("外部文风技能执行完成")
    if result.get("json_path"):
        logger.info("结构化风格卡（创作侧机读，契约见 schemas/style-card.schema.json）: %s", result["json_path"])
    return 0


def cmd_semantic(args: argparse.Namespace) -> int:
    """Delegate semantic index build/search to the standalone scripts."""
    cmd: list[str]
    if args.semantic_command == "build":
        cmd = [sys.executable, _python("semantic_index.py"), "--build", "--lib", args.lib]
        if args.model:
            cmd.extend(["--model", args.model])
        if args.force:
            cmd.append("--force")
        if args.batch_size != 32:
            cmd.extend(["--batch-size", str(args.batch_size)])
        if args.log_format:
            cmd.extend(["--log-format", args.log_format])
        return _run(cmd)

    if args.semantic_command == "search":
        cmd = [
            sys.executable, _python("semantic_search.py"),
            args.query, "--lib", args.lib,
            "--top-k", str(args.top_k),
            "--threshold", str(args.threshold),
        ]
        if args.model:
            cmd.extend(["--model", args.model])
        if args.json:
            cmd.append("--json")
        if args.log_format:
            cmd.extend(["--log-format", args.log_format])
        return _run(cmd)

    logger.error("未知 semantic 子命令: %s", args.semantic_command)
    return 2


def cmd_status(args: argparse.Namespace) -> int:
    """Print a global status panel from the breakpoint database."""
    lib = args.lib
    db_path = os.path.join(lib, ".index_state.db")

    print(f"\n📊 索引库状态面板: {lib}")
    print(f"   生成时间: {_now()}")

    if not os.path.exists(db_path):
        print("   断点库尚未创建（还没有执行过索引任务）")
        # Still show filesystem-level counts
        for d in DIRS:
            p = Path(lib) / d
            count = len(list(p.iterdir())) if p.exists() else 0
            print(f"   {d}: {count} 项")
        return 0

    # v4.6.5：先经 IndexDB 打开一次，触发路径归一化与重复行归并迁移，
    # 防止旧库中 `\` 与 `/` 混写导致的段数/token 虚报直接进面板。
    from index_db import IndexDB

    try:
        with IndexDB(lib):
            pass
    except sqlite3.Error as e:
        logger.error("打开断点库失败: %s", e)
        return 1

    try:
        conn = sqlite3.connect(db_path)
        rows = conn.execute(
            """SELECT segment_path, status, retries, error, prompt_tokens,
                      completion_tokens, updated_at
               FROM segment_tasks"""
        ).fetchall()
        conn.close()
    except sqlite3.Error as e:
        logger.error("读取断点库失败: %s", e)
        return 1

    # Aggregate by novel-author
    books: dict[tuple[str, str], dict] = {}
    book_cache: dict[str, tuple[str, str]] = {}
    for segment_path, status, retries, error, p_tokens, c_tokens, updated_at in rows:
        novel, author = _book_from_segment_path(segment_path, book_cache)
        key = (novel, author)
        book = books.setdefault(key, {
            "pending": 0, "done": 0, "failed": 0, "other": 0,
            "prompt": 0, "completion": 0, "errors": [], "last_update": 0,
        })
        book[status if status in ("pending", "done", "failed") else "other"] += 1
        book["prompt"] += p_tokens or 0
        book["completion"] += c_tokens or 0
        book["last_update"] = max(book["last_update"], updated_at or 0)
        if status == "failed" and error:
            book["errors"].append(f"{Path(segment_path).name}: {error}")

    total_done = sum(b["done"] for b in books.values())
    total_failed = sum(b["failed"] for b in books.values())
    total_prompt = sum(b["prompt"] for b in books.values())
    total_completion = sum(b["completion"] for b in books.values())

    print(f"\n   书籍数: {len(books)} | 总段数: {len(rows)}")
    print(f"   已完成: {total_done} | 失败: {total_failed}")
    print(f"   Token 累计: prompt {total_prompt:,} / completion {total_completion:,}")
    print()

    if not books:
        print("   暂无书籍记录")
        return 0

    header = f"{'小说':<20} {'作者':<12} {'待处理':>6} {'已完成':>6} {'失败':>6} {'Prompt':>10} {'Completion':>10} {'最后更新':>19}"
    print(header)
    print("-" * len(header))
    for (novel, author), b in sorted(books.items(), key=lambda kv: -kv[1]["last_update"]):
        last = datetime.datetime.fromtimestamp(b["last_update"]).strftime("%Y-%m-%d %H:%M:%S") if b["last_update"] else "-"
        print(
            f"{novel:<20} {author:<12} "
            f"{b['pending']:>6} {b['done']:>6} {b['failed']:>6} "
            f"{b['prompt']:>10,} {b['completion']:>10,} {last:>19}"
        )

    failed_books = [(k, b) for k, b in books.items() if b["failed"]]
    if failed_books:
        print("\n❌ 失败明细（最近 10 条）:")
        for (novel, author), b in failed_books[:10]:
            print(f"  《{novel}》- {author}: {b['failed']} 段失败")
            for err in b["errors"][:3]:
                print(f"      - {err}")

    print()
    return 0 if total_failed == 0 else 1


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="成人文学索引编排器：集中式入口",
    )
    parser.add_argument("--lib", default=DEFAULT_LIB, help="索引库根目录")
    add_log_format_argument(parser)

    sub = parser.add_subparsers(dest="command", required=True)

    p_init = sub.add_parser("init", help="初始化索引库")
    p_init.set_defaults(func=cmd_init)

    p_import = sub.add_parser("import-manifest", help="按 source-manifest.json 清单导入并建库")
    p_import.add_argument("--manifest", required=True, help="source-manifest.json 路径")
    p_import.add_argument("--novel", default=None, help="覆盖小说名称（默认从清单条目提取）")
    p_import.add_argument("--author", default=None, help="覆盖作者名称（默认从清单条目提取）")
    p_import.add_argument("--volume", default="", help="卷号前缀")
    p_import.add_argument("--chapters-per-seg", type=int, default=5)
    p_import.add_argument("--chars-per-seg", type=int, default=10000)
    p_import.add_argument("--workers", type=int, default=32)
    p_import.add_argument("--force", action="store_true", help="强制覆盖重跑")
    p_import.add_argument("--split-only", action="store_true", help="仅执行拆段与来源登记，不调用大模型构建索引")
    p_import.set_defaults(func=cmd_import_manifest)

    p_build = sub.add_parser("build", help="从源文件完整建库")
    p_build.add_argument("--source", required=True, help="源文件路径")
    p_build.add_argument("--novel", required=True, help="小说名称")
    p_build.add_argument("--author", required=True, help="作者名称")
    p_build.add_argument("--volume", default="", help="卷号前缀（分卷源文件）")
    p_build.add_argument("--chapters-per-seg", type=int, default=5)
    p_build.add_argument("--chars-per-seg", type=int, default=10000)
    p_build.add_argument("--workers", type=int, default=32)
    p_build.add_argument("--force", action="store_true", help="覆盖已有段/索引")
    p_build.add_argument("--skip-existing", action="store_true", help="段未变时跳过索引生成")
    p_build.set_defaults(func=cmd_build)

    p_verify = sub.add_parser("verify", help="执行验证门禁")
    p_verify.set_defaults(func=cmd_verify)

    p_resume = sub.add_parser("resume", help="续跑失败/未完成的段")
    p_resume.add_argument("--workers", type=int, default=32)
    p_resume.set_defaults(func=cmd_resume)

    p_delete = sub.add_parser("delete", help="删除一本书")
    p_delete.add_argument("--novel", required=True)
    p_delete.add_argument("--author", required=True)
    p_delete.add_argument("--yes", action="store_true", help="跳过二次确认")
    p_delete.set_defaults(func=cmd_delete)

    p_status = sub.add_parser("status", help="索引库状态面板")
    p_status.set_defaults(func=cmd_status)

    p_style = sub.add_parser("style", help="文风蒸馏（轻量或外部完整蒸馏）")
    p_style.add_argument("--novel", required=True, help="小说名称")
    p_style.add_argument("--author", required=True, help="作者名称")
    p_style.add_argument(
        "--style-skill",
        default=None,
        help="外部文风技能：技能目录 / .py 入口 / 命令字符串（默认读取 INDEX_STYLE_SKILL）",
    )
    p_style.add_argument(
        "--output-dir",
        default=None,
        help="风格卡输出目录（默认索引库同级 风格卡\\<书名-作者>）",
    )
    p_style.set_defaults(func=cmd_style)

    p_semantic = sub.add_parser("semantic", help="语义索引构建/检索（可选模块，ADR-0004）")
    semantic_sub = p_semantic.add_subparsers(dest="semantic_command", required=True)

    p_sem_build = semantic_sub.add_parser("build", help="构建语义索引")
    p_sem_build.add_argument("--model", default=None, help="embedding 模型名称")
    p_sem_build.add_argument("--force", action="store_true", help="强制重建语义索引")
    p_sem_build.add_argument("--batch-size", type=int, default=32, help="编码批次大小")
    p_sem_build.set_defaults(func=cmd_semantic)

    p_sem_search = semantic_sub.add_parser("search", help="语义检索")
    p_sem_search.add_argument("query", help="自然语言查询")
    p_sem_search.add_argument("--model", default=None, help="embedding 模型名称")
    p_sem_search.add_argument("--top-k", type=int, default=5, help="返回结果数")
    p_sem_search.add_argument("--threshold", type=float, default=0.5, help="相似度阈值")
    p_sem_search.add_argument("--json", action="store_true", help="以 JSON 输出")
    p_sem_search.set_defaults(func=cmd_semantic)

    return parser


def main() -> int:
    parser = _build_parser()
    args = parser.parse_args()
    configure_logging(fmt=args.log_format)

    # Ensure library directories exist for all commands except init
    if args.command != "init":
        for d in DIRS:
            os.makedirs(os.path.join(args.lib, d), exist_ok=True)

    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
