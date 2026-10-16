# -*- coding: utf-8 -*-
"""发布自证：包必须能脱离作者柜根工作，且三类拒绝要说清真原因。

为什么单开这个文件（都是现场实测出来的缺口，不是设想）：
- `scripts/orchestrate.py:174` 从 `SKILL_DIR.parent/"schemas"` 取清单校验器。收件机上没有柜根，
  `import-manifest` 会以「加载清单校验器异常（拒收）」失败——**报的还是错因**，用户以为是自己清单坏了。
- `tests/test_manifest_ingest.py:12-15` 自己也从柜根取校验器，所以新宿主连包内测试都跑不起来。
- 工单 T09 第 1 项要的四类拒绝里，重复 ID 与更新失败这两类在产品侧没有测试。
- 产品根目录没有 LICENSE/VERSION，`SKILL.md:5` 只写了 license: Proprietary，版本没有单一权威点。
"""
from __future__ import annotations

import json
import re
import shutil
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ORCHESTRATE = str(ROOT / "scripts" / "orchestrate.py")
VENDOR_DIR = ROOT / "scripts" / "vendor"
# 本文件同时也证明「收件人能跑包内测试」：测试自己也只认包内 vendor，不碰柜根
sys.path.insert(0, str(VENDOR_DIR))
sys.path.insert(0, str(ROOT / "scripts"))
import source_manifest as sm  # noqa: E402

SYNthetic_TEXT = "第一回 试笔\n\n他推开门，雨气混着栀子味涌进来。合成稿，不含真实书目内容。\n"


def _child_utf8() -> dict:
    """被测 CLI 子进程固定按 UTF-8 输出，且不往被验收目录留字节码。

    中文 Windows 的本地编码是 GBK：子进程写 UTF-8、父进程按 GBK 解码就成了乱码，
    「重复」这类关键字断言会假红（2026-10-02 普通环境实测）。
    """
    return dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="utf-8", PYTHONDONTWRITEBYTECODE="1")


def _manifest_with(tmp_path: Path, items: list[dict]) -> Path:
    path = tmp_path / "source-manifest.json"
    path.write_text(json.dumps(sm.build_manifest(generator="release-test", items=items),
                               ensure_ascii=False), encoding="utf-8")
    return path


def _item(txt_path: Path, source_id: str) -> dict:
    return sm.create_manifest_item(source_id=source_id, platform="synthetic",
                                   source_url=f"synthetic://{source_id}",
                                   txt_path=txt_path.name, sha256=sm.sha256_file(txt_path),
                                   status="completed", title="合成书目")


def test_import_manifest_runs_without_cabinet_schemas(tmp_path):
    """把包拷到「柜根不存在」的陌生父目录下，import-manifest 必须照样跑通。"""
    txt = tmp_path / "合成-第01回.txt"
    txt.write_text(SYNthetic_TEXT, encoding="utf-8")
    manifest = _manifest_with(tmp_path, [_item(txt, "SYN0001")])

    # 只带包内文件，父目录故意不放 schemas —— 这就是收件人的现场
    pkg = tmp_path / "收件" / "成人文学索引_v4.8.3"
    pkg.mkdir(parents=True)
    shutil.copytree(ROOT / "scripts", pkg / "scripts",
                    ignore=shutil.ignore_patterns("__pycache__", "*.egg-info"))

    lib = tmp_path / "lib"
    res = subprocess.run(
        [sys.executable, str(pkg / "scripts" / "orchestrate.py"), "--lib", str(lib),
         "import-manifest", "--manifest", str(manifest), "--split-only"],
        capture_output=True, text=True, encoding="utf-8", errors="replace", cwd=str(pkg))
    out = res.stdout + res.stderr
    assert res.returncode == 0, f"脱离柜根后必须可用，实得 rc={res.returncode}\n{out[-400:]}"
    assert "校验器异常" not in out, out[-400:]
    assert "SYN0001" in out, out[-300:]


def test_duplicate_source_id_is_rejected_naming_the_id(tmp_path):
    """重复 ID 必须拒收，且原因要点明是重复，不能是「校验器坏了」。"""
    txt = tmp_path / "合成-第01回.txt"
    txt.write_text(SYNthetic_TEXT, encoding="utf-8")
    manifest = _manifest_with(tmp_path, [_item(txt, "SYNDUP"), _item(txt, "SYNDUP")])

    res = subprocess.run(
        [sys.executable, ORCHESTRATE, "--lib", str(tmp_path / "lib"),
         "import-manifest", "--manifest", str(manifest)],
        env=_child_utf8(),
        capture_output=True, text=True, encoding="utf-8", errors="replace")
    out = res.stdout + res.stderr
    assert res.returncode != 0, f"重复 ID 竟然被放行: {out[-300:]}"
    assert "重复" in out, out[-400:]
    assert "SYNDUP" in out, f"要指名道姓才好用: {out[-400:]}"


def test_failed_item_is_not_recorded_as_ingested(tmp_path, monkeypatch):
    """建库失败的条目不得登记为已导入——登记了就会被判 unchanged 而永不重做。"""
    txt = tmp_path / "合成-第01回.txt"
    txt.write_text(SYNthetic_TEXT, encoding="utf-8")
    manifest = _manifest_with(tmp_path, [_item(txt, "SYNFAIL")])
    lib = tmp_path / "lib"

    import importlib

    orchestrate = importlib.import_module("orchestrate")
    monkeypatch.setattr(orchestrate, "cmd_build", lambda args: 1)
    args = orchestrate.build_arg_parser().parse_args(
        ["--lib", str(lib), "import-manifest", "--manifest", str(manifest)]) if hasattr(
        orchestrate, "build_arg_parser") else None
    if args is None:
        import argparse

        args = argparse.Namespace(lib=str(lib), manifest=str(manifest), novel=None, author=None,
                                 volume="", chapters_per_seg=10, chars_per_seg=20000,
                                 workers=1, force=False, log_format=None, split_only=False)
    rc = orchestrate.cmd_import_manifest(args)
    assert rc != 0, "建库失败必须让整条命令失败"

    from index_db import IndexDB

    with IndexDB(str(lib)) as db:
        assert "SYNFAIL" not in db.get_ingested_sources(), "失败条目不得被登记为已导入"


def test_release_identity_files_exist_and_agree(tmp_path):
    """版本要有单一权威点，许可要落成文件——只有 frontmatter 一句话，收件人无法核验。"""
    version_file = ROOT / "VERSION"
    license_file = ROOT / "LICENSE"
    assert version_file.is_file(), "缺 VERSION：目录名/frontmatter/发布清单三处版本没有权威点"
    assert license_file.is_file(), "缺 LICENSE：只有 frontmatter 声明，分发时没有许可文本"
    declared = version_file.read_text(encoding="utf-8").strip()
    front = (ROOT / "SKILL.md").read_text(encoding="utf-8", errors="replace")
    m = re.search(r'version:\s*"?([\d][\w.\-]*)"?', front)
    assert m, front[:400]
    assert m.group(1) == declared, f"SKILL.md 版本 {m.group(1)} ≠ VERSION {declared}"
