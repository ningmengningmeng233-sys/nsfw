# -*- coding: utf-8 -*-
"""功能性回归测试 v4.6（审查修复验证）

构造临时测试小说与临时索引库，全链路验证：
  1. 章节识别（章卷混合 / 纯卷级 / 英文无空格 / 中文数字）
  2. 拆段（标准 / 无章节字数切分 / GBK 编码 / 正文含 --- 不误判 / 多卷段号接续 / 追更 / 幂等冲突）
  3. 行号校准（金句中英文括号 / 区间引用 / 待核标记）
  4. 直调引擎集成（mock OpenAI 兼容 API：断点 / 校准自动执行 / 断点续传跳过）
  5. 小说索引 / 行号体检 / 结构验证批次

用法: python scripts/_功能测试.py
临时目录保留在系统 temp 下（novel_index_test_*），供失败排查。
"""
import os
import re
import shutil
import subprocess
import sys
import tempfile

if __name__ == "__main__":  # 只有 CLI 直跑才改；被导入时不得动宿主 stdio（会吞掉测试框架的汇总行）
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

TESTS = []
PASS = FAIL = 0


def test(name):
    def deco(fn):
        TESTS.append((name, fn))
        return fn
    return deco


def run_all():
    global PASS, FAIL
    print("=" * 60)
    print("成人文学索引 v4.8.3 功能性回归测试")
    print("=" * 60)
    for name, fn in TESTS:
        try:
            fn()
            PASS += 1
            print(f"  ✓ {name}")
        except AssertionError as e:
            FAIL += 1
            print(f"  ✗ {name}: 断言失败 → {e}")
        except Exception as e:
            FAIL += 1
            print(f"  ✗ {name}: 异常 → {type(e).__name__}: {e}")
    print("-" * 60)
    print(f"结果: {PASS} 通过 / {FAIL} 失败")
    sys.exit(1 if FAIL else 0)


# ---------------------------------------------------------------- 测试环境

TEST_ROOT = tempfile.mkdtemp(prefix="novel_index_test_")
LIB = os.path.join(TEST_ROOT, "索引库")
SCRIPTS = os.path.join(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SCRIPTS)

# 环境变量必须在 import 模块前设置（模块级常量读取 env）
os.environ["INDEX_LIB_DIR"] = LIB
os.environ["OPENAI_API_KEY"] = "test-key"
os.environ["SUMMARY_API_MODEL"] = "test-model"

# ---------------------------------------------------------------- 1. 章节识别

from chapter_patterns import (CHAPTER_RE, normalize_ch_name, cn2ar,
                              title_level, extract_chapters)
from split_segments import find_chapter_lines


@test("章节识别:章卷混合时卷标题被过滤")
def _():
    lines = ["第一卷 风起云涌", "第一章 废柴", "第二章 测试", "第三章 退婚"]
    chs = find_chapter_lines(lines)
    assert len(chs) == 3, chs
    assert chs[0][2] == "第1章" and all(n.startswith("第") for _, _, n in chs), chs
    assert "第一卷" not in [t for _, t, _ in chs]
    # extract_chapters 同口径（LLM 章节列表）
    ts = extract_chapters("\n".join(lines))
    assert ts == ["第一章 废柴", "第二章 测试", "第三章 退婚"], ts


@test("章节识别:纯卷级章节卷即章单位")
def _():
    lines = ["第一卷 起始", "第二卷 发展", "第三卷 高潮"]
    chs = find_chapter_lines(lines)
    assert len(chs) == 3, chs
    assert [n for _, _, n in chs] == ["第1卷", "第2卷", "第3卷"], chs
    assert title_level("第一卷 起始") == "volume"
    assert title_level("第一章 废柴") == "chapter"
    assert title_level("随便一行") is None


@test("章节识别:英文无空格 Chapter2 与中文数字")
def _():
    assert bool(CHAPTER_RE.match("Chapter2 转折"))
    assert bool(CHAPTER_RE.match("第一百二十三章 大结局"))
    assert normalize_ch_name("Chapter2 转折") == "第2章"
    assert normalize_ch_name("第一百二十三章") == "第123章"
    assert cn2ar("一百零三") == 103 and cn2ar("三十五") == 35 and cn2ar("十一") == 11


@test("章节识别:无编号词误判正文行(P1-1 回归)")
def _():
    # 正文行以序章/番外等开头但不跟空白/标点 → 不得识别为章节标题
    assert not bool(CHAPTER_RE.match("番外内容,继续写正文"))
    assert not bool(CHAPTER_RE.match("序章写完,主角登场"))
    assert not bool(CHAPTER_RE.match("尾声临近,气氛紧张"))
    # 真标题(词后跟空白/标点/行尾)仍识别
    assert bool(CHAPTER_RE.match("番外 标题"))
    assert bool(CHAPTER_RE.match("番外：少年往事"))
    assert bool(CHAPTER_RE.match("序章"))
    assert bool(CHAPTER_RE.match("后记。"))
    # find_chapter_lines 过滤:正文混入「番外内容」不产生额外章节
    chs = find_chapter_lines(["第一章 开局", "正文行一", "番外内容,这不是标题", "第二章 继续"])
    assert len(chs) == 2, chs


@test("章节识别:BOM 与裸阿拉伯章节标题")
def _():
    import _common
    root = tempfile.mkdtemp(prefix="bom_")
    path = os.path.join(root, "bom.txt")
    with open(path, "w", encoding="utf-8-sig") as f:
        f.write("第一章\n正文一\n第二章\n正文二")
    text = _common.read_text_auto(path)
    chs = find_chapter_lines(text.splitlines())
    assert [x[2] for x in chs] == ["第1章", "第2章"], chs
    shutil.rmtree(root, ignore_errors=True)


