# -*- coding: utf-8 -*-
"""IDX-03 回归：--force 全量重切必须隔离旧多余段，杜绝章节重叠残留。

缺陷：成人文学索引缺少 Stager 阶段隔离机制，--force 重切直接覆盖写，
旧切法残留段文件不清理，在用目录出现章节重叠与重复段。

受控 RED：段数变少重切场景，旧多余段必须被安全移出在用目录至 .superseded/。
全离线，不调模型。
"""
from __future__ import annotations

import os
import pathlib
import subprocess
import sys

SCRIPTS = pathlib.Path(__file__).resolve().parents[1] / "scripts"
PY = sys.executable
NOVEL, AUTHOR = "隔离验证", "验证作者"
PAIR = f"{NOVEL}-{AUTHOR}"


def env_for(tmp_path):
    env = {k: v for k, v in os.environ.items()
           if not any(s in k.upper() for s in
                      ("KEY", "TOKEN", "SECRET", "PROXY", "ENDPOINT", "OPENAI", "SUMMARY_"))}
    env.update({"INDEX_LIB_DIR": "", "PYTHONIOENCODING": "utf-8",
                "PYTHONPYCACHEPREFIX": str(tmp_path / "pyc"),
                "TEMP": str(tmp_path), "TMP": str(tmp_path)})
    return env


def run(tmp_path, script, *args):
    return subprocess.run([PY, "-X", "utf8", str(SCRIPTS / script), *[str(a) for a in args]],
                          cwd=str(SCRIPTS.parent), env=env_for(tmp_path), capture_output=True,
                          timeout=300, encoding="utf-8", errors="replace")


def write_source(path, chapters):
    path.write_text("# 隔离验证\n" + "".join(
        f"第{n}章 标题{n}\n这是第{n}章的正文内容，独有句子{n}。\n" for n in range(1, chapters + 1)),
        encoding="utf-8")


def new_lib(tmp_path):
    lib = tmp_path / "lib"
    for d in ("小说原文", "小说索引", "章节索引"):
        (lib / d).mkdir(parents=True)
    return lib


def split(tmp_path, lib, src, per_seg=1, force=False):
    args = [src, "--novel", NOVEL, "--author", AUTHOR, "--lib", lib,
            "--chapters-per-seg", str(per_seg)]
    if force:
        args.append("--force")
    return run(tmp_path, "split_segments.py", *args)


def segs(lib):
    d = lib / "小说原文" / PAIR
    return sorted(p.name for p in d.glob("*.md")) if d.is_dir() else []


def quarantine(lib):
    return sorted(str(p.relative_to(lib)).replace(os.sep, "/") for p in lib.rglob("*")
                  if p.is_file() and ".superseded" in p.parts)


def test_force_resplit_removes_stale_segments(tmp_path):
    """段数变少重切：旧多余段必须移出在用目录，与干净重切终态一致。"""
    src = tmp_path / "src.txt"
    ref, inc = new_lib(tmp_path), new_lib(tmp_path / "i")
    write_source(src, 5)
    assert split(tmp_path, inc, src).returncode == 0
    assert len(segs(inc)) == 5
    # 段数变少：5 章 1 章/段 -> 5 章 5 章/段，只剩 1 段
    write_source(src, 5)
    assert split(tmp_path, ref, src, per_seg=5).returncode == 0
    r = split(tmp_path, inc, src, per_seg=5, force=True)
    assert r.returncode == 0, (r.stdout or "")[-500:] + (r.stderr or "")[-500:]
    assert segs(inc) == segs(ref), f"force 重切留下过期段：{sorted(set(segs(inc)) - set(segs(ref)))}"
    assert len(segs(inc)) == 1, f"在用目录仍有重叠残留段：{segs(inc)}"
    moved = quarantine(inc)
    assert len(moved) == 5, f"旧 5 段必须全部隔离至 .superseded/：{moved}"


def test_force_resplit_quarantines_index_together(tmp_path):
    """被取代段与其章节索引一起可恢复隔离，不删除。"""
    src = tmp_path / "src.txt"
    lib = new_lib(tmp_path)
    write_source(src, 3)
    assert split(tmp_path, lib, src, per_seg=1).returncode == 0
    for name in segs(lib):
        stem = name[:-len(".md")]
        (lib / "章节索引" / f"{stem}-idx.md").write_text(
            "---\n小说名称: 《隔离验证》\n作者: 验证作者\n---\n索引正文\n", encoding="utf-8")
    before_idx = sorted(p.name for p in (lib / "章节索引").glob("*.md"))
    assert len(before_idx) == 3
    write_source(src, 3)
    r = split(tmp_path, lib, src, per_seg=3, force=True)
    assert r.returncode == 0, (r.stdout or "")[-500:] + (r.stderr or "")[-500:]
    assert len(segs(lib)) == 1
    live_idx = sorted(p.name for p in (lib / "章节索引").glob("*.md"))
    assert live_idx == [], f"旧 3 索引必须全部随段隔离，在用目录不得残留：{live_idx}"
    moved = quarantine(lib)
    seg_moved = [m for m in moved if "小说原文" in m]
    idx_moved = [m for m in moved if "章节索引" in m]
    assert len(seg_moved) == 3, f"旧 3 段未隔离：{moved}"
    assert len(idx_moved) == 3, f"旧 3 索引未随段隔离：{moved}"
