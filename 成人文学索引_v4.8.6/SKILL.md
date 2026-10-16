---
name: adult-literature-index
author: 柠檬才不萌
license: Proprietary
description: >
  当用户要给小说（TXT/Markdown）建索引库、追更或重建已索引的书、问某剧情在哪一章/某句原文在哪、
  体检修复索引库、删除/恢复/备份书库，或要借鉴已索引作品的笔法写作时使用；
  出现「空库跑蒸馏」「抽样 0/0」「sk-your-key-here 占位密钥未改」「某句原文查不到」这类症状也使用。
  触发：成人文学索引、小说索引（旧名，仍作通用别名）、索引库、建库、追更、某剧情在哪一章、
  某句原文在哪、体检索引库、用XX的笔法写、仿XX文风、文风蒸馏、风格卡、style-card.json、
  小说导航、章节索引、防撞车。
  不适用：语料是长篇专著/资料/独立文档集、要求回答带文件名+行号双引用的精准问答（归孪生「知识库转录」技能）。
  通用续写/仿写/总结类请求属默认不触发，须问清目标作品与范围后再判断是否进入本流程。
metadata:
  display_name: 成人文学索引

  version: "4.8.6"
---

# 成人文学索引 v4.8.4：编排器协议

## 概述（核心原则）

与 **知识库转录_v5.4.4** 是共用同名 `scripts/*.py` 索引内核的双胞胎配对（分叉登记见第 6 节）：本仓是定制侧（`twin_role: customization`），风格卡/文风蒸馏、`style` 子命令、`norm_name` 防 glob 元字符为独有；建库与检索逻辑跟主干走。
核心原则：**可检索、可复核、可恢复，证据收口**——每条结论附文件路径、命令返回码或统计数字；未过验证门禁只能标 `BLOCKED` 或 `NEEDS_REVIEW`。

## 0. 启动引导

> 新会话第一条消息已点明分支（“建库”“查 XX 在哪章”“用 XX 的笔法写”）→ 直接进第 1 节路由；否则展示菜单，等用户选择。本引导只读配置与目录。

### 0.1 欢迎菜单（按实际状态填写，不编造统计）

```text
📚 成人文学索引 v4.8.4
索引库：<实际绝对路径>
库内书籍：<N> 本（按 小说索引/*.md 统计）
1 建库  2 追更/重建  3 检索/验证  4 基于索引创作  5 文风蒸馏  6 删除/恢复/备份  7 状态面板
回复数字，或直接说需求。
```

### 0.2 启动检查（完成标准：四项都有结论）

- `.env`：缺失 → 提示复制 `.env.example`；`OPENAI_API_KEY` 为空或仍为 `sk-your-key-here` → 建库前停在 `BLOCKED`。
- `<索引库>`：`.env` 的 `INDEX_LIB_DIR`，为空则 `<技能目录>\索引库`；全技能统一使用该绝对路径。
- 库内现状：列出 `小说索引\*.md` 书名，或运行 `orchestrate.py --lib "<索引库>" status`；空库推荐分支 1。
- 可选能力：`INDEX_EXECUTOR` / `INDEX_STYLE_SKILL` / `INDEX_ENABLE_SEMANTIC`；未配置分别按 `direct`、仅轻量风格卡、关闭语义检索执行。

> 配置、输入、输出、缓存的位置速查与首次使用四步：`QUICKSTART.md`（单一来源）。报错先查 `FAQ.md`。

## 1. 路由：先选一个分支

| 分支 | 识别语境 | 立即读取 | 最终交付（完成标准） |
|---|---|---|---|
| 建库 | 从 `.txt/.md` 或 `source-manifest.json` 清单建立小说索引 | `references/建立索引.md` 的建库部分 | 段文件 + 章节索引 + 小说导航 + 验收摘要；VERIFY 三条命令返回 0 |
| 追更/重建 | 已有书库，源文件新增、修订或需要重跑 | `references/建立索引.md` 的追更/恢复部分 | 受影响索引 + 刷新导航 + 增量验收；完整 VERIFY 通过 |
| 检索/验证 | 查询章节、剧情、原文位置，或要求体检/修复 | 检索读 `references/索引内容.md`；验证读 `references/建立索引.md` 的验证部分 | 命中 N 处 + 索引文件/段文件行号，或验证命令返回码与问题清单 |
| 基于索引创作 | 明确提到小说索引、索引桥段或防撞车 | `references/索引内容.md` 的创作部分 | 创作稿 + 素材出处 + 防撞车清单 |
| 文风蒸馏 | 指定已索引作品/作者的笔法 | `references/索引内容.md` 的文风部分 | 轻量风格卡（或外部完整蒸馏）+ 抽样边界，落盘时同时给创作侧机读的 `style-card.json`；目标书无索引时按降级路径答复 |
| 删除/恢复 | 删除某书、恢复备份、重置库 | `references/建立索引.md` 的运维部分 | 删除/恢复清单 + 二次确认记录 + 复核报告 |

模糊请求（“续写/仿写/总结/查一下”）先问清：目标小说、索引库、范围；确认后进入对应分支。可溯源作品笔法 → 文风蒸馏；无索引来源 → 按 reference 的降级路径答复并说明前提。

## 2. 不变量：任何分支都必须遵守

