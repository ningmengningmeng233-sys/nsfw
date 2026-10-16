#!/usr/bin/env python3
"""
章节索引直调生成器 v4.0（ADR-0001）

每个段文件 → 一个线程 → 直调 LLM API 生成章节索引。
- 32 路并行（ThreadPoolExecutor），429 自动重试退避
- SQLite 断点层（index_db.py）：done 跳过、失败写原因、中断重跑零浪费
- --skip-existing：目标索引文件已存在则跳过（文件即状态兜底）
- 行号精度：段文件逐行加行号前缀传入；生成后自动跑行号校准
  （v4.0 双层：金句引用回查修正 + 区间/短语引用高置信修正，--no-range 可关）
- prompt 模板：templates/直调任务模板.md（单源真相）

v3.32 变更：
- 并发默认 16 → 32（DeepSeek 并发上限 2500，429 风险≈0）
- max_tokens 12000 → 20000（堵输出截断）
- 流式输出（stream=True + SSE 解析，跳过 keep-alive 注释行）
- token 消耗统计（usage 记入断点库，结束打印汇总；v3.9 起默认请求流式 usage，
  个别 API 不认 stream_options 时自动降级）
- 段文件内容 hash 检测（追更改写段文件 → 自动重跑，防行号漂移）

v3.9 修复：
- --no-range 参数生效（此前只在独立运行校准脚本时可用，直调内嵌路径丢失该开关）
- 校准改用 --lib 指定的索引库（此前内嵌校准固定用 .env 的 INDEX_LIB_DIR，自定义
  库时找不到段文件静默空转）

配置（技能目录 .env，见 .env.example）:
  OPENAI_API_KEY / SUMMARY_API_URL / SUMMARY_API_MODEL / INDEX_LIB_DIR

用法:
  python build_segment_index.py "<段文件路径>" ... [--lib <索引库根目录>] [--workers 32] [--skip-existing] [--no-fix] [--no-range]
"""
import argparse
import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

SKILL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(SKILL_DIR, "scripts"))

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(SKILL_DIR, ".env"))
except Exception:
    pass

import requests
from chapter_patterns import extract_chapters, normalize_ch_name
from index_db import IndexDB, file_hash
from _common import parse_frontmatter, read_text_auto, resolve_under
from _logging import add_log_format_argument, configure_logging, get_logger
import tags

API_URL = os.environ.get("SUMMARY_API_URL", "https://api.openai.com/v1/chat/completions")
API_KEY = os.environ.get("OPENAI_API_KEY", "")
API_MODEL = os.environ.get("SUMMARY_API_MODEL", "gpt-4o-mini")
INDEX_LIB_DIR = os.environ.get("INDEX_LIB_DIR", "").strip() or os.path.join(SKILL_DIR, "索引库")
API_KEY_PLACEHOLDER = "sk-your-key-here"


def _ensure_api_key() -> str:
    """API key 预检：空值或仍为配置模板占位符时直接失败，不发无意义请求。"""
    key = (API_KEY or "").strip()
    if not key or key == API_KEY_PLACEHOLDER:
        raise RuntimeError(
            "OPENAI_API_KEY 未配置或仍为占位符 sk-your-key-here，"
            "请在 <技能目录>/.env 填写真实 key 后重试"
        )
    return key

TEMPLATE_FILE = os.path.join(SKILL_DIR, "templates", "直调任务模板.md")
CHAPTER_TMPL = os.path.join(SKILL_DIR, "templates", "章节索引模板.md")

logger = get_logger(__name__)


def number_lines(text):
    lines = text.splitlines()
    return "\n".join(f"{i+1:05d}: {line}" for i, line in enumerate(lines))


def _unwrap_fence(content):
    """v4.6.4-fix(本地运维): deepseek-chat 偶发把整份输出包进 ```markdown 代码围栏，
    导致 parse_frontmatter 拿不到以 --- 开头的 frontmatter → 结构校验误报
    “frontmatter 缺少全部字段”。此函数剥掉首尾围栏行，恢复正常 markdown。"""
    s = content.lstrip("﻿").lstrip()
    if s.startswith("```"):
        lines = s.splitlines()
        if len(lines) >= 3 and lines[-1].strip().startswith("```"):
            inner = "\n".join(lines[1:-1])
            if inner.strip():
                return inner
        rest = "\n".join(lines[1:])
        if rest.strip():
            return rest
    return content


