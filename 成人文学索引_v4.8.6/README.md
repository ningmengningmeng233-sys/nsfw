# Adult Literature Index · 成人文学索引 v4.8.5

这是一个用于小说建库、追更、章节/原文定位、索引验证、基于索引创作、文风蒸馏和索引运维的 Agent Skill。

完整的人类使用文档、配置和变更说明见 [README_CN.md](./README_CN.md)。5 分钟上手见 [QUICKSTART.md](./QUICKSTART.md)，排错见 [FAQ.md](./FAQ.md)。模型执行协议见 [SKILL.md](./SKILL.md)。

## Quick start

运行命令必须把脚本路径锚定到技能目录，不依赖当前工作目录：

```text
uv run --with requests --with python-dotenv python "<技能目录>/scripts/split_segments.py" "<源文件>" --novel "小说名" --author "作者" --lib "<索引库>"
uv run --with requests --with python-dotenv python "<技能目录>/scripts/build_segment_index.py" "<索引库>/小说原文/<小说名>-<作者>/" --lib "<索引库>" --skip-existing
```

`INDEX_EXECUTOR=direct` 是默认执行器；`subagent` 需要宿主提供 Agent/子代理能力。不要把 `.env`、API key 或实际索引库数据放入分发包。