@test("章节识别:混合格式章号(P1-2 回归)")
def _():
    # 中文+阿拉伯混合:此前静默忽略阿拉伯数字(一千零821→1000)
    assert cn2ar("一千零821") == 1821
    assert cn2ar("第1234") == 1234
    assert normalize_ch_name("第一千零821章 决战") == "第1821章"
    # 纯中文/纯阿拉伯不受影响
    assert cn2ar("第一千零二十一章") == 1021
    assert cn2ar("1001") == 1001


@test("断点库:mark_done UPSERT 兜底(P2 回归)")
def _():
    import tempfile as _tf
    from index_db import IndexDB
    root = _tf.mkdtemp(prefix="idxdb_upsert_")
    lib = os.path.join(root, "lib")
    os.makedirs(lib, exist_ok=True)
    seg = os.path.join(lib, "小说原文", "某书-某作者", "某书-某作者-001.md")
    os.makedirs(os.path.dirname(seg), exist_ok=True)
    open(seg, "w", encoding="utf-8").write("正文")
    out = os.path.join(lib, "章节索引", "某书-某作者-001-idx.md")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    open(out, "w", encoding="utf-8").write("索引内容")
    with IndexDB(lib) as db:
        # 无 mark_pending 先行,直接 mark_done → 行应存在且 should_skip 生效
        db.mark_done(seg, out, 10, 20)
        assert db.get(seg) is not None, "mark_done 后行应存在(UPSERT 兜底)"
        assert db.should_skip(seg, out) is True, "done+文件存在+hash一致 → 应跳过"
        assert db.stats()[0].get("done") == 1, "done 状态统计"
    import shutil
    shutil.rmtree(root, ignore_errors=True)


@test("章节识别:千/万级中文数字(审查 Bug1 回归)")
def _():
    # 正则字符类必须识别千级标题(此前漏识别→千章后正文全并入末段)
    assert bool(CHAPTER_RE.match("第一千二百三十章 巅峰"))
    assert bool(CHAPTER_RE.match("第一万零五章 决战"))
    # cn2ar 计算(此前 1230→230、1001→1、12000→2)
    assert cn2ar("一千二百三十") == 1230
    assert cn2ar("一千零一") == 1001
    assert cn2ar("一万零五") == 10005
    assert cn2ar("三万二千") == 32000
    assert cn2ar("十万") == 100000
    # 规范化命名(千章小说的段文件名/导航/最后索引章节依赖它)
    assert normalize_ch_name("第一千二百三十章 巅峰") == "第1230章"
    assert normalize_ch_name("第一万零五章 决战") == "第10005章"


@test("章节识别:卷·章前缀标题(第1卷·第3章)为章级")
def _():
    t = "第1卷·第3章 标题3"
    assert title_level(t) == "chapter", "卷前缀+章级单位应为章级"
    assert normalize_ch_name(t) == "第3章", "章名应取章级单位而非卷级"
    # 卷前缀+无编号词:第一卷 序章 → 序章级(既有命名)
    assert normalize_ch_name("第一卷 序章") == "第序章"
    assert normalize_ch_name("第一卷 风起云涌") == "第1卷"
    chs = find_chapter_lines(["第1卷·第1章 开局", "第1卷·第2章 发展", "第二卷·第1章 转场"])
    assert [n for _, _, n in chs] == ["第1章", "第2章", "第1章"], chs


# ---------------------------------------------------------------- 2. 拆段

def make_std_body(chapters, lines_per=8, prefix=""):
    out = []
    for i in range(1, chapters + 1):
        out.append(f"{prefix}第{i}章 标题{i}")
        for j in range(lines_per):
            out.append(f"情节内容行,用于填充测试文字,编号{i}-{j}。")
    return "\n".join(out)


def run_script(module, argv):
    old = sys.argv
    sys.argv = argv
    try:
        return module.main()
    finally:
        sys.argv = old


@test("拆段:12 章标准切分 5/5/2 且段范围正确")
def _():
    lib = os.path.join(TEST_ROOT, "t_std")
    src = os.path.join(TEST_ROOT, "std.txt")
    with open(src, "w", encoding="utf-8") as f:
        f.write(make_std_body(12))
    import split_segments
    rc = run_script(split_segments, ["split_segments.py", src, "--novel", "标准测试", "--author", "作者A", "--lib", lib])
    assert rc == 0, rc
    seg_dir = os.path.join(lib, "小说原文", "标准测试-作者A")
    names = sorted(os.listdir(seg_dir), key=lambda f: int(re.search(r"-(\d{3})\.md$", f).group(1)))
    assert len(names) == 3, names
    assert names[0].endswith("001.md") and names[2].endswith("003.md"), names
    t1 = open(os.path.join(seg_dir, names[0]), encoding="utf-8").read()
    t3 = open(os.path.join(seg_dir, names[2]), encoding="utf-8").read()
    assert "章节范围: 第1章~第5章" in t1, t1
    assert "章节范围: 第11章~第12章" in t3, t3
    # 幂等:重跑 → 全部 skipped,rc=0
    rc = run_script(split_segments, ["split_segments.py", src, "--novel", "标准测试", "--author", "作者A", "--lib", lib])
    assert rc == 0
    # 同名内容变化(某章正文被改,章节结构不变)→ conflicted 报错退出
    with open(src, "r", encoding="utf-8") as f:
        orig = f.read()
    with open(src, "w", encoding="utf-8") as f:
        f.write(orig.replace("编号1-0。", "编号1-0-改。"))
    rc = run_script(split_segments, ["split_segments.py", src, "--novel", "标准测试", "--author", "作者A", "--lib", lib])
    assert rc == 1, "同名内容变更应冲突报错"
    # --force 覆盖后恢复
    rc = run_script(split_segments, ["split_segments.py", src, "--novel", "标准测试", "--author", "作者A", "--lib", lib, "--force"])
    assert rc == 0


