# -*- coding: utf-8 -*-
import json
import os
import subprocess
import sys
from pathlib import Path
import pytest
from conftest import run_py

ROOT = Path(__file__).resolve().parent.parent
ORCHESTRATE = str(ROOT / "scripts" / "orchestrate.py")
# 校验器随包 vendor 在 scripts/vendor/：以前这里从 ROOT.parent/"schemas" 取柜根共享内核，
# 收件机没有作者柜根就整包测试都跑不起来（工单 K：独立包不得依赖柜子父目录）。
VENDOR_DIR = ROOT / "scripts" / "vendor"
if str(VENDOR_DIR) not in sys.path:
    sys.path.insert(0, str(VENDOR_DIR))
import source_manifest as sm  # noqa: E402


def _run(*args: str) -> subprocess.CompletedProcess:
    return run_py([ORCHESTRATE, *args], check=False)


def test_manifest_ingest_success_and_replay(tmp_path):
    """测试通过 source-manifest.json 正常导入并记录 source_id，重复导入判定为 unchanged。"""
    lib = tmp_path / "lib"
    txt_file = tmp_path / "渡口_第01回.txt"
    txt_file.write_text("第一回 渡口相逢\n这是小说的第一段测试正文。\n", encoding="utf-8")
    sha = sm.sha256_file(txt_file)

    manifest_file = tmp_path / "source-manifest.json"
    item = sm.create_manifest_item(
        source_id="BV1test123",
        platform="bilibili",
        source_url="https://www.bilibili.com/video/BV1test123",
        txt_path=txt_file.name,
        sha256=sha,
        status="completed",
        title="渡口风云",
    )
    manifest_data = sm.build_manifest(
        generator="test",
        items=[item],
    )
    manifest_file.write_text(json.dumps(manifest_data, ensure_ascii=False), encoding="utf-8")

    # 执行 import-manifest
    res = _run("--lib", str(lib), "import-manifest", "--manifest", str(manifest_file), "--author", "测试作者", "--split-only")
    assert res.returncode == 0, f"STDOUT: {res.stdout}\nSTDERR: {res.stderr}"
    assert "BV1test123" in res.stdout

    # 验证断点库中的记录
    sys.path.insert(0, str(ROOT / "scripts"))
    from index_db import IndexDB

    with IndexDB(str(lib)) as db:
        known = db.get_ingested_sources()
        assert "BV1test123" in known
        assert known["BV1test123"]["sha256"] == sha
        assert known["BV1test123"]["novel"] == "渡口风云"

    # 再次导入相同的 manifest：应判定为 0 个新条目，0 个更新，退出码为 0
    res2 = _run("--lib", str(lib), "import-manifest", "--manifest", str(manifest_file), "--author", "测试作者", "--split-only")
    assert res2.returncode == 0
    assert "无需更新" in res2.stdout or "0 个待处理" in res2.stdout or "无变化" in res2.stdout


def test_manifest_ingest_retranscription_update(tmp_path):
    """测试重转后文件哈希更新，索引端识别为 updated 并更新库记录。"""
    lib = tmp_path / "lib"
    txt_file = tmp_path / "渡口_第01回.txt"
    txt_file.write_text("版本1 原文内容\n", encoding="utf-8")
    sha1 = sm.sha256_file(txt_file)

    manifest_file = tmp_path / "source-manifest.json"
    item = sm.create_manifest_item(
        source_id="BV1update",
        platform="bilibili",
        source_url="https://www.bilibili.com/video/BV1update",
        txt_path=txt_file.name,
        sha256=sha1,
        status="completed",
        title="重转测试",
    )
    manifest_file.write_text(
        json.dumps(sm.build_manifest(generator="test", items=[item]), ensure_ascii=False),
        encoding="utf-8",
    )

    res1 = _run("--lib", str(lib), "import-manifest", "--manifest", str(manifest_file), "--split-only")
    assert res1.returncode == 0

    # 重转：文本更新
    txt_file.write_text("版本2 校对后的内容更丰富\n", encoding="utf-8")
    sha2 = sm.sha256_file(txt_file)
    item2 = sm.create_manifest_item(
        source_id="BV1update",
        platform="bilibili",
        source_url="https://www.bilibili.com/video/BV1update",
        txt_path=txt_file.name,
        sha256=sha2,
        status="completed",
        title="重转测试",
    )
    manifest_file.write_text(
        json.dumps(sm.build_manifest(generator="test", items=[item2]), ensure_ascii=False),
        encoding="utf-8",
    )

    res2 = _run("--lib", str(lib), "import-manifest", "--manifest", str(manifest_file), "--split-only")
    assert res2.returncode == 0
    assert "重转" in res2.stdout or "更新" in res2.stdout

    sys.path.insert(0, str(ROOT / "scripts"))
    from index_db import IndexDB

    with IndexDB(str(lib)) as db:
        known = db.get_ingested_sources()
        assert known["BV1update"]["sha256"] == sha2


def test_manifest_ingest_rejects_missing_or_corrupt_files(tmp_path):
    """测试索引端严格拒收文件不存在或哈希篡改的清单。"""
    lib = tmp_path / "lib"
    fake_txt = tmp_path / "不存在的.txt"

    manifest_file = tmp_path / "source-manifest.json"
    item = sm.create_manifest_item(
        source_id="BV_bad",
        platform="bilibili",
        source_url="https://www.bilibili.com/video/BV_bad",
        txt_path=fake_txt.name,
        sha256="0" * 64,
        status="completed",
    )
    manifest_file.write_text(
        json.dumps(sm.build_manifest(generator="test", items=[item]), ensure_ascii=False),
        encoding="utf-8",
    )

    res = _run("--lib", str(lib), "import-manifest", "--manifest", str(manifest_file))
    assert res.returncode != 0
    output = res.stdout + res.stderr
    assert "拒收" in output or "错误" in output or "不存在" in output or "校验失败" in output
