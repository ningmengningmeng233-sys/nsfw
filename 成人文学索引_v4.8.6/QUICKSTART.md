# 成人文学索引 v4.8.3 · 快速开始（QUICKSTART）

> 面向人类的 5 分钟上手指南。需要 Python 3.10+；完整规则见 `SKILL.md`（模型执行协议）与 `README_CN.md`（完整文档）。

## 一页流程

```text
没有 .env？
  └─→ Copy-Item .env.example .env，填写 OPENAI_API_KEY / SUMMARY_API_URL / SUMMARY_API_MODEL
        ↓
准备源文件：任意路径的 .txt / .md（GBK/UTF-8 均可）
确认：小说名、作者；多卷书说明卷号
        ↓
orchestrate.py --lib "<索引库>" init
orchestrate.py --lib "<索引库>" build --source "<源文件>" --novel "书名" --author "作者"
        ↓
build 内部自动：拆段 → 生成章节索引 → VERIFY 验证 → 发布小说导航
        ↓
建库完成后才能：检索定位 / 基于索引创作 / 文风蒸馏
```

## 位置速查

| 项 | 默认位置 |
|---|---|
| 配置文件 | `<技能目录>\.env` |
| 源文件输入 | 你指定的任意绝对路径（skill 不搬移原文件） |
| 拆段输出 | `<索引库>\小说原文\<小说名>-<作者>\` |
| 章节索引 | `<索引库>\章节索引\` |
| 小说导航 | `<索引库>\小说索引\<小说名>-<作者>.md` |
| 风格卡 | `<索引库>\..\风格卡\<小说名>-<作者>\style-card.md`（人读）+ 同目录 `style-card.json`（创作侧机读） |
| 断点/缓存 | `<索引库>\.index_state.db`、`<索引库>\.semantic_index\` |
| 备份 | 复制整个 `<索引库>` + 风格卡目录 |

## 第 1 步：配置（一次即可）

```powershell
# 在技能目录下
Copy-Item .env.example .env
notepad .env
```

`.env` 里至少填 3 项；`INDEX_LIB_DIR` 留空则默认使用 `<技能目录>\索引库`：

```ini
OPENAI_API_KEY=你的key
SUMMARY_API_URL=https://api.deepseek.com/v1/chat/completions
SUMMARY_API_MODEL=deepseek-chat
INDEX_LIB_DIR=
```

## 第 2 步：建第一本书

所有命令用绝对路径；`orchestrate.py` 的 `--lib` **必须放在子命令前**：

```text
uv run --with requests --with python-dotenv python "<技能目录>/scripts/orchestrate.py" --lib "<索引库>" init
uv run --with requests --with python-dotenv python "<技能目录>/scripts/orchestrate.py" --lib "<索引库>" build --source "D:\novels\某小说.txt" --novel "书名" --author "作者"
```

build 会依次输出/生成：

1. `小说原文/<书名>-<作者>/...` 段文件
2. `章节索引/<书名>-<作者>-第X章~第Y章-NNN-idx.md`
3. `小说索引/<书名>-<作者>.md` 导航
4. 验收摘要（状态 / 验证 / 失败项）

## 第 3 步：检查结果

```text
uv run --with requests --with python-dotenv python "<技能目录>/scripts/orchestrate.py" --lib "<索引库>" status
```

状态面板显示书籍数、段数、失败数、token 用量。`build` 结尾必须看到验证全部通过（返回码 0）。

## 第 4 步：使用

| 想做什么 | 怎么问 / 怎么跑 |
|---|---|
| 查剧情在哪章 | “在小说索引库里查「退婚」在哪几章” |
| 查某句原文 | “「莫欺少年穷」在第几章，原文在哪行” |
| 基于索引创作 | “基于《书名》的索引写一个不撞车的桥段” |
| 文风蒸馏 | “用《书名》的笔法写……”，或 `orchestrate.py --lib "<索引库>" style --novel 书名 --author 作者` |
| 追更 | 源文件更新后用 `split_segments.py --append` 增量拆段，再重跑受影响索引并完整 VERIFY |
| 体检 | `orchestrate.py --lib "<索引库>" verify` |

> 文风蒸馏的前置条件：目标作品**已经建库**；完整蒸馏还需在 `.env` 配置 `INDEX_STYLE_SKILL`。

## 日常三件事

```text
# 1. 追更：先增量拆段（只处理新增章节；末段变化会并入/新建）
uv run --with requests --with python-dotenv python "<技能目录>/scripts/split_segments.py" "<更新后的源文件>" --novel "书名" --author "作者" --lib "<索引库>" --append
#    然后重跑受影响段的索引，并执行完整 VERIFY；导航用 build_novel_index.py --force 刷新

# 2. 体检
uv run --with requests --with python-dotenv python "<技能目录>/scripts/orchestrate.py" --lib "<索引库>" verify

# 3. 备份（复制整个 <索引库> + 风格卡目录；恢复后必须重新 verify）
```

遇到报错先看 `FAQ.md`。
