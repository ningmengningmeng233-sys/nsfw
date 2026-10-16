# 成人文学索引 v4.8.3 · 常见问题与排错（FAQ）

> 按“症状 → 原因 → 处理”查表。向模型求助时，请贴出完整命令与报错第一屏，不要只说“报错了”。

## 一、命令与配置

### Q1. orchestrate.py 报 `unrecognized arguments: --lib`

`--lib` 是全局参数，必须放在子命令**前面**。

```text
# 错误
orchestrate.py build --source xxx --novel 书名 --author 作者 --lib "<索引库>"
# 正确
orchestrate.py --lib "<索引库>" build --source xxx --novel 书名 --author 作者
```

### Q2. 建库不调用 API，或一直提示要配置

- 检查 `<技能目录>\.env` 是否存在；
- `OPENAI_API_KEY` 是否还是占位符 `sk-your-key-here`；
- `SUMMARY_API_URL` / `SUMMARY_API_MODEL` 是否填了。
- 只报告“已配置/未配置”，**不要把 key 贴进对话或日志**。

### Q2.5. 自定义标签报 `No module named 'yaml'`

`tags.custom.yaml` 需要 PyYAML。用 uv 临时环境时在命令里加 `--with pyyaml`：

```text
uv run --with requests --with python-dotenv --with pyyaml python "<技能目录>/scripts/build_segment_index.py" ...
```

或直接 `pip install -r requirements.txt`。

### Q2.6. Python 版本不兼容 / 语法报错

本技能需要 **Python 3.10+**（代码使用 `str | None` 与内置泛型注解）。请升级 Python 后再运行。

### Q3. .env 找不到 / 复制模板

`.env` 必须位于 `SKILL.md` 所在目录，不在 cwd。初始化也会自动复制：

```text
uv run --with requests --with python-dotenv python "<技能目录>/scripts/orchestrate.py" --lib "<索引库>" init
```

### Q4. 设置了 INDEX_LIB_DIR 但脚本还用默认库

脚本读取的是 `<技能目录>\.env`，不是当前目录的 `.env`。确认 `INDEX_LIB_DIR` 写在技能目录的 `.env` 里，并用绝对路径。

## 二、文风蒸馏

### Q5. 为什么没有文风蒸馏输出？

文风蒸馏有三个前置条件，缺一不可：

1. 触发词明确指定作品/作者（“用 XX 的笔法写”“仿 XX 文风”）；
2. 目标作品**已经建库**（`章节索引/` 里有对应索引）；库为空时按降级路径答复“先建索引”，这是预期行为；
3. 完整蒸馏还需在 `.env` 配置 `INDEX_STYLE_SKILL`，否则只出轻量风格卡。

### Q6. 外部 skill 目录找不到入口

`INDEX_STYLE_SKILL`（或 `--style-skill`）接受三种写法：

1. **技能目录**（如 `D:\skills\wenfeng-skill`）：按以下顺序解析入口脚本，命中即停：
   1. 该目录 `SKILL.md` front matter 里的 `style-entry: 相对路径`；
   2. `scripts/run_style.py`；
   3. `run.py`。
   入口以子进程调用，收到 `--lib <索引库> --novel <书名> --author <作者> --output-dir <风格卡目录>`
   四个参数，并把风格卡写进 `--output-dir`。三个候选都没命中（或 `style-entry` 指的文件不存在）
   时，报错会点名查过哪些候选——按提示补 `SKILL.md` 的 `style-entry` 或放一个 `scripts/run_style.py` 即可。
2. `.py` 文件路径：直接子进程调用，传 `--lib/--novel/--author`（不传 `--output-dir`，保持旧接口兼容）。
3. 可执行命令字符串：按 `shlex.split` 拆分执行，检查返回码。

外部技能非零退出时只回显它 stderr 的**尾部摘要**（已去掉堆栈、并隐去 `api_key`/`token` 之类的值），
不会把整段 traceback 甩给你。

### Q7. 风格卡在哪里？

默认写到索引库同级，人读一份 + 机读一份同目录同词干：

