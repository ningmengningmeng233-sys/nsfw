#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""包内自检门：一条命令在**当前树**里跑完，开发树与发布包同一把尺。

为什么要有这条（都是别的产品踩过的坑，不是设想）：
- 共享协议件以前从作者柜根偷偷导入，收件机上报成「加载清单校验器异常」，
  把协议缺件说成用户的清单坏了 → 本门必须验「脱离柜根可用」。
- 版本没有单一权威点时，发布清单、目录名、frontmatter 会各说一套（知识库转录现场就有 5.4.2/5.4.4/5.4.5 三份）。
- vendor 副本与主干漂移后没人知道 → 摘要按 `scripts/vendor/VENDOR.json` 登记逐项比。
- 私人 `.env` 与凭据形状不能进发布包 → 只有在包内（`--as-package` 或给了 `--manifest`）才判这一类，
  开发树本来就有 `.env`，拿它判红等于让门失去日常可用性。

约定：末行 `Result: N/M checks passed`；exit 0 只在无未过项时。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
import tempfile
import os
from pathlib import Path

# 门是检查器：自己进程与子进程都不往被验收目录留字节码（见 tests/test_gate_self_hygiene.py D1）
sys.dont_write_bytecode = True

ROOT = Path(__file__).resolve().parents[1]
SHIPPED_TESTS = ["tests/test_release_package.py", "tests/test_manifest_ingest.py",
                 "tests/conftest.py"]
PRIVATE_PATHS = [re.compile(r"[A-Z]:[\\/](?:Users|迁移|Janson)", re.I),
                 re.compile(r"[A-Z]:[\\/].*自制skill", re.I),
                 re.compile(r"/home/[a-z0-9_]+/", re.I)]
CREDENTIAL_SHAPES = [
    ("SECRET_VALUE", re.compile(r"\b(?:sk|ghp|github_pat|xox[bap])[-_][A-Za-z0-9\-]{16,}")),
    ("BEARER_LONG", re.compile(r"bearer\s+[A-Za-z0-9\-_]{24,}", re.I)),
]
FORBIDDEN_MEMBERS = re.compile(r"(^|/)\.env$|\.git/|__pycache__|\.egg-info|/vendor\.off")
TEXT_SUFFIXES = {".py", ".md", ".json", ".toml", ".txt", ".yaml", ".yml", ".example"}


