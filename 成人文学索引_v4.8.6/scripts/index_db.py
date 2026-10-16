#!/usr/bin/env python3
"""
索引库 SQLite 断点层（完整移植转录技能 transcript_db.py 模式）

记录每个段任务的处理状态，实现断点续传：
- 任务前查状态：done → 跳过（不重复消耗 API）
- 失败写原因与重试次数
- 中断重跑不重做已完成段

一致性规则：
- DB 标记 done 但目标索引文件缺失 → 视为未完成（文件是最终真相）
- 段文件内容变更（追更并入新章）→ seg_hash 变化 → 视为未完成强制重跑
  （防止行号漂移导致旧索引静默失效）

v3.32 新增：seg_hash（内容哈希防陈旧）+ prompt_tokens/completion_tokens（token 统计）。
v4.6.5 修复：所有写入/查询对路径做 normpath 归一化——Windows 下 `\\` 与 `/`
混写曾绕过 segment_path 主键判重，同一段被记两次（status 虚报段数与 token）；
旧库在线迁移自动归并历史重复行（token 保留累计，状态取 done > failed > pending）。

用法:
  from index_db import IndexDB
  with IndexDB(index_lib_dir) as db:
      db.should_skip(segment_path, out_path)  # 查断点（自动校验 hash）
      db.mark_pending(segment_path, out_path)
      db.mark_done(segment_path, out_path, prompt_tokens=..., completion_tokens=...)
      db.mark_failed(segment_path, error)
"""
import hashlib
import os
import sqlite3
import threading
import time
from pathlib import Path

from _logging import get_logger

DB_NAME = ".index_state.db"

logger = get_logger(__name__)


def normalize_path(path):
    """统一路径分隔符（Windows 下 `\\` 与 `/` 混写会让主键判重失效）。"""
    if not path:
        return path
    return os.path.normpath(str(path))