@test("拆段:章卷混合小说不再错段")
def _():
    lib = os.path.join(TEST_ROOT, "t_mix")
    src = os.path.join(TEST_ROOT, "mix.txt")
    body = ["第一卷 风起云涌"] + make_std_body(11).splitlines()
    with open(src, "w", encoding="utf-8") as f:
        f.write("\n".join(body))
    import split_segments
    rc = run_script(split_segments, ["split_segments.py", src, "--novel", "混合测试", "--author", "作者B", "--lib", lib])
    assert rc == 0
    seg_dir = os.path.join(lib, "小说原文", "混合测试-作者B")
    names = sorted(os.listdir(seg_dir), key=lambda f: int(re.search(r"-(\d{3})\.md$", f).group(1)))
    assert len(names) == 3, names  # 11 章 → 5/5/1
    t1 = open(os.path.join(seg_dir, names[0]), encoding="utf-8").read()
    assert "章节范围: 第1章~第5章" in t1, "卷标题不应混入段范围: " + t1[:200]


@test("拆段:纯卷级章节命名第X卷")
def _():
    lib = os.path.join(TEST_ROOT, "t_volonly")
    src = os.path.join(TEST_ROOT, "volonly.txt")
    body = "\n".join(f"第{i}卷 卷标{i}" + "\n" + "卷正文行,填充内容。" * 5 for i in range(1, 7))
    with open(src, "w", encoding="utf-8") as f:
        f.write(body)
    import split_segments
    rc = run_script(split_segments, ["split_segments.py", src, "--novel", "纯卷测试", "--author", "作者C", "--lib", lib])
    assert rc == 0
    seg_dir = os.path.join(lib, "小说原文", "纯卷测试-作者C")
    t1 = open(os.path.join(seg_dir, "纯卷测试-作者C-第1卷~第5卷-001.md"), encoding="utf-8").read()
    assert "章节范围: 第1卷~第5卷" in t1, t1[:200]


@test("拆段:无章节按字数切分 + 字符守恒")
def _():
    lib = os.path.join(TEST_ROOT, "t_chars")
    src = os.path.join(TEST_ROOT, "noch.txt")
    paras = []
    for i in range(30):
        paras.append("段落" + str(i) + "。" + "填充文字。内容填充。测试句子。" * 30)
    with open(src, "w", encoding="utf-8") as f:
        f.write("\n\n".join(paras))
    import split_segments
    rc = run_script(split_segments, ["split_segments.py", src, "--novel", "无章测试", "--author", "作者D", "--lib", lib, "--chars-per-seg", "2000"])
    assert rc == 0
    seg_dir = os.path.join(lib, "小说原文", "无章测试-作者D")
    names = sorted(os.listdir(seg_dir))
    assert len(names) >= 3, names
    # 字符守恒:段文件拼接(去 frontmatter) ≈ 源文件
    total = 0
    for n in names:
        t = open(os.path.join(seg_dir, n), encoding="utf-8").read()
        body = t.split("---\n", 2)[2]
        total += len(body.replace("\n\n", ""))
    src_chars = sum(len(p) for p in paras)
    assert total == src_chars, f"{total} vs {src_chars}"


@test("拆段:GBK 编码源文件")
def _():
    lib = os.path.join(TEST_ROOT, "t_gbk")
    src = os.path.join(TEST_ROOT, "gbk.txt")
    with open(src, "w", encoding="gbk") as f:
        f.write(make_std_body(6))
    import split_segments
    rc = run_script(split_segments, ["split_segments.py", src, "--novel", "GBK测试", "--author", "作者E", "--lib", lib])
    assert rc == 0, "GBK 源文件应通过编码回退链正常读取"


@test("拆段:md 源文件正文含 --- 行不误判")
def _():
    lib = os.path.join(TEST_ROOT, "t_fm")
    src = os.path.join(TEST_ROOT, "fm.md")
    body = "---\ntitle: 导出的书\n---\n第一章 开始\n第一段正文。\n\n---\n第二章 继续\n第二段正文。"
    with open(src, "w", encoding="utf-8") as f:
        f.write(body)
    import split_segments
    rc = run_script(split_segments, ["split_segments.py", src, "--novel", "FM测试", "--author", "作者F", "--lib", lib])
    assert rc == 0
    seg_dir = os.path.join(lib, "小说原文", "FM测试-作者F")
    t1 = open(os.path.join(seg_dir, "FM测试-作者F-第1章~第2章-001.md"), encoding="utf-8").read()
    # 正文(含 --- 行)应完整保留在段文件里
    assert "---\n第二章 继续" in t1, "正文含 --- 行被误吞:\n" + t1