def sha256_file(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def row(name: str, passed: bool, detail: str, status: str = "") -> dict:
    return {"name": name, "passed": bool(passed), "status": status or ("PASS" if passed else "FAIL"),
            "detail": detail[:1200]}


def _child_env(write_bytecode: bool = False) -> tuple[dict, Path]:
    """门启动的任何子进程：字节码一律落在树外前缀，输出按 UTF-8。

    门不能要求调用方先设 PYTHONDONTWRITEBYTECODE——那等于用测量环境替被测对象过门。
    write_bytecode=True 只给编译检查用：它必须真的产出 .pyc（落在树外），
    否则「编译检查」会退化成 0 字节的空跑。
    """
    prefix = Path(tempfile.mkdtemp(prefix="gate-child-pyc-", dir=str(_gate_tmp_root())))
    env = dict(os.environ, PYTHONPYCACHEPREFIX=str(prefix), PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
    if not write_bytecode:
        env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env, prefix



def check_imports(root: Path) -> dict:
    """包内自检：编排器必须能在没有柜根的目录下加载到校验器。"""
    code = (
        "import sys; sys.path.insert(0, r'%s');"
        "import orchestrate as o;"
        "m = o.load_source_manifest();"
        "print('validator=' + m.__file__)"
    % str(root / "scripts"))
    env, _prefix = _child_env()
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                          encoding="utf-8", errors="replace", cwd=str(root),
                          env=env)
    out = (proc.stdout + proc.stderr).strip()
    if proc.returncode != 0:
        return row("validator_importable", False, f"rc={proc.returncode}: {out[-300:]}")
    inside = str(root / "scripts" / "vendor") in out.replace("/", "\\")
    return row("validator_importable", inside,
               out[-300:] + ("" if inside else " —— 校验器不是包内件，收件机将必断"))


def check_vendor_digests(root: Path) -> dict:
    reg = root / "scripts" / "vendor" / "VENDOR.json"
    if not reg.is_file():
        return row("vendor_digest", False, "NOT_RUN: 缺 scripts/vendor/VENDOR.json 登记", "NOT_RUN")
    doc = json.loads(reg.read_text(encoding="utf-8"))
    bad, checked = [], 0
    for item in doc.get("items", []):
        target = root / item["path"]
        checked += 1
        if not target.is_file():
            bad.append(f"{item['path']} 缺席")
            continue
        actual = sha256_file(target)[: int(item.get("sha256_digits", 64))]
        if actual != item["sha256"]:
            bad.append(f"{item['path']} 摘要漂移 {actual} ≠ 登记 {item['sha256']}")
    return row("vendor_digest", not bad,
               f"登记 {checked} 项，漂移/缺席 {len(bad)}：{bad[:3] if bad else '无'}")


def check_identity(root: Path) -> dict:
    version_file = root / "VERSION"
    skill = root / "SKILL.md"
    if not (version_file.is_file() and skill.is_file()):
        return row("version_single_source", False, "缺 VERSION 或 SKILL.md")
    declared = version_file.read_text(encoding="utf-8").strip()
    m = re.search(r'version:\s*"?([\d][\w.\-]*)"?', skill.read_text(encoding="utf-8", errors="replace"))
    if not m:
        return row("version_single_source", False, "SKILL.md 里找不到 version 字段")
    same = m.group(1) == declared
    return row("version_single_source", same,
               f"VERSION={declared} SKILL.md={m.group(1)}"
               + ("" if same else " —— 两处不一致，发布清单就会各说一套"))


def _gate_tmp_root() -> Path:
    """编译产物落盘位置：显式 GATE_TMP_ROOT 优先，否则走系统临时目录；绝不让字节码落在被验收的树里。"""
    env = os.environ.get("GATE_TMP_ROOT", "").strip()
    cand = Path(env) if env else Path(tempfile.gettempdir()) / "gate-pyc"
    try:
        cand.mkdir(parents=True, exist_ok=True)
        return cand
    except OSError:
        return Path(tempfile.gettempdir())


def compile_scripts_outside_tree(scripts_dir: Path) -> tuple[bool, str]:
    """用子进程 + PYTHONPYCACHEPREFIX 把 .pyc 引到树外临时目录。

    上一版直接 `compileall.compile_dir(force=True)` 把字节码写进 `scripts/__pycache__`，
    同一把门的卫生项又禁止 `__pycache__` —— 门自己把自己判红，收件人照 INSTALL.md 跑必见 8/9。
    这里既不放宽卫生检查，也不跳过缓存检查，只把产物挪出被验收目录。
    """
    env, prefix = _child_env(write_bytecode=True)
    try:
        proc = subprocess.run([sys.executable, "-m", "compileall", "-q", "-f", str(scripts_dir)],
                              capture_output=True, text=True, encoding="utf-8", errors="replace",
                              env=env, timeout=300)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"编译子进程异常：{exc}"
    polluted = sorted(str(p.relative_to(scripts_dir.parent).as_posix())
                      for p in scripts_dir.rglob("__pycache__"))
    produced = sum(1 for p in prefix.rglob("*.pyc"))
    out = (proc.stdout or "") + (proc.stderr or "")
    if polluted:
        return False, f"字节码仍落在被验收目录内：{polluted[:3]}"
    detail = (f"编译产物引到树外（前缀 {prefix.name}，生成 {produced} 个 .pyc）；"
              f"树内 __pycache__ 目录 0；退出码 {proc.returncode}"
              + ("" if proc.returncode == 0 else "；输出尾 " + out[-160:]))
    return proc.returncode == 0, detail



def check_python_compiles(root: Path) -> dict:
    ok, detail = compile_scripts_outside_tree(root / "scripts")
    return row("compileall", ok, detail)


def check_shipped_tests(root: Path) -> dict:
    present = [p for p in SHIPPED_TESTS if (root / p).is_file()]
    if not present:
        return row("shipped_acceptance", False, "NOT_RUN: 包内一个验收测试文件都没有", "NOT_RUN")
    env, _prefix = _child_env()
    try:
        proc = subprocess.run([sys.executable, "-m", "pytest", *present, "-q",
                              "-p", "no:cacheprovider"],
                              capture_output=True, text=True, encoding="utf-8",
                              errors="replace", cwd=str(root), env=env, timeout=600)
    except FileNotFoundError:
        return row("shipped_acceptance", False, "NOT_RUN: 本机没有 python/pytest", "NOT_RUN")
    except subprocess.TimeoutExpired:
        return row("shipped_acceptance", False, "TIMEOUT: 验收测试 600 秒未返回", "TIMEOUT")
    if "no tests ran" in (proc.stdout + proc.stderr):
        return row("shipped_acceptance", False, "NOT_RUN: pytest 不可用或没收到用例", "NOT_RUN")
    return row("shipped_acceptance", proc.returncode == 0,
               (proc.stdout + proc.stderr)[-300:], "PASS" if proc.returncode == 0 else "FAIL")


def check_package_hygiene(root: Path, manifest_paths: list[str] | None) -> list[dict]:
    """脱敏类检查：只在「这是发布包」的口径下判，开发树本来就有 .env。"""
    rows = []
    members = manifest_paths if manifest_paths is not None else [
        p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()]
    leaked = [m for m in members if FORBIDDEN_MEMBERS.search(m)]
    rows.append(row("no_private_or_build_files", not leaked,
                    f"清单/树内命中禁止件 {len(leaked)}：{leaked[:4]}" if leaked
                    else f"检查 {len(members)} 个成员，无 .env、无 .git、无构建产物", ))
    hits_priv, hits_cred, scanned = [], [], 0
    for rel in members:
        if rel in leaked:
            continue
        path = root / rel
        if path.suffix.lower() not in TEXT_SUFFIXES and path.name not in {"LICENSE", "VERSION"}:
            continue
        if not path.is_file():
            continue
        scanned += 1
        text = path.read_text(encoding="utf-8", errors="replace")
        for rule in PRIVATE_PATHS:
            if rule.search(text):
                hits_priv.append(f"{rel}:{text[:rule.search(text).start()].count(chr(10)) + 1}")
                break
        for name, rule in CREDENTIAL_SHAPES:
            if rule.search(text):
                hits_cred.append(f"{name} {rel}")
    rows.append(row("no_private_paths", not hits_priv,
                    f"扫描 {scanned} 份文本，私人路径命中 {len(hits_priv)}：{hits_priv[:4]}"))
    rows.append(row("no_credential_shapes", not hits_cred,
                    f"凭据形状命中 {len(hits_cred)}：{hits_cred[:4]}（只记规则与路径，不记明文）"))
    return rows


CABINET_REF = re.compile(r"schemas/[\w.\-]+")
CABINET_REF_REGISTRY = Path("scripts/vendor/CABINET_ONLY_REFS.json")


def check_cabinet_only_refs(root: Path, members: list[str]) -> dict:
    """包内文档引用了「只有开发柜里才有」的路径，就必须逐条登记并说明。

    现场教训：SKILL.md 与 FAQ.md 让读者跑 `schemas/contract_check.py`、看
    `schemas/style-card.schema.json`，而独立安装的包根本没有柜根 —— 收件人照做必然失败，
    这属阻断使用而不是文档瑕疵。登记成清单后，新增未登记的柜根引用会直接判红。
    """
    reg_path = root / CABINET_REF_REGISTRY
    registered = {}
    if reg_path.is_file():
        doc = json.loads(reg_path.read_text(encoding="utf-8"))
        registered = {item["reference"]: item for item in doc.get("references", [])}
    member_set = set(members)
    found = {}
    for rel in members:
        path = root / rel
        if rel in ("SKILL.md",) or path.suffix.lower() not in TEXT_SUFFIXES or not path.is_file():
            if path.suffix.lower() not in TEXT_SUFFIXES and rel != "SKILL.md":
                continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for m in CABINET_REF.finditer(text):
            ref = m.group(0)
            if (root / ref).is_file() or f"scripts/{ref}" in member_set:
                continue                       # 包内真的有，不算悬空
            found.setdefault(ref, []).append(rel)
    unregistered = sorted(set(found) - set(registered))
    stale = sorted(set(registered) - set(found))
    detail = (f"悬空柜根引用 {len(found)} 项；未登记 {len(unregistered)}：{unregistered[:4]}；"
              f"登记已失效 {len(stale)}：{stale[:3]}")
    return row("cabinet_only_refs_declared", not unregistered and not stale, detail)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=str(ROOT))
    ap.add_argument("--as-package", action="store_true", help="按发布包口径追加脱敏检查")
    ap.add_argument("--manifest", type=Path, help="发布清单 manifest.json（给了就按包口径）")
    ap.add_argument("--json", type=Path)
    args = ap.parse_args()
    root = Path(args.root).resolve()

    rows = [check_python_compiles(root), check_imports(root), check_vendor_digests(root),
            check_identity(root), check_shipped_tests(root)]
    members = None
    if args.manifest and args.manifest.is_file():
        doc = json.loads(args.manifest.read_text(encoding="utf-8"))
        members = [f["path"] for f in doc.get("files", [])]
        rows.append(row("manifest_members_present",
                        all((root / m).is_file() for m in members),
                        f"清单 {len(members)} 项，树内缺 {sum(1 for m in members if not (root / m).is_file())} 项"))
    if args.as_package or members is not None:
        if members is None:
            members = [p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()]
        rows += check_package_hygiene(root, members)
        rows.append(check_cabinet_only_refs(root, members))
    else:
        rows.append(row("package_hygiene", True,
                        "SKIPPED-BY-DESIGN: 开发树本来含 .env；在解压包内或用 --manifest 复跑才会判这一类",
                        "DECLARED"))

    failed = [r["name"] for r in rows if not r["passed"]]
    report = {"root": str(root), "mode": "package" if (args.as_package or members) else "dev-tree",
              "total": len(rows), "passed": len(rows) - len(failed), "failed": failed, "rows": rows}
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(report, ensure_ascii=False, indent=1) + "\n",
                             encoding="utf-8", newline="\n")
    for r in rows:
        print(f"[{r['status']:>7}] {r['name']}: {r['detail'][:110]}")
    print(f"Result: {report['passed']}/{report['total']} checks passed")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
