# -*- coding: utf-8 -*-
"""IDX-07 门禁适配器：将 scripts/_功能测试.py 的数据级回归用例纳入 pytest。

背景：核心拆段/断点/行号校准/直调引擎回归长期只活在 ``_功能测试.py`` 的
自研 ``@test`` 注册表里，pytest 默认只收 ``tests/test_*.py``，导致门禁假绿。
本适配器把注册表逐项展开为 pytest 参数化用例，定义顺序即执行顺序
（末项复用直调项的临时库，与独立运行语义一致）。

环境隔离：被适配模块在导入期会写入 INDEX_LIB_DIR/OPENAI_API_KEY/
SUMMARY_API_MODEL；适配器在导入后立即恢复现场，每个用例运行时再通过
fixture 重建该环境，用例结束还原，保证既不污染其它测试，也不改动
被适配模块的独立运行语义。
"""
from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

_FUNC_ENV_KEYS = ("INDEX_LIB_DIR", "OPENAI_API_KEY", "SUMMARY_API_MODEL")

_host_env = {key: os.environ.get(key) for key in _FUNC_ENV_KEYS}
_func = importlib.import_module("_功能测试")
for _key, _val in _host_env.items():
    if _val is None:
        os.environ.pop(_key, None)
    else:
        os.environ[_key] = _val

_CASE_IDS = [name for name, _fn in _func.TESTS]


@pytest.fixture
def _functional_env():
    saved = {key: os.environ.get(key) for key in _FUNC_ENV_KEYS}
    os.environ["INDEX_LIB_DIR"] = _func.LIB
    os.environ["OPENAI_API_KEY"] = "test-key"
    os.environ["SUMMARY_API_MODEL"] = "test-model"
    yield
    for key, val in saved.items():
        if val is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = val


@pytest.mark.parametrize("case_id", range(len(_CASE_IDS)), ids=_CASE_IDS)
def test_functional_case(case_id, _functional_env):
    _name, fn = _func.TESTS[case_id]
    fn()