- `<技能目录>` = 本 `SKILL.md` 所在目录；`<索引库>` = `<技能目录>/.env` 的 `INDEX_LIB_DIR`，为空则 `<技能目录>/索引库`。
- 所有模板、reference、脚本从 `<技能目录>` 解析；索引 `源文件` 路径相对 `<索引库>` 解析，绝不相对 cwd。
- 源文件、段文件、已完成索引默认只读；内容变更只走追更/重建，检索阶段不直接改文件。
- 检索/创作按四级读取与授权作用域执行；完成定位或满足需求即停，不扩大到原文或全文。
- **证据收口**：每条结论附文件路径、命令返回码或统计数字；失败、截断、待核、零命中照实标注。
- **门禁**：VERIFY 未通过只能标 `BLOCKED` 或 `NEEDS_REVIEW`；`DONE` = 验证通过且交付契约各栏都有证据。

`.env` 不存在时按 `direct` + 默认 `<技能目录>/索引库` 执行并提示配置 API；`subagent` 仅在宿主确实提供 Agent/子代理能力时可用，否则停在 `BLOCKED` 或回退 `direct`。脚本一律使用技能目录绝对路径：

```text
uv run --with requests --with python-dotenv python "<技能目录>/scripts/脚本.py" ...
```

## 3. 分支流程：按分支读取对应 reference

选定分支后只读下表指向的小节，读完立即返回执行；reference 是决策与门禁协议，不是背景材料。

| 分支 | 读取文件 |
|---|---|
| 建库 / 追更 / 重建 / 验证 / 删除 / 恢复 | `references/建立索引.md` |
| 检索 / 授权 / 创作 / 文风蒸馏 | `references/索引内容.md` |

## 4. 交付契约（证据收口）

每个分支结尾使用以下结构；完成标准：除“失败项”外每栏都有实际内容，`DONE` 只与验证通过的证据同时出现。

```text
状态：DONE / BLOCKED / NEEDS_REVIEW
范围：本次处理的书、章节、文件或抽样范围
读取/生成：实际读取和写入的路径清单
验证：命令、返回码、关键统计或人工抽检结果
失败项：没有则写“无”
恢复/下一步：可直接执行的下一步；未完成不得省略
```

## 命令速查（本仓·定制侧，均已对照 scripts/ 代码核对）

统一调用前缀与完整命令表（初始化/建库/验证续跑/删书/文风蒸馏/语义索引/行号校准/重建导航/重命名迁移）：`references/命令速查与红旗.md`，执行脚本前先读该表核对参数。

## 常见错误 / 红旗（本仓代码防御分支与测试实测，非臆造）

全部 8 条红旗及对应防御代码与测试名：`references/命令速查与红旗.md`（自原正文一字未改挪入）。

## 5. 资源指针

- 建库、追更、验证、恢复、删除：`references/建立索引.md`，只读对应分支小节。
- 检索、授权、全文搜索、跨书、创作、文风：`references/索引内容.md`，先执行授权决策，再执行读取层级。
- LLM 输出：`templates/直调任务模板.md`；子代理输出：`templates/子代理任务模板.md`。
- 章节索引：`templates/章节索引模板.md`；小说导航：`templates/小说索引模板.md`；标签：`templates/包含元素标签词汇表.md`。
- 用户侧快速开始与位置速查：`QUICKSTART.md`；排错：`FAQ.md`。

## 6. 跨技能契约指针（机器可读，勿手删）

本仓的内核主干是 **知识库转录_v5.4.4**（18 份同名 `scripts/*.py` 同源分叉），本仓是它的定制层（风格卡 / 文风蒸馏 / `style` 子命令 / `norm_name` 防 glob 元字符 / `normpath` 主键归一化）；已裁决不抽公共 indexlib，改为把每一处分叉登记在案。

改 `scripts/*.py` 前先看登记簿 `<工作区根>/schemas/twin-kernel.json` 里该文件的 `status`：`identical` 必须两侧同步改、`trunk-ahead` 表示本仓落后主干（可回填）、`customized` 禁止拿主干版本覆盖本仓、`pending-rebase` 先判归属。自检：`PYTHONIOENCODING=utf-8 python schemas/contract_check.py`。

> **适用范围**：上面两条路径（`schemas/twin-kernel.json`、`schemas/contract_check.py`）属于技能柜共享内核，只在开发柜里存在。独立安装的本包不含柜根，收件人的自检请用包内门：`PYTHONIOENCODING=utf-8 python scripts/check_package.py --as-package`；随包分发的协议件及其来源摘要登记在 `scripts/vendor/VENDOR.json`，缺件或漂移该门会直接判红。

<!-- pipeline-contract:begin
id: adult-index-twin-kernel
namespace: twin-kernel
schema: schemas/twin-kernel.json
version: "1.0.0"
skill_version: "4.8.6"
twin_of: "知识库转录_v5.4.8"  # 本仓内核主干所在：建库/检索代码同源于它
twin_role: customization  # 本仓是定制层：风格卡/文风蒸馏/style 子命令/norm_name 防 glob 元字符/normpath 主键归一化都是本仓独有
twin_reason: "已裁决不抽公共 indexlib（两份都在产线、分叉千行级、无人验收重构），改为把 18 份同名 scripts/*.py 的每一处分叉登记进 schemas/twin-kernel.json，由 schemas/contract_check.py 门住静默漂移；主干侧新增能力回填前先查登记簿 status，customized 的一律禁止无脑覆盖"
pipeline-contract:end -->