def file_hash(path):
    """段文件内容 SHA-256（前 16 位 hex）。变更检测用，毫秒级。"""
    try:
        with open(path, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()[:16]
    except OSError:
        return None


def _path_has_book_prefix(path: str | None, prefix: str) -> bool:
    """路径任一部分等于 prefix 或以 `prefix-` 开头（字面量，不解释元字符）。"""
    if not path or not prefix:
        return False
    parts = [part for part in Path(str(path)).parts if part]
    return any(part == prefix or part.startswith(prefix + "-") for part in parts)


def _rename_book_prefix(path: str | None, old_prefix: str, new_prefix: str) -> str | None:
    """替换路径中等于 old_prefix 或以 `old_prefix-` 开头的部分；无变化返回 None。"""
    if not path:
        return None
    parts = list(Path(str(path)).parts)
    changed = False
    for i, part in enumerate(parts):
        if part == old_prefix:
            parts[i] = new_prefix
            changed = True
        elif part.startswith(old_prefix + "-"):
            parts[i] = new_prefix + part[len(old_prefix):]
            changed = True
    if not changed:
        return None
    return os.path.join(*parts)


class IndexDB:
    def __init__(self, index_lib_dir):
        self.path = os.path.join(index_lib_dir, DB_NAME)
        self._lock = threading.Lock()
        # check_same_thread=False：直调引擎在 32 个工作线程中共享连接，用锁串行化
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        with self._lock:
            self.conn.execute(
                """CREATE TABLE IF NOT EXISTS segment_tasks (
                    segment_path TEXT PRIMARY KEY,
                    out_path TEXT,
                    status TEXT NOT NULL DEFAULT 'pending',
                    retries INTEGER NOT NULL DEFAULT 0,
                    error TEXT,
                    seg_hash TEXT,
                    prompt_tokens INTEGER DEFAULT 0,
                    completion_tokens INTEGER DEFAULT 0,
                    created_at INTEGER,
                    updated_at INTEGER
                )"""
            )
            self.conn.execute(
                """CREATE TABLE IF NOT EXISTS ingested_sources (
                    source_id TEXT PRIMARY KEY,
                    platform TEXT,
                    source_url TEXT,
                    txt_path TEXT,
                    sha256 TEXT,
                    status TEXT,
                    novel TEXT,
                    author TEXT,
                    imported_at TEXT
                )"""
            )
            self.conn.commit()
            self._migrate()

    def _migrate(self):
        """旧库（v3.31 无 seg_hash/token 列）在线迁移：ALTER TABLE 补列。"""
        cols = {r[1] for r in self.conn.execute("PRAGMA table_info(segment_tasks)").fetchall()}
        for col, decl in (("seg_hash", "TEXT"), ("prompt_tokens", "INTEGER DEFAULT 0"),
                          ("completion_tokens", "INTEGER DEFAULT 0")):
            if col not in cols:
                self.conn.execute(f"ALTER TABLE segment_tasks ADD COLUMN {col} {decl}")
        self.conn.commit()
        # 迁移提示：旧 done 记录 hash 为空 → should_skip 判定为需重跑（安全策略）
        stale = self.conn.execute(
            "SELECT COUNT(*) FROM segment_tasks WHERE status='done' AND (seg_hash IS NULL OR seg_hash='')"
        ).fetchone()[0]
        if stale:
            logger.info(f"  ℹ️  断点库迁移：{stale} 条旧 done 记录无内容哈希，将按变更重新生成（安全策略）")
        self._dedupe_separators()

    _STATUS_RANK = {"done": 2, "failed": 1, "pending": 0}

    def _dedupe_separators(self):
        """v4.6.5：归并因 `\\` / `/` 分隔符混写产生的重复任务行。

        同一段文件的两个写法会各自 INSERT 成功（主键是原始字符串），导致 status
        面板段数与 token 虚报。归并规则：状态取优先级最高者（done > failed >
        pending），retries 取大，token 保留累计（两次执行都真实消耗了 API），
        归并后把保留行的路径改写为归一化形式。
        """
        rows = self.conn.execute(
            "SELECT rowid, segment_path FROM segment_tasks ORDER BY rowid"
        ).fetchall()
        groups: dict[str, list[int]] = {}
        for rowid, seg in rows:
            groups.setdefault(normalize_path(seg), []).append(rowid)

        merged = 0
        for rowids in groups.values():
            if len(rowids) < 2:
                continue
            primary = rowids[0]
            for dup in rowids[1:]:
                cols = ("status", "retries", "seg_hash", "prompt_tokens", "completion_tokens", "error")
                pri = self.conn.execute(
                    f"SELECT {', '.join(cols)} FROM segment_tasks WHERE rowid=?", (primary,)
                ).fetchone()
                dup_row = self.conn.execute(
                    f"SELECT {', '.join(cols)} FROM segment_tasks WHERE rowid=?", (dup,)
                ).fetchone()
                status = max((pri[0], dup_row[0]), key=lambda s: self._STATUS_RANK.get(s, 0))
                seg_hash = pri[2] or dup_row[2]
                error = None if status == "done" else (pri[5] or dup_row[5])
                self.conn.execute(
                    "UPDATE segment_tasks SET status=?, retries=?, seg_hash=?, "
                    "prompt_tokens=?, completion_tokens=?, error=? WHERE rowid=?",
                    (
                        status,
                        max(pri[1] or 0, dup_row[1] or 0),
                        seg_hash,
                        (pri[3] or 0) + (dup_row[3] or 0),
                        (pri[4] or 0) + (dup_row[4] or 0),
                        error,
                        primary,
                    ),
                )
                self.conn.execute("DELETE FROM segment_tasks WHERE rowid=?", (dup,))
                merged += 1

        # 保留行路径改写为归一化形式，防止下次再分叉
        changed = 0
        for rowid, seg in self.conn.execute("SELECT rowid, segment_path FROM segment_tasks").fetchall():
            norm = normalize_path(seg)
            if norm != seg:
                self.conn.execute("UPDATE segment_tasks SET segment_path=? WHERE rowid=?", (norm, rowid))
                changed += 1
        out_rows = self.conn.execute("SELECT rowid, out_path FROM segment_tasks WHERE out_path IS NOT NULL").fetchall()
        for rowid, out in out_rows:
            norm = normalize_path(out)
            if norm != out:
                self.conn.execute("UPDATE segment_tasks SET out_path=? WHERE rowid=?", (norm, rowid))
                changed += 1
        if merged or changed:
            self.conn.commit()
        if merged:
            logger.info(f"  ℹ️  断点库归并：{merged} 条因路径分隔符混写产生的重复记录")

    def close(self):
        with self._lock:
            self.conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def _now(self):
        return int(time.time())

    def get(self, segment_path):
        with self._lock:
            row = self.conn.execute(
                "SELECT segment_path, out_path, status, retries, error, seg_hash FROM segment_tasks WHERE segment_path=?",
                (normalize_path(segment_path),),
            ).fetchone()
        return row

    def should_skip(self, segment_path, out_path, seg_hash=None):
        """断点判断：done + 索引文件存在 + 段文件内容未变 → 跳过"""
        if seg_hash is None:
            seg_hash = file_hash(segment_path)
        row = self.get(segment_path)
        if row and row[2] == "done" and os.path.exists(segment_path) and os.path.exists(out_path):
            # 旧版本迁移记录没有 seg_hash，必须重跑一次建立安全锚点；
            # 缺失源文件也不能把 None==None 当成已完成。
            return bool(row[5]) and row[5] == seg_hash
        return False

    def mark_pending(self, segment_path, out_path, seg_hash=None):
        if seg_hash is None:
            seg_hash = file_hash(segment_path)
        now = self._now()
        with self._lock:
            self.conn.execute(
                """INSERT INTO segment_tasks
                   (segment_path, out_path, status, seg_hash, error, created_at, updated_at)
                   VALUES (?, ?, 'pending', ?, NULL, ?, ?)
                   ON CONFLICT(segment_path) DO UPDATE SET
                     out_path=excluded.out_path, status='pending', seg_hash=excluded.seg_hash,
                     error=NULL, updated_at=excluded.updated_at""",
                (normalize_path(segment_path), normalize_path(out_path), seg_hash, now, now),
            )
            self.conn.commit()

    def mark_done(self, segment_path, out_path, prompt_tokens=0, completion_tokens=0, seg_hash=None):
        if seg_hash is None:
            seg_hash = file_hash(segment_path)
        now = self._now()
        with self._lock:
            # v4.5 审查修复（P2）：UPSERT 对齐 mark_failed——此前只 UPDATE，对不存在的
            # 行静默 no-op（断点跳过判断失效重跑却无提示），正常流程有 mark_pending
            # 先行所以不触发，但误用无任何提示；现在与 mark_failed 语义一致
            self.conn.execute(
                """INSERT INTO segment_tasks (segment_path, out_path, status, seg_hash,
                                              prompt_tokens, completion_tokens, created_at, updated_at)
                   VALUES (?, ?, 'done', ?, ?, ?, ?, ?)
                   ON CONFLICT(segment_path) DO UPDATE SET
                     out_path=excluded.out_path, status='done', error=NULL,
                     seg_hash=excluded.seg_hash, prompt_tokens=excluded.prompt_tokens,
                     completion_tokens=excluded.completion_tokens, updated_at=excluded.updated_at""",
                (normalize_path(segment_path), normalize_path(out_path), seg_hash,
                 prompt_tokens, completion_tokens, now, now),
            )
            self.conn.commit()

    def mark_failed(self, segment_path, error):
        now = self._now()
        with self._lock:
            self.conn.execute(
                """INSERT INTO segment_tasks (segment_path, status, retries, error, created_at, updated_at)
                   VALUES (?, 'failed', 1, ?, ?, ?)
                   ON CONFLICT(segment_path) DO UPDATE SET
                     status='failed', retries=retries+1, error=?, updated_at=?""",
                (normalize_path(segment_path), error[:500], now, now, error[:500], now),
            )
            self.conn.commit()

    def stats(self):
        with self._lock:
            rows = self.conn.execute(
                "SELECT status, COUNT(*) FROM segment_tasks GROUP BY status"
            ).fetchall()
            toks = self.conn.execute(
                "SELECT SUM(prompt_tokens), SUM(completion_tokens) FROM segment_tasks WHERE status='done'"
            ).fetchone()
        return dict(rows), (toks[0] or 0, toks[1] or 0)

    def delete_by_prefix(self, prefix: str) -> int:
        """删除断点记录中路径包含 `<prefix>` 目录或 `<prefix>-` 文件名的行（字面量匹配）。

        供 delete_novel 使用；不把书名拼进 SQL LIKE / glob，避免 `[`、`%`、`_`
        等元字符误匹配其他书。
        """
        with self._lock:
            rows = self.conn.execute(
                "SELECT segment_path, out_path FROM segment_tasks"
            ).fetchall()
            targets = [
                seg for seg, out in rows
                if _path_has_book_prefix(seg, prefix) or _path_has_book_prefix(out, prefix)
            ]
            for seg in targets:
                self.conn.execute(
                    "DELETE FROM segment_tasks WHERE segment_path = ?", (seg,)
                )
            self.conn.commit()
            return len(targets)

    def rename_prefix(self, old_prefix: str, new_prefix: str) -> int:
        """重命名断点记录中包含 `<old_prefix>` 目录/文件名的路径（字面量匹配）。"""
        with self._lock:
            rows = self.conn.execute(
                "SELECT segment_path, out_path FROM segment_tasks"
            ).fetchall()
            updated = 0
            for seg, out in rows:
                new_seg = _rename_book_prefix(seg, old_prefix, new_prefix)
                new_out = _rename_book_prefix(out, old_prefix, new_prefix)
                if new_seg is None and new_out is None:
                    continue
                self.conn.execute(
                    "UPDATE segment_tasks SET segment_path = ?, out_path = ? WHERE segment_path = ?",
                    (new_seg or seg, new_out or out, seg),
                )
                updated += 1
            self.conn.commit()
            return updated

    def get_ingested_sources(self) -> dict[str, dict]:
        """返回已摄取源清单 {source_id: {...}}。"""
        with self._lock:
            rows = self.conn.execute(
                """SELECT source_id, platform, source_url, txt_path, sha256,
                          status, novel, author, imported_at
                   FROM ingested_sources"""
            ).fetchall()
            result = {}
            for r in rows:
                result[r[0]] = {
                    "source_id": r[0],
                    "platform": r[1],
                    "source_url": r[2],
                    "txt_path": r[3],
                    "sha256": r[4],
                    "status": r[5],
                    "novel": r[6],
                    "author": r[7],
                    "imported_at": r[8],
                }
            return result

    def record_ingested_source(self, *, source_id: str, platform: str, source_url: str,
                               txt_path: str, sha256: str, status: str,
                               novel: str, author: str, imported_at: str | None = None) -> None:
        """记录或更新已摄取源。"""
        import datetime
        now = imported_at or datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with self._lock:
            self.conn.execute(
                """INSERT INTO ingested_sources (source_id, platform, source_url, txt_path, sha256, status, novel, author, imported_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(source_id) DO UPDATE SET
                     platform=excluded.platform, source_url=excluded.source_url,
                     txt_path=excluded.txt_path, sha256=excluded.sha256,
                     status=excluded.status, novel=excluded.novel,
                     author=excluded.author, imported_at=excluded.imported_at""",
                (str(source_id), str(platform), str(source_url), str(txt_path),
                 str(sha256).lower(), str(status), str(novel), str(author), now),
            )
            self.conn.commit()