@test("拆段:多卷同卷重跑不重复段号")
def _():
    lib = os.path.join(TEST_ROOT, "t_vol_rerun")
    import split_segments
    for volume in ("第1卷", "第2卷"):
        src = os.path.join(TEST_ROOT, volume + "_rerun.txt")
        with open(src, "w", encoding="utf-8") as f:
            f.write(make_std_body(10, prefix=volume + "·"))
        assert run_script(split_segments, ["split_segments.py", src, "--novel", "重跑测试", "--author", "作者R", "--lib", lib, "--volume", volume]) == 0
    src2 = os.path.join(TEST_ROOT, "第2卷_rerun.txt")
    assert run_script(split_segments, ["split_segments.py", src2, "--novel", "重跑测试", "--author", "作者R", "--lib", lib, "--volume", "第2卷", "--force"]) == 0
    seg_dir = os.path.join(lib, "小说原文", "重跑测试-作者R")
    names = sorted(os.listdir(seg_dir))
    assert len(names) == 4, names
    assert len({re.search(r"-(\d{3})\.md$", n).group(1) for n in names}) == 4, names


@test("拆段:多卷多文件段号跨卷接续")
def _():
    lib = os.path.join(TEST_ROOT, "t_vol")
    import split_segments
    # 第1卷:10 章 → 001-002
    src1 = os.path.join(TEST_ROOT, "vol1.txt")
    with open(src1, "w", encoding="utf-8") as f:
        f.write(make_std_body(10, prefix="第1卷·"))
    rc = run_script(split_segments, ["split_segments.py", src1, "--novel", "分卷测试", "--author", "作者G",
                                     "--lib", lib, "--volume", "第1卷"])
    assert rc == 0
    # 第2卷:10 章 → 003-004（接续而非从 001 重来）
    src2 = os.path.join(TEST_ROOT, "vol2.txt")
    with open(src2, "w", encoding="utf-8") as f:
        f.write(make_std_body(10, prefix="第2卷·"))
    rc = run_script(split_segments, ["split_segments.py", src2, "--novel", "分卷测试", "--author", "作者G",
                                     "--lib", lib, "--volume", "第2卷"])
    assert rc == 0
    seg_dir = os.path.join(lib, "小说原文", "分卷测试-作者G")
    names = sorted(os.listdir(seg_dir))
    assert len(names) == 4, names
    assert names[0].startswith("分卷测试-作者G-第1卷"), names
    assert names[2].startswith("分卷测试-作者G-第2卷"), names
    nos = [int(re.search(r"-(\d{3})\.md$", n).group(1)) for n in names]
    assert nos == [1, 2, 3, 4], f"跨卷段号应全局唯一递增: {nos}"


@test("拆段:追更 --append 并入末段/新建段")
def _():
    lib = os.path.join(TEST_ROOT, "t_append")
    src = os.path.join(TEST_ROOT, "app.txt")
    with open(src, "w", encoding="utf-8") as f:
        f.write(make_std_body(12))
    import split_segments
    rc = run_script(split_segments, ["split_segments.py", src, "--novel", "追更测试", "--author", "作者H", "--lib", lib])
    assert rc == 0
    seg_dir = os.path.join(lib, "小说原文", "追更测试-作者H")
    # 新增 3 章(13-15) → 并入末段 003
    extra1 = "\n".join(f"第{i}章 续写{i}\n续写内容行,填充。" for i in range(13, 16))
    with open(src, "a", encoding="utf-8") as f:
        f.write("\n" + extra1)
    rc = run_script(split_segments, ["split_segments.py", src, "--novel", "追更测试", "--author", "作者H", "--lib", lib, "--append"])
    assert rc == 0, rc
    names = sorted(os.listdir(seg_dir))
    assert len(names) == 3, "并入末段,文件数不变: " + str(names)
    t3 = open(os.path.join(seg_dir, "追更测试-作者H-第11章~第12章-003.md"), encoding="utf-8").read()
    assert "章节范围: 第11章~第15章" in t3, "并入后章节范围应更新: " + t3[:150]
    # 再新增 6 章(16-21) → 新建 004(第16~20章)、005(第21章)
    more = "\n".join(f"第{i}章 新增{i}\n新章内容行。" for i in range(16, 22))
    with open(src, "a", encoding="utf-8") as f:
        f.write("\n" + more)
    rc = run_script(split_segments, ["split_segments.py", src, "--novel", "追更测试", "--author", "作者H", "--lib", lib, "--append"])
    assert rc == 0, rc
    names = sorted(os.listdir(seg_dir))
    assert len(names) == 5, names
    assert any("004" in n for n in names) and any("005" in n for n in names), names


# ---------------------------------------------------------------- 3. 行号校准