def validate_index_output(content, seg_path, chapters, lib_dir=None):
    """验证 LLM 产出的最小结构，未通过不得写入 done 状态。

    这不是内容质量判定，而是防止空响应、错误文件类型、源文件错指和漏章索引
    被断点层误认为成功。内容质量仍由编排器抽样盲检负责。
    """
    meta, body = parse_frontmatter(content)
    required = (
        "小说名称", "作者", "索引类型", "索引序号", "章节范围", "总章节数",
        "是否完结", "包含元素", "源文件", "创建日期", "最后更新", "状态",
    )
    missing = [key for key in required if key not in meta or not str(meta[key]).strip()]
    if missing:
        return f"索引 frontmatter 缺少字段: {', '.join(missing)}"
    if meta.get("索引类型") != "章节索引":
        return f"索引类型错误: {meta.get('索引类型')!r}"
    try:
        count = int(str(meta["总章节数"]).strip().strip("'\\\""))
    except ValueError:
        return f"总章节数不是整数: {meta.get('总章节数')!r}"
    if chapters and count != len(chapters):
        return f"总章节数不匹配: frontmatter={count}, 段内章节={len(chapters)}"
    if count < 1:
        return f"总章节数必须为正数: {count}"
    source = str(meta["源文件"]).replace("\\", "/").strip()
    if os.path.basename(source) != os.path.basename(seg_path):
        return f"源文件未指向当前段: {source!r} != {os.path.basename(seg_path)!r}"
    if lib_dir:
        source_path = resolve_under(lib_dir, source)
        if source_path is None:
            return f"源文件回指越出索引库: {source!r}"
        if os.path.abspath(source_path) != os.path.abspath(seg_path):
            return f"源文件回指路径不一致: {source_path!r} != {os.path.abspath(seg_path)!r}"
    if not body.strip() or not re.search(r"^##\s+", body, re.M):
        return "索引正文为空或缺少二级章节小节"
    if chapters:
        headings = [h for h in re.findall(r"^##\s+(.+)$", body, re.M)
                    if not h.startswith(("本段概况", "本段总结"))]
        if len(headings) != len(chapters):
            return f"章节小节数量不匹配: 实际={len(headings)}, 预期={len(chapters)}"
        # v4.6.3：只查数量会放过 LLM 把章节标题写反/写错的情况，导致行号与
        # 检索结果张冠李戴；这里比较规范化后的章名序列。
        expected_names = [normalize_ch_name(c.strip()) for c in chapters]
        actual_names = [normalize_ch_name(h.strip()) for h in headings]
        if actual_names != expected_names:
            return f"章节小节标题与段文件不匹配: 实际={actual_names}, 预期={expected_names}"
    m = re.search(r"-(\d{3})\.md$", os.path.basename(seg_path))
    idx_no = str(meta["索引序号"]).strip().strip("'\\\"")
    if m and idx_no.zfill(3) != m.group(1):
        return f"索引序号与段文件不一致: {idx_no!r} != {m.group(1)!r}"
    return None


