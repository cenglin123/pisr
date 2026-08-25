# 模型默认池与分工 — 详细参考

> 本表由 [SKILL.md 的模型选择入口](../SKILL.md) 按需加载（数据层，随模型换代更新）。主文件保留选模型规则、遥测与翻转门槛。
>
> PISR 的可用模型由仓库根目录的 [`config/allowed-models.json`](../config/allowed-models.json) 唯一决定。默认值是 `cc-switch-xiaomi-mi-mo/mimo-v2.5` 与 `cc-switch-xiaomi-mi-mo/mimo-v2.5-pro`（与 OCSR 默认池同源，经 cc-switch 网关的 custom provider）；用户可修改该非空、无重复的 JSON 字符串数组，下一次命令启动时加载。未列出的模型不可选。

下表仅描述**仓库默认配置**下的角色分工；用户替换白名单后，必须选择当前配置中存在且已通过 preflight 的模型，不能沿用表中的已移除 ID。

按**角色**分工，不按任务表面难度：

| 角色 | 默认模型 | cost 资料来源 | 说明 |
|------|---------|---|------|
| 批量执行 worker（机械杂活：转换、摘要、矫正、抽取） | `cc-switch-xiaomi-mi-mo/mimo-v2.5` | `pi --list-models` 目录不含价格；以事件流 usage 实测记账 | MiMo 轻量档，配合 [SKILL.md 的自足 prompt、验收与止损](../SKILL.md)使用 |
| 只读 reviewer（fresh 对抗评审、验收核查） | `cc-switch-xiaomi-mi-mo/mimo-v2.5` + `--tools read,grep,find,ls` | 同上 | 工具面硬白名单是 PISR 的核心增益 |
| 判断密集角色（verdict、语义审查、meta 判断） | `cc-switch-xiaomi-mi-mo/mimo-v2.5-pro` | 同上 | 默认池只含 MiMo 单 family，不能伪装成跨 family 异构评议；需要异构视角时先由用户将经验证的模型加入配置，再 `pi --list-models` 核对后使用 |

提醒：OCSR 默认池的 `xiaomi/mimo-v2.5` 与 PISR 默认池的 `cc-switch-xiaomi-mi-mo/mimo-v2.5` 是**同一网关的不同接入命名**（opencode 侧 vs pi 侧 provider 名）；两池各自独立配置，互不引用。