@test("行号校准:金句中英文括号修正 + 区间修正 + 不污染")
def _():
    lib = os.path.join(TEST_ROOT, "t_calib")
    seg_dir = os.path.join(lib, "小说原文", "校准测试-作者I")
    os.makedirs(seg_dir, exist_ok=True)
    seg = os.path.join(seg_dir, "校准测试-作者I-第1章~第5章-001.md")
    seg_body = ("---\n小说名称: 《校准测试》\n作者: 作者I\n章节范围: 第1章~第5章\n"
                "对应索引: 校准测试-作者I-第1章~第5章-001-idx.md\n分段方式: 按章节切分\n---\n"
                "第一章 开局\n第一段正文内容足够长。\n这是一个测试金句,用来验证行号校准。\n第二章 发展\n正文二。\n第三章 中局")
    with open(seg, "w", encoding="utf-8") as f:
        f.write(seg_body)
    os.makedirs(os.path.join(lib, "章节索引"), exist_ok=True)
    idx = os.path.join(lib, "章节索引", "校准测试-作者I-第1章~第5章-001-idx.md")
    idx_body = (
        "---\n小说名称: 《校准测试》\n作者: 作者I\n索引类型: 章节索引\n索引序号: '001'\n"
        "章节范围: 第1章~第5章\n总章节数: 5\n是否完结: 连载中\n包含元素:\n  - 装逼打脸 | 打脸\n"
        "源文件: 小说原文/校准测试-作者I/校准测试-作者I-第1章~第5章-001.md\n"
        "创建日期: 2026-08-16\n最后更新: 2026-08-16\n状态: 完成\n分段方式: 按章节切分\n---\n\n"
        "# 《校准测试》章节索引 001\n\n"
        "## 第1章 开局（行号：8 ~ 11）\n\n### 金句/名场面\n\n"
        "- \"这是一个测试金句,用来验证行号校准。\"（行999，中文括号错误行号）\n"
        "- \"第一段正文内容足够长。\"(行3，英文括号错误行号)\n\n### 剧情概要\n\n"
        "测试金句附近的转述（行1，区间引用错误行号）\n"
        "第一段正文内容足够长（行1~3，区间引用）\n"
    )
    with open(idx, "w", encoding="utf-8") as f:
        f.write(idx_body)
    import verify_and_fix_lines as v
    rc = run_script(v, ["verify_and_fix_lines.py", idx, "--lib", lib])
    assert rc == 0
    fixed = open(idx, encoding="utf-8").read()
    assert "（行10，中文括号错误行号）" in fixed, "中文括号金句应修正为真实行号 10:\n" + fixed
    assert "（行9，英文括号错误行号）" in fixed, "英文括号金句应修正为真实行号 9:\n" + fixed
    assert "（行1，区间引用错误行号）" in fixed, "未命中区间引用应保持原样不污染:\n" + fixed
    assert "（行9~11，区间引用）" in fixed, "区间引用唯一命中应修正保持长度:\n" + fixed


# ---------------------------------------------------------------- 4. 直调引擎集成（mock API）

import http.server
import json
import threading

# mock 索引内容:行号故意错,验证自动校准;frontmatter 规范,验证后续处理
MOCK_IDX = (
    "---\n小说名称: 《直调测试》\n作者: 作者J\n索引类型: 章节索引\n索引序号: '001'\n"
    "章节范围: 第1章~第5章\n总章节数: 5\n是否完结: 连载中\n包含元素:\n  - 装逼打脸 | 打脸\n"
    "源文件: 小说原文/直调测试-作者J/直调测试-作者J-第1章~第5章-001.md\n"
    "创建日期: 2026-08-16\n最后更新: 2026-08-16\n状态: 完成\n分段方式: 按章节切分\n---\n\n"
    "# 《直调测试》章节索引 001\n\n"
    "## 第1章 开局（行号：8 ~ 11）\n\n### 金句/名场面\n\n"
    "- \"这是一个测试金句,用来验证行号校准。\"（行999，mock 错误行号）\n\n"
    "### 剧情概要\n\n直调引擎集成测试内容。\n\n"
    "## 第2章 发展（行号：12 ~ 13）\n\n### 剧情概要\n\n第二章内容。\n\n"
    "## 第3章 中局（行号：14 ~ 15）\n\n### 剧情概要\n\n第三章内容。\n\n"
    "## 第4章 转折（行号：16 ~ 17）\n\n### 剧情概要\n\n第四章内容。\n\n"
    "## 第5章 收束（行号：18 ~ 19）\n\n### 剧情概要\n\n第五章内容。\n"
)