def call_llm(system_prompt, user_prompt, max_tokens=28000, retries=3):
    """流式直调 LLM。返回 (content, usage)；429 退避 10/20/30s，错误退避 5/10/15s。
    v3.9：默认请求流式 usage（stream_options include_usage）；个别 API 不认该参数
    返回 400 时降级重试（无 token 统计，不阻塞主流程）。"""
    _ensure_api_key()
    payload = {
        "model": API_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "max_tokens": max_tokens,
        "temperature": 0.4,
        "stream": True,
    }
    include_usage = True
    last_err = None
    for attempt in range(retries):
        try:
            if include_usage:
                payload["stream_options"] = {"include_usage": True}
            resp = requests.post(
                API_URL, json=payload,
                headers={"Content-Type": "application/json", "Authorization": f"Bearer {API_KEY}"},
                timeout=(30, 600), stream=True,
            )
            if resp.status_code == 429:
                resp.close()
                wait = 10 * (attempt + 1)
                logger.info(f"    ⏳ 429 限流，等待 {wait}s 重试...")
                # v4 修复：记录最后一次 429，重试耗尽后抛出明确错误（此前 last_err
                # 保持 None，raise None 抛 TypeError 误导排查）
                last_err = RuntimeError(f"429 限流重试 {retries} 次耗尽（等待 {wait}s 后仍被限流）")
                time.sleep(wait)
                continue
            resp.raise_for_status()
            try:
                parts, usage, done = [], {}, False
                # v3.9 修复：SSE 按字节流手动分割（iter_lines 的 str.splitlines() 会把
                # UTF-8 中文的 0x85 字节在 ISO-8859-1 解码后误判为换行符，拆断 JSON 行）
                buf = bytearray()
                finish_reason = None
                for raw in resp.iter_content(8192):
                    if done:
                        break  # [DONE] 后不再读取/解析（防残余帧污染 parts）
                    buf += raw
                    while b"\n" in buf:
                        line, _, buf = buf.partition(b"\n")
                        sline = line.strip()
                        if not sline.startswith(b"data:"):
                            continue  # 跳过 SSE keep-alive 注释（: keep-alive）
                        data = sline[5:].strip()
                        if data == b"[DONE]":
                            buf = bytearray()
                            done = True
                            break
                        try:
                            chunk = json.loads(data.decode("utf-8"))
                        except (json.JSONDecodeError, UnicodeDecodeError):
                            continue
                        for ch in chunk.get("choices", []):
                            if ch.get("finish_reason"):
                                finish_reason = ch["finish_reason"]
                            delta = ch.get("delta") or {}
                            if delta.get("content"):
                                parts.append(delta["content"])
                        if chunk.get("usage"):
                            usage = chunk["usage"]
            finally:
                # v4.5 修复（审查）：正常完成路径必须释放流式连接——[DONE] 提前
                # break 未读完 body，不复用连接，32 并发 × 数百段会累积连接泄漏
                resp.close()
            content = "".join(parts).strip()
            if finish_reason == "length":
                raise ValueError("流式响应因 max_tokens 截断，索引未完整生成")
            if not content:
                raise ValueError("流式响应为空")
            return content, usage
        except requests.HTTPError as e:
            status = getattr(getattr(e, "response", None), "status_code", None)
            if e.response is not None:
                try:
                    e.response.close()
                except Exception:
                    pass
            if include_usage and status == 400:
                # 部分 OpenAI 兼容 API 不认 stream_options → 降级为无 usage 重试
                include_usage = False
                payload.pop("stream_options", None)
                logger.info("    ℹ️ API 不支持 include_usage，降级重试（token 统计不可用）")
                continue
            if status in (401, 403):
                # 认证/授权错误重试无意义：立即失败并给出明确原因
                last_err = RuntimeError(f"API 认证失败（HTTP {status}），请检查 OPENAI_API_KEY / API 地址与权限")
                logger.error("    ❌ %s", last_err)
                break
            last_err = e
            wait = 5 * (attempt + 1)
            logger.warning(f"    ⚠️ 调用失败({e})，{wait}s 后重试 ({attempt+1}/{retries})")
            time.sleep(wait)
        except Exception as e:
            last_err = e
            wait = 5 * (attempt + 1)
            logger.warning(f"    ⚠️ 调用失败({e})，{wait}s 后重试 ({attempt+1}/{retries})")
            time.sleep(wait)
    raise last_err


