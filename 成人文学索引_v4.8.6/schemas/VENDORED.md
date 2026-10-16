# schemas/ 受控副本说明（VENDORED）

`scripts/style_card_schema.py` 的查找顺序支持"技能仓库内 `schemas/style-card.schema.json`"
（发行包可自行内置一份）。独立安装的包没有柜根同级目录，不内置则风格卡写盘自校验必抛错，
因此随包带一份受控副本。

| 项目 | 事实 |
|---|---|
| 副本 | `schemas/style-card.schema.json`（5986 字节） |
| 来源 | 技能柜共享内核 `schemas/style-card.schema.json`（W/schemas，2026-10-11 字节） |
| 副本 sha256 | `e51f0bc4ca7f7da7994f77222b171997a03592286f6269dedbab006593e4cf4f` |
| 与来源是否逐字节一致 | 是（复制时校验） |
| 同步规则 | 上游变更须同步本副本并更新本文件登记的摘要；`scripts/style_card_schema.py` 的三级查找顺序不变 |
| 许可 | 随主干许可，见 `LICENSE` 第二节 |
