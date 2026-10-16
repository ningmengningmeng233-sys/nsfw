import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

# --- B1 编码范式：子进程 Python 的编码由这里钉死，测试文件一律走 run_py 出口 ---
# 与 B站/抖音/去AI味 等仓同一套范式（治理仓 G10 认这个形状）。
_PYTHON_ENCODING_FLAGS = {
    "PYTHONUTF8": "1",
    "PYTHONIOENCODING": "utf-8",
    "PYTHONLEGACYWINDOWSSTDIO": "0",
}


def clean_env(**extra):
    """以 os.environ 为底，强制钉死 Python 子进程编码 flag（调用方连 extra 都覆盖不了）。"""
    env = dict(os.environ)
    env.update(_PYTHON_ENCODING_FLAGS)
    env.update(extra)
    env.update(_PYTHON_ENCODING_FLAGS)
    return env


def run_py(args, cwd=None, **kw):
    """跑 Python 子进程的唯一出口：bytes 捕获后统一 utf-8 解码，不受 GBK 控制台影响。

    内部禁止 text=True / encoding=：调用方传了也会被剥掉，避免两套解码口径。
    """
    env = clean_env(**(kw.pop("env", None) or {}))
    kw.pop("text", None)
    kw.pop("encoding", None)
    kw.pop("universal_newlines", None)
    kw.pop("capture_output", None)
    res = subprocess.run([sys.executable, *args], cwd=cwd, env=env,
                         capture_output=True, **kw)
    res.stdout = (res.stdout or b"").decode("utf-8", "replace")
    res.stderr = (res.stderr or b"").decode("utf-8", "replace")
    return res
# --- B1 end ---

# --- B6 诊断开关：仅 LEMON_DIAG=1 时启用 faulthandler 线程栈转储（常规跑不生效） ---
if os.environ.get("LEMON_DIAG") == "1":
    import faulthandler

    faulthandler.enable()
    faulthandler.dump_traceback_later(60, exit=False)  # 60s 后把所有线程栈打到 stderr
# --- LEMON_DIAG end ---


@pytest.fixture(autouse=True)
def _resource_convergence(monkeypatch):
    """B6 收口 fixture（autouse）：把「跑完不退出」的静默挂死变成显式红。

    1) 关闭本测试期间打开但仍未关闭的 SQLite 连接；
    2) 对仍在运行的非 daemon 线程 join(timeout=5)；超时未收敛则 pytest.fail，
       防止解释器退出时在 threading._shutdown 处永久阻塞（CI 只能靠 timeout 掐死、退出码失真）。
    """
    opened_conns = []
    _real_connect = sqlite3.connect

    def _tracked_connect(*args, **kwargs):
        conn = _real_connect(*args, **kwargs)
        opened_conns.append(conn)
        return conn

    monkeypatch.setattr(sqlite3, "connect", _tracked_connect)
    yield
    for conn in opened_conns:
        try:
            conn.close()
        except Exception:
            pass  # 已关闭/已失效的连接不影响收口判定
    stragglers = [
        t
        for t in threading.enumerate()
        if t is not threading.main_thread()
        and not getattr(t, "daemon", True)
        and t.is_alive()
    ]
    for t in stragglers:
        t.join(timeout=5)
    leaked = [t.name for t in stragglers if t.is_alive()]
    if leaked:
        names = ", ".join(leaked)
        pytest.fail(f"{len(leaked)}级:线程未收敛: {names}")


# --- B6 兜底退出：绕过 CPython 收尾时 join 非 daemon 线程的死等 ---
_b6_exitstatus = {"code": None}


def pytest_sessionfinish(session, exitstatus):
    _b6_exitstatus["code"] = int(exitstatus)


def pytest_unconfigure(config):
    """最后手段：仍有非 daemon 线程存活时，Python 解释器会在 threading._shutdown
    处永久阻塞（即 B6 的「跑完不退出、靠 timeout 掐死」）。此时 pytest 已完成全部
    统计与终端输出，退出码已按真实结果算好（失败=1、收集错=2/4），先 flush 双流再
    os._exit 携带该真实码退出——不洗白失败：sessionfinish 未记录到码时保守按 1。"""
    stragglers = [
        t
        for t in threading.enumerate()
        if t is not threading.main_thread()
        and not getattr(t, "daemon", True)
        and t.is_alive()
    ]
    if not stragglers:
        return
    code = _b6_exitstatus["code"]
    if code is None:
        code = 1
    try:
        sys.stdout.flush()
        sys.stderr.flush()
    except Exception:
        pass
    os._exit(code)  # noqa: B009 —— 绕过常驻非 daemon 线程，仅在有悬挂线程时到达


@pytest.fixture
def tmp_lib():
    """Yield a temporary index library directory."""
    path = tempfile.mkdtemp(prefix="novel-index-test-")
    for d in ("小说原文", "小说索引", "章节索引"):
        os.makedirs(os.path.join(path, d), exist_ok=True)
    yield path
    shutil.rmtree(path, ignore_errors=True)


@pytest.fixture
def sample_segment(tmp_lib):
    """Create a minimal segment file and return its path."""
    novel_author = "test-author"
    novel_dir = os.path.join(tmp_lib, "小说原文", novel_author)
    os.makedirs(novel_dir, exist_ok=True)
    path = os.path.join(novel_dir, f"{novel_author}-第1章~第2章-001.md")
    content = (
        "---\n"
        "小说名称: 《test》\n"
        "作者: author\n"
        "章节范围: 第1章~第2章\n"
        f"对应索引: {novel_author}-第1章~第2章-001-idx.md\n"
        "分段方式: 按章节切分\n"
        "---\n"
        "第1章 开篇\n第一行\n第二行\n"
        "第2章 继续\n第三行\n第四行\n"
    )
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    return path


@pytest.fixture
def sample_index(tmp_lib, sample_segment):
    """Create a minimal chapter index file and return its path."""
    novel_author = "test-author"
    idx_dir = os.path.join(tmp_lib, "章节索引")
    path = os.path.join(idx_dir, f"{novel_author}-第1章~第2章-001-idx.md")
    content = (
        "---\n"
        "小说名称: 《test》\n"
        "作者: author\n"
        "索引类型: 章节索引\n"
        "索引序号: '001'\n"
        "章节范围: 第1章~第2章\n"
        "总章节数: '2'\n"
        "是否完结: 连载中\n"
        "包含元素:\n  - 打脸\n"
        f"源文件: 小说原文/{novel_author}/{novel_author}-第1章~第2章-001.md\n"
        "创建日期: 2026-08-18\n"
        "最后更新: 2026-08-18\n"
        "状态: 完成\n"
        "---\n"
        "## 第1章 开篇（行号：1 ~ 2）\n"
        "### 剧情概要\n剧情\n"
        "### 金句\n- \"第一行\"（行1，开篇）\n"
        "## 第2章 继续（行号：3 ~ 4）\n"
        "### 剧情概要\n剧情\n"
    )
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    return path