def build_one(seg_path, out_dir, skip_existing, do_fix, do_range, lib_dir, db):
    text = read_text_auto(seg_path)
    meta, body = parse_frontmatter(text)
    if not meta.get("小说名称") or not meta.get("作者") or not meta.get("章节范围"):
        raise ValueError(f"段文件 frontmatter 缺少小说名称/作者/章节范围: {seg_path}")
    novel = meta["小说名称"].strip("《》")
    author = meta["作者"].strip()
    seg_name = os.path.basename(seg_path).replace(".md", "")
    out_path = os.path.join(out_dir, seg_name + "-idx.md")
    seg_hash = file_hash(seg_path)
    chapters = extract_chapters(body)

    def existing_is_valid():
        try:
            with open(out_path, encoding="utf-8-sig") as f:
                existing = f.read()
        except (OSError, UnicodeError):
            return False
        return validate_index_output(existing, seg_path, chapters, lib_dir) is None

    # 断点：DB done + 文件存在 + 内容 hash 一致 + 输出结构合法 → 跳过。
    # 损坏/截断索引必须重跑，不能被 done 状态永久遮蔽。
    if db.should_skip(seg_path, out_path, seg_hash):
        if existing_is_valid():
            logger.info(f"  ⏭️  {seg_name}: 已完成且索引结构合法（断点跳过）")
            return out_path, "skipped"
        logger.warning(f"  ⚠️  {seg_name}: 断点指向损坏索引，重新生成")
    # --skip-existing：仅当段文件 hash 未变且输出结构合法时跳过。
    if skip_existing and os.path.exists(out_path):
        row = db.get(seg_path)
        old_hash = row[5] if row else None
        if old_hash and old_hash == seg_hash and existing_is_valid():
            logger.info(f"  ⏭️  {seg_name}: 索引文件已存在、结构合法且段文件未变（--skip-existing）")
            return out_path, "skipped"

    db.mark_pending(seg_path, out_path, seg_hash)
    numbered = number_lines(text)

    try:
        template = read_text_auto(TEMPLATE_FILE)
        chapter_tmpl = read_text_auto(CHAPTER_TMPL)
    except FileNotFoundError as e:
        logger.error("  ❌ %s: 缺少模板文件 %s", seg_name, e)
        db.mark_failed(seg_path, f"missing template: {e}")
        return None, "failed"

    vocab = tags.load_tag_vocabulary(lib_dir)
    if not vocab:
        logger.error("  ❌ %s: 标签词汇表为空或无法加载", seg_name)
        db.mark_failed(seg_path, "empty tag vocabulary")
        return None, "failed"

    # 本地运维补丁(脱敏输出): 词汇表转组代号，模型只输出【组N】，写入前本地展开
    try:
        from _tag_codes import build_code_map, vocab_to_code_text, expand_tag_codes
        _code_map = build_code_map(vocab)
        if _code_map:
            vocab = vocab_to_code_text(_code_map)
    except ImportError:
        _code_map = None

    logger.info(f"  [{seg_name}] 开始: {novel} | {meta.get('章节范围','')} | {len(chapters)} 章 | {len(text)} 字符")

    # 从模板文件构造 prompt（单源真相）
    parts = template.split("## 系统提示\n", 1)
    if len(parts) != 2:
        db.mark_failed(seg_path, "直调任务模板缺少‘## 系统提示’")
        return None, "failed"
    prompt_body = parts[1]
    user_marker = "\n## 用户输入模板\n"
    if user_marker not in prompt_body:
        db.mark_failed(seg_path, "直调任务模板缺少‘## 用户输入模板’")
        return None, "failed"
    system_prompt, user_tmpl = prompt_body.split(user_marker, 1)
    system_prompt = system_prompt.strip()
    user_tmpl = user_tmpl.strip()

    user_prompt = user_tmpl.format(
        seg_path=seg_path,
        novel=novel,
        author=author,
        chapter_range=meta.get("章节范围", ""),
        chapter_count=len(chapters),
        chapters="\n".join(chapters),
        template=chapter_tmpl,
        vocab=vocab,
        numbered=numbered,
    )

    content, usage = call_llm(system_prompt, user_prompt)
    content = _unwrap_fence(content)
    if _code_map:
        content = expand_tag_codes(content, _code_map)
    # 本地运维补丁(源文件自愈): 模型照抄模板占位符时以真实相对路径覆盖
    _src_line = "源文件: " + os.path.relpath(seg_path, lib_dir).replace("\\", "/")
    content, _n = re.subn(r"^(源文件:\s*).*$", _src_line.replace("\\", "\\\\"), content, count=1, flags=re.M)
    if _n == 0 and re.search(r"^状态:", content, re.M):
        content = content.replace("\n状态:", f"\n{_src_line}\n状态:", 1)
    # 本地运维补丁(索引序号自愈): 索引序号=段文件编号（模板硬性要求），模型偶发写错
    # 时以文件名尾号覆盖——glm-4-flash 对多位编号提取不稳定，重跑无法收敛
    _m_no = re.search(r"-(\d{3})\.md$", os.path.basename(seg_path))
    if _m_no:
        content, _ = re.subn(r"^(索引序号:\s*).*$", rf"索引序号: {_m_no.group(1)}", content, count=1, flags=re.M)
    # 本地运维补丁(总章节数自愈): 模型偶发误数章节时以段内实际章节数覆盖
    if chapters:
        content, _ = re.subn(r"^(总章节数:\s*).*$", rf"总章节数: {len(chapters)}", content, count=1, flags=re.M)
    validation_error = validate_index_output(content, seg_path, chapters, lib_dir)
    if validation_error:
        db.mark_failed(seg_path, validation_error)
        logger.error(f"  ❌ {seg_name}: 索引结构校验失败: {validation_error}")
        return None, "failed"
    prompt_tokens = usage.get("prompt_tokens", 0)
    completion_tokens = usage.get("completion_tokens", 0)

    with open(out_path, "w", encoding="utf-8") as f:
        f.write(content.rstrip() + "\n")

    # 自动行号校准 + 一致性检查（失败不阻塞）
    # v3.9 修复：传入 lib_dir/do_range——此前 process_file 拿不到 --lib 与 --no-range，
    # 自定义索引库时校准找错库、--no-range 开关不生效
    if do_fix:
        try:
            from types import SimpleNamespace
            from verify_and_fix_lines import process_file, check_consistency
            calib = SimpleNamespace(lib=lib_dir, no_range=not do_range)
            process_file(out_path, calib)
            check_consistency(out_path)
        except Exception as e:
            logger.warning(f"    ⚠️ 行号校准/一致性检查异常: {e}")

    db.mark_done(seg_path, out_path, prompt_tokens, completion_tokens, seg_hash)
    logger.info(f"  ✅ {os.path.basename(out_path)} 已生成（{len(content)} 字符 | prompt {prompt_tokens} | completion {completion_tokens}）")
    return out_path, "done"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("segments", nargs="+", help="段文件路径（可多个，也可传目录）")
    parser.add_argument("--lib", default=INDEX_LIB_DIR, help="索引库根目录（默认 INDEX_LIB_DIR/.env 或技能目录/索引库）")
    parser.add_argument("--out", help="章节索引输出目录（默认 <索引库>/章节索引）")
    parser.add_argument("--workers", type=int, default=32)
    parser.add_argument("--skip-existing", action="store_true", help="目标索引文件已存在且段文件 hash 未变时跳过")
    parser.add_argument("--no-fix", action="store_true", help="跳过行号校准")
    parser.add_argument("--no-range", action="store_true", help="跳过区间/短语引用校准（只做金句校准）")
    add_log_format_argument(parser)
    args = parser.parse_args()
    configure_logging(fmt=args.log_format)
    if args.workers <= 0:
        parser.error("--workers 必须是正整数")

    try:
        _ensure_api_key()
    except RuntimeError as e:
        logger.error("❌ %s", e)
        return 1

    # 展开目录参数为段文件列表
    # v3.9 修复：目录扫描只收规范段文件（`-NNN.md` 结尾）——此前目录下所有 .md
    # （含说明文件）都会被当作段文件直调，浪费 token
    seg_files = []
    for s in args.segments:
        if os.path.isdir(s):
            seg_files.extend(
                os.path.join(s, f)
                for f in sorted(os.listdir(s))
                if re.search(r"-\d{3}\.md$", f) and not f.endswith("-idx.md")
            )
        else:
            seg_files.append(s)

    out_dir = args.out or os.path.join(args.lib, "章节索引")
    os.makedirs(out_dir, exist_ok=True)
    for d in ("小说原文", "小说索引", "章节索引"):
        os.makedirs(os.path.join(args.lib, d), exist_ok=True)

    logger.info(f"📚 待处理 {len(seg_files)} 个段 → 输出: {out_dir}")
    logger.info(f"🔄 直调模式（{args.workers} workers, 模型: {API_MODEL}, 断点: SQLite, 流式: 开）...")

    t0 = time.time()
    ok = skipped = fail = 0
    with IndexDB(args.lib) as db:
        with ThreadPoolExecutor(max_workers=args.workers) as ex:
            futures = {
                ex.submit(build_one, p, out_dir, args.skip_existing, not args.no_fix,
                          not args.no_range, args.lib, db): p
                for p in seg_files
            }
            for fut in as_completed(futures):
                p = futures[fut]
                try:
                    _, status = fut.result()
                    if status == "done":
                        ok += 1
                    elif status == "skipped":
                        skipped += 1
                    else:
                        fail += 1
                except Exception as e:
                    fail += 1
                    logger.error(f"  ❌ {os.path.basename(p)} 失败: {e}")
                    try:
                        with IndexDB(args.lib) as db2:
                            db2.mark_failed(p, str(e))
                    except Exception:
                        pass
        _, (sum_prompt, sum_comp) = db.stats()

    elapsed = time.time() - t0
    logger.info(f"\n{'='*50}")
    logger.info(f"📊 完成: 生成 {ok}, 跳过 {skipped}, 失败 {fail}, 耗时 {elapsed:.0f}s")
    if ok:
        logger.info(f"💰 token 统计: prompt 共 {sum_prompt:,} / completion 共 {sum_comp:,}（均值 prompt {sum_prompt//max(ok,1):,}、completion {sum_comp//max(ok,1):,} 每段）")
    if fail:
        logger.info("ℹ️  失败段已记录到索引库断点库，重跑同一命令即可续传（重试已计数）")
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