class _MockHandler(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length", 0)))
        payload = {"id": "mock", "choices": [{"delta": {"content": MOCK_IDX}}], "usage": {"prompt_tokens": 100, "completion_tokens": 50}}
        body = f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
        body += "data: {\"choices\":[{\"delta\":{}}],\"usage\":{\"prompt_tokens\":100,\"completion_tokens\":50}}\n\n"
        body += "data: [DONE]\n\n"
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        self.wfile.write(body.encode("utf-8"))

    def log_message(self, *a):
        pass


def start_mock_server():
    srv = http.server.HTTPServer(("127.0.0.1", 0), _MockHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


@test("直调引擎:mock API 全流程 + 自动校准 + 断点续传")
def _():
    srv = start_mock_server()
    try:
        os.environ["SUMMARY_API_URL"] = f"http://127.0.0.1:{srv.server_port}/v1/chat/completions"
        lib = os.path.join(TEST_ROOT, "t_direct")
        seg_dir = os.path.join(lib, "小说原文", "直调测试-作者J")
        os.makedirs(seg_dir, exist_ok=True)
        seg = os.path.join(seg_dir, "直调测试-作者J-第1章~第5章-001.md")
        with open(seg, "w", encoding="utf-8") as f:
            f.write("---\n小说名称: 《直调测试》\n作者: 作者J\n章节范围: 第1章~第5章\n"
                    "对应索引: 直调测试-作者J-第1章~第5章-001-idx.md\n分段方式: 按章节切分\n---\n"
                    "第一章 开局\n第一段正文。\n这是一个测试金句,用来验证行号校准。\n第二章 发展\n正文二。\n第三章 中局\n正文三。\n第四章 转折\n正文四。\n第五章 收束\n正文五。")
        import build_segment_index as b
        rc = run_script(b, ["build_segment_index.py", seg, "--lib", lib])
        assert rc == 0, rc
        out = os.path.join(lib, "章节索引", "直调测试-作者J-第1章~第5章-001-idx.md")
        assert os.path.exists(out), "索引文件应生成"
        content = open(out, encoding="utf-8").read()
        assert "（行10，mock 错误行号）" in content, "直调后自动校准应修正金句行号 999→10:\n" + content
        # 断点:重跑 → 跳过
        rc = run_script(b, ["build_segment_index.py", seg, "--lib", lib])
        assert rc == 0
        # 断点库状态
        from index_db import IndexDB
        with IndexDB(lib) as db:
            row = db.get(seg)
            assert row[2] == "done", row
    finally:
        srv.shutdown()
        os.environ.pop("SUMMARY_API_URL", None)


@test("小说索引:导航/标签聚合/最后索引章节")
def _():
    lib = os.path.join(TEST_ROOT, "t_novelidx")
    idx_dir = os.path.join(lib, "章节索引")
    os.makedirs(idx_dir, exist_ok=True)
    for no, rng in [(1, "第1章~第5章"), (2, "第6章~第10章")]:
        with open(os.path.join(idx_dir, f"导航测试-作者K-{rng}-{no:03d}-idx.md"), "w", encoding="utf-8") as f:
            f.write(f"---\n小说名称: 《导航测试》\n作者: 作者K\n索引类型: 章节索引\n索引序号: '{no:03d}'\n"
                    f"章节范围: {rng}\n总章节数: 5\n是否完结: 连载中\n包含元素:\n  - 装逼打脸 | 打脸\n"
                    f"源文件: 小说原文/导航测试-作者K/导航测试-作者K-{rng}-{no:03d}.md\n"
                    f"创建日期: 2026-08-16\n最后更新: 2026-08-16\n状态: 完成\n---\n")
    import build_novel_index as b
    rc = run_script(b, ["build_novel_index.py", "--novel", "导航测试-作者K", "--lib", lib])
    assert rc == 0, rc
    out = os.path.join(lib, "小说索引", "导航测试-作者K.md")
    content = open(out, encoding="utf-8").read()
    assert "| 001 | 第1章~第5章 |" in content and "| 002 | 第6章~第10章 |" in content, content
    assert "最后索引章节: 第10章" in content, content
    assert "装逼打脸 | 打脸" in content, content
    assert "总章节数: 10" in content, content


@test("小说索引:--force 刷新保留已补写的概况/亮点(Q12)")
def _():
    lib = os.path.join(TEST_ROOT, "t_force")
    idx_dir = os.path.join(lib, "章节索引")
    os.makedirs(idx_dir, exist_ok=True)
    def write_idx(no, rng, chs):
        with open(os.path.join(idx_dir, f"保留测试-作者M-{rng}-{no:03d}-idx.md"), "w", encoding="utf-8") as f:
            f.write(f"---\n小说名称: 《保留测试》\n作者: 作者M\n索引类型: 章节索引\n索引序号: '{no:03d}'\n"
                    f"章节范围: {rng}\n总章节数: {chs}\n是否完结: 连载中\n包含元素:\n  - 金句 | 名台词\n"
                    f"源文件: 小说原文/保留测试-作者M/保留测试-作者M-{rng}-{no:03d}.md\n"
                    f"创建日期: 2026-08-16\n最后更新: 2026-08-16\n状态: 完成\n---\n")
    write_idx(1, "第1章~第5章", 5)
    write_idx(2, "第6章~第10章", 5)
    import build_novel_index as b
    out = os.path.join(lib, "小说索引", "保留测试-作者M.md")
    # 首次生成
    assert run_script(b, ["b.py", "--novel", "保留测试-作者M", "--lib", lib]) == 0
    # 模拟编排器补写概况/亮点(替换占位符);主线补写内容含「XXX」字样——
    # P3-6 回归:extract_section 占位判定不得把含 XXX 的补写内容误判为未补写
    t = open(out, encoding="utf-8").read()
    t = t.replace("- 主角：（编排器补写）", "- 主角：主角甲（补写内容）")
    t = t.replace("- 主线：（编排器补写）", "- 主线：XXX 组织暗中布局（补写内容）")
    t = t.replace("- （编排器补写）", "- 第1章退婚名场面（补写内容）")
    open(out, "w", encoding="utf-8").write(t)
    # 追更:新增第三段(2 章)→ --force 刷新
    write_idx(3, "第11章~第12章", 2)
    assert run_script(b, ["b.py", "--novel", "保留测试-作者M", "--lib", lib, "--force"]) == 0
    t = open(out, encoding="utf-8").read()
    assert "主角甲（补写内容）" in t and "XXX 组织暗中布局（补写内容）" in t, "概况补写内容应保留(含 XXX 字样):\n" + t
    assert "第1章退婚名场面（补写内容）" in t, "亮点补写内容应保留:\n" + t
    assert "章节数：12" in t, "动态行应刷新为新值:\n" + t
    assert "最后索引章节: 第12章" in t and "| 003 | 第11章~第12章 |" in t, "导航/最后索引章节应刷新:\n" + t
    assert "主角：（编排器补写）" not in t and "### 爽点密集区\n- （编排器补写）" not in t, "概况/亮点不应残留占位符:\n" + t
    # v4.5 审查新增:伏笔追踪表/人物关系链 占位存在 + force 保留
    assert "## 伏笔全局追踪表" in t and "## 人物关系链" in t, "聚合表小节应存在:\n" + t
    assert "伏笔全局追踪表" in t and "| 伏笔 | 埋设章节 | 状态 | 回收章节 |" in t, "伏笔表结构:\n" + t
    # 模拟编排器补写聚合表 → force 保留
    t = open(out, encoding="utf-8").read()
    t = t.replace("| （编排器补写） | | | |", "| 黑戒之谜 | 第1章 | 已回收 | 第4章 |")
    open(out, "w", encoding="utf-8").write(t)
    assert run_script(b, ["b.py", "--novel", "保留测试-作者M", "--lib", lib, "--force"]) == 0
    t = open(out, encoding="utf-8").read()
    assert "| 黑戒之谜 | 第1章 | 已回收 | 第4章 |" in t, "伏笔表补写内容 force 后应保留:\n" + t
    # 未补写场景:占位保持(行为不变)
    assert run_script(b, ["b.py", "--novel", "保留测试-作者M", "--lib", lib, "--force"]) == 0
    t = open(out, encoding="utf-8").read()
    assert "主角甲（补写内容）" in t, "已补写内容二次 force 仍应保留"


@test("小说索引:「是否完结」聚合(审查 Bug2 回归)")
def _():
    lib = os.path.join(TEST_ROOT, "t_finished")
    idx_dir = os.path.join(lib, "章节索引")
    os.makedirs(idx_dir, exist_ok=True)
    def write_idx(no, status):
        with open(os.path.join(idx_dir, f"完结测试-作者N-第1章~第5章-{no:03d}-idx.md"), "w", encoding="utf-8") as f:
            f.write(f"---\n小说名称: 《完结测试》\n作者: 作者N\n索引类型: 章节索引\n索引序号: '{no:03d}'\n"
                    f"章节范围: 第1章~第5章\n总章节数: 5\n是否完结: {status}\n包含元素:\n  - 金句\n"
                    f"源文件: 小说原文/完结测试-作者N/完结测试-作者N-第1章~第5章-{no:03d}.md\n"
                    f"创建日期: 2026-08-16\n最后更新: 2026-08-16\n状态: 完成\n---\n")
    import build_novel_index as b
    out = os.path.join(lib, "小说索引", "完结测试-作者N.md")
    # 场景1:全部已完结 → 已完结
    write_idx(1, "已完结")
    write_idx(2, "已完结")
    assert run_script(b, ["b.py", "--novel", "完结测试-作者N", "--lib", lib]) == 0
    t = open(out, encoding="utf-8").read()
    assert "是否完结: 已完结" in t, "全部已完结应标已完结:\n" + t
    # 场景2:任一段连载中 → 连载中(保守,防假完结)
    write_idx(3, "连载中")
    assert run_script(b, ["b.py", "--novel", "完结测试-作者N", "--lib", lib, "--force"]) == 0
    t = open(out, encoding="utf-8").read()
    assert "是否完结: 连载中" in t, "任一段连载中应标连载中:\n" + t


@test("小说索引:小说名/作者名含连字符时 --novel 拆参不歧义")
def _():
    lib = os.path.join(TEST_ROOT, "t_hyphen")
    idx_dir = os.path.join(lib, "章节索引")
    os.makedirs(idx_dir, exist_ok=True)
    # 小说名「斗罗-冰火」作者「魔厨」→ 文件名 斗罗-冰火-魔厨-001-idx.md
    fname = "斗罗-冰火-魔厨-第1章~第5章-001-idx.md"
    with open(os.path.join(idx_dir, fname), "w", encoding="utf-8") as f:
        f.write("---\n小说名称: 《斗罗-冰火》\n作者: 魔厨\n索引类型: 章节索引\n索引序号: '001'\n"
                "章节范围: 第1章~第5章\n总章节数: 5\n是否完结: 连载中\n包含元素:\n  - 金句\n"
                "源文件: 小说原文/斗罗-冰火-魔厨/斗罗-冰火-魔厨-第1章~第5章-001.md\n"
                "创建日期: 2026-08-16\n最后更新: 2026-08-16\n状态: 完成\n---\n")
    import build_novel_index as b
    # 文件名前缀反推:应拆成 (斗罗-冰火, 魔厨) 而非 (斗罗, 冰火-魔厨) 或 (斗罗-冰火-魔厨, '')
    assert b.parse_novel_arg("斗罗-冰火-魔厨", lib) == ("斗罗-冰火", "魔厨")
    # 无匹配时回退 rsplit(最后一段为作者)
    assert b.parse_novel_arg("不存在的小说-佚名", lib) == ("不存在的小说", "佚名")
    # 整链路:--novel 生成小说索引
    rc = run_script(b, ["build_novel_index.py", "--novel", "斗罗-冰火-魔厨", "--lib", lib])
    assert rc == 0, rc
    assert os.path.exists(os.path.join(lib, "小说索引", "斗罗-冰火-魔厨.md"))


@test("验证批次:多书混放时按书分组检查编号(P2-1 回归)")
def _():
    lib = os.path.join(TEST_ROOT, "t_multi_verify")
    os.makedirs(os.path.join(lib, "小说原文"), exist_ok=True)
    os.makedirs(os.path.join(lib, "小说索引"), exist_ok=True)
    idx_dir = os.path.join(lib, "章节索引")
    os.makedirs(idx_dir, exist_ok=True)
    def write_idx(book, author, no):
        with open(os.path.join(idx_dir, f"{book}-{author}-第1章~第5章-{no:03d}-idx.md"), "w", encoding="utf-8") as f:
            f.write(f"---\n小说名称: 《{book}》\n作者: {author}\n索引类型: 章节索引\n索引序号: '{no:03d}'\n"
                    f"章节范围: 第1章~第5章\n总章节数: 5\n是否完结: 连载中\n包含元素:\n  - 金句\n"
                    f"源文件: 小说原文/{book}-{author}/{book}-{author}-第1章~第5章-{no:03d}.md\n"
                    f"创建日期: 2026-08-16\n状态: 完成\n---\n")
    # 书A 缺 002,书B 001-005 齐全 —— 此前全库粒度会漏报
    for no in (1, 3):
        write_idx("书A", "作者A", no)
    for no in range(1, 6):
        write_idx("书B", "作者B", no)
    r = subprocess.run([sys.executable, os.path.join(SCRIPTS, "_验证批次.py"), "--lib", lib],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    assert "《书A》-作者A: 编号不连续，缺: [2]" in r.stdout, "A 书缺 002 应被检出:\n" + r.stdout
    assert "《书B》-作者B: 编号连续" in r.stdout, "B 书编号连续应正常:\n" + r.stdout
    assert r.returncode == 1, "有缺段应返回失败"


@test("行号体检:区间终点与金句不重复计数")
def _():
    lib = os.path.join(TEST_ROOT, "t_refs_edge")
    seg_dir = os.path.join(lib, "小说原文", "边界测试-作者S")
    idx_dir = os.path.join(lib, "章节索引")
    os.makedirs(seg_dir, exist_ok=True)
    os.makedirs(idx_dir, exist_ok=True)
    seg = os.path.join(seg_dir, "边界测试-作者S-第1章-001.md")
    idx = os.path.join(idx_dir, "边界测试-作者S-第1章-001-idx.md")
    with open(seg, "w", encoding="utf-8") as f:
        f.write("---\n小说名称: 《边界测试》\n作者: 作者S\n---\n第1章\n正文")
    with open(idx, "w", encoding="utf-8") as f:
        f.write("---\n源文件: 小说原文/边界测试-作者S/边界测试-作者S-第1章-001.md\n---\n"
                "## 第1章（行号：4 ~ 5）\n### 金句\n- \\\"正文内容\\\"（行4）\n"
                "### 剧情概要\n文本（行4~999）")
    r = subprocess.run([sys.executable, os.path.join(SCRIPTS, "check_line_refs.py"), "--lib", lib, "--detail"],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    assert r.returncode == 1 and "越界" in r.stdout, r.stdout


@test("行号校准:共享前缀多命中探针扩展重试(P3-2 回归)")
def _():
    from verify_and_fix_lines import find_real_line
    seg_lines = ["编号1100000000000001内容甲", "编号1100000000000002内容乙", "其它无关内容行"]
    # 15 字符探针共享前缀多命中 → 扩展 25 字符后唯一命中 → 修正为 1
    assert find_real_line(seg_lines, "编号1100000000000001内容甲", 99) == 1
    # 前 35 字符完全相同 → 扩展后仍多命中 → -1(跳过,不污染)
    seg_lines2 = ["编号1100000000000001" + "甲" * 17 + "内容一",
                  "编号1100000000000001" + "甲" * 17 + "内容二", "其它"]
    assert find_real_line(seg_lines2, seg_lines2[0], 99) == -1


@test("体检与验证批次:check_line_refs / _验证批次")
def _():
    # 复用直调测试库(有完整段文件+索引)跑体检
    lib = os.path.join(TEST_ROOT, "t_direct")
    rc = subprocess.run([sys.executable, os.path.join(SCRIPTS, "check_line_refs.py"), "--lib", lib],
                        capture_output=True, text=True, encoding="utf-8", errors="replace").returncode
    assert rc == 0, f"体检应无越界/章外引用(校准已修正), rc={rc}"
    # 独立小库跑验证批次(段文件+索引+小说索引齐全)
    lib2 = os.path.join(TEST_ROOT, "t_batch")
    seg_dir = os.path.join(lib2, "小说原文", "批次测试-作者L")
    os.makedirs(seg_dir, exist_ok=True)
    with open(os.path.join(seg_dir, "批次测试-作者L-第1章~第5章-001.md"), "w", encoding="utf-8") as f:
        f.write("---\n小说名称: 《批次测试》\n作者: 作者L\n章节范围: 第1章~第5章\n"
                "对应索引: 批次测试-作者L-第1章~第5章-001-idx.md\n分段方式: 按章节切分\n---\n第一章 开局\n正文。")
    idx_dir = os.path.join(lib2, "章节索引")
    os.makedirs(idx_dir, exist_ok=True)
    with open(os.path.join(idx_dir, "批次测试-作者L-第1章~第5章-001-idx.md"), "w", encoding="utf-8") as f:
        f.write("---\n小说名称: 《批次测试》\n作者: 作者L\n索引类型: 章节索引\n索引序号: '001'\n"
                "章节范围: 第1章~第5章\n总章节数: 5\n是否完结: 连载中\n包含元素:\n  - 金句 | 名台词\n"
                "源文件: 小说原文/批次测试-作者L/批次测试-作者L-第1章~第5章-001.md\n"
                "创建日期: 2026-08-16\n最后更新: 2026-08-16\n状态: 完成\n分段方式: 按章节切分\n---\n"
                "## 第1章 开局（行号：8 ~ 9）\n\n### 剧情概要\n\n内容。\n")
    os.makedirs(os.path.join(lib2, "小说索引"), exist_ok=True)
    import build_novel_index as b
    rc = run_script(b, ["build_novel_index.py", "--novel", "批次测试-作者L", "--lib", lib2])
    assert rc == 0
    r2 = subprocess.run([sys.executable, os.path.join(SCRIPTS, "_验证批次.py"), "--lib", lib2],
                        capture_output=True, text=True, encoding="utf-8", errors="replace")
    assert r2.returncode == 0, f"验证批次应全部通过, rc={r2.returncode}, 输出:\n{r2.stdout}"


if __name__ == "__main__":
    run_all()
    print(f"\n临时测试目录: {TEST_ROOT}（保留供排查，可手动删除）")