```text
<索引库>\..\风格卡\<小说名>-<作者>\style-card.md
<索引库>\..\风格卡\<小说名>-<作者>\style-card.json
```

`style-card.json` 是创作侧消费的契约文件（字段定义见包内 `schemas/style-card.schema.json` 受控副本，来源登记见 `schemas/VENDORED.md`；包内自校验逻辑与协议件见 `scripts/style_card_schema.py`、`scripts/vendor/` 与 `scripts/check_package.py`），
轻量蒸馏与目录式完整蒸馏都会产出；写盘前会按该 schema 自校验。目录可用 `--output-dir` 覆盖。
CLI 成功后只打印路径，卡片内容需要打开该文件查看。

## 三、位置、缓存与备份

### Q8. 哪些缓存可以删？删了会怎样？

| 路径 | 可否删除 | 影响 |
|---|---|---|
| `__pycache__\`、`.pytest_cache\` | 可随时删 | 无 |
| `<索引库>\.semantic_index\` | 可删 | 语义检索失效，重新 `semantic build --force` |
| `<索引库>\.index_state.db` | 可删，但别随便删 | 断点与 hash 锚点丢失；`--skip-existing` 需要库里已有 hash 记录才会跳过，丢失后大概率会重新调用 API。先恢复备份的 DB，或接受重建 |

### Q9. 怎么备份和恢复？

备份 = 复制整个 `<索引库>`（三目录 + `.index_state.db`）+ 风格卡目录。恢复后必须重新运行：

```text
uv run --with requests --with python-dotenv python "<技能目录>/scripts/orchestrate.py" --lib "<索引库>" verify
```

## 四、建库与追更

### Q10. 源文件读取乱码 / UnicodeDecodeError

脚本内置编码回退：UTF-8 → GB18030 → GBK。仍失败时，用记事本“另存为 UTF-8”后再跑。

### Q11. 多卷小说章号错乱 / 整卷丢失

多卷且卷内章号重复时，每个卷文件必须单独处理并加 `--volume`：

```text
split_segments.py "第5卷.txt" --novel 书名 --author 作者 --lib "<索引库>" --volume 第5卷
```

### Q12. 追更后索引没更新 / 行号漂移

- 源文件更新后用 `split_segments.py ... --append` 增量拆段；全新书才用 `orchestrate.py build`；
- 只重跑新增/变化段；
- **完整 VERIFY 通过后**才刷新小说导航；
- 不要手改段文件或索引文件来“修行号”。

### Q13. 验证失败，状态是 NEEDS_REVIEW

按顺序看这三条的输出，哪条非 0 修哪条：

```text
_验证批次.py --lib "<索引库>"
verify_and_fix_lines.py "<索引库>/章节索引" --lib "<索引库>"
check_line_refs.py --lib "<索引库>" --detail
```

失败段可用 `orchestrate.py --lib "<索引库>" resume` 续跑；未通过验证不得宣布“已完成”。

## 五、检索、创作与其他

### Q14. 库是空的，问检索/创作/文风怎么办？

先建库。空库时模型按协议给出降级路径，不会伪造命中或风格结论；手动跑 `style` 也只会得到“索引中未收录足够……”的占位卡。

### Q15. 语义检索用不了

语义检索是可选模块：

1. 安装依赖 `numpy` + `sentence-transformers`；
2. 构建索引：`orchestrate.py --lib "<索引库>" semantic build --force`；
3. 未构建时关键词检索不受影响，语义召回自动跳过。

### Q16. 检索时总被要求授权 / 限制读取

检索按“元数据 → 索引段落 → 全部索引 → 原文段落”四级执行；定位类问题会自动进第 2 级，看原文仍需明确要求。长期授权记录在 `<索引库>\_授权状态.yaml`，模板见同目录 `_授权状态.yaml.example`。

### Q17. 删书后想恢复

删除不可逆。删除前先备份索引库与风格卡；恢复时把备份复制回 `<索引库>`，再跑完整 `verify`。断点库残留可参照 `references/建立索引.md` 的运维小节处理。
