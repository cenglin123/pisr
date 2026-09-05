# 模型默认池与分工 — 详细参考

> 本表由 [SKILL.md 的模型选择入口](../SKILL.md) 按需加载（数据层，随模型换代更新）。主文件保留选模型规则、遥测与翻转门槛。
>
> PISR 的可用模型由仓库根目录的 [`config/allowed-models.json`](../config/allowed-models.json) 唯一决定。默认值从本机 `pi --list-models` 目录原样照抄：`xiaomi/mimo-v2.5`、`xiaomi/mimo-v2.5-pro`、`minimax-cn/MiniMax-M3`、`deepseek/deepseek-v4-flash`；用户可修改该非空、无重复的 JSON 字符串数组，下一次命令启动时加载。未列出的模型不可选。

下表仅描述**仓库默认配置**下的角色分工；用户替换白名单后，必须选择当前配置中存在且已通过 preflight 的模型，不能沿用表中的已移除 ID。

按**角色**分工，不按任务表面难度：

| 角色 | 默认模型 | cost 资料来源 | 说明 |
|------|---------|---|---|
| 批量执行 worker（机械杂活：转换、摘要、矫正、抽取） | `xiaomi/mimo-v2.5` | `pi --list-models` 目录不含价格；以事件流 usage 实测记账 | MiMo 轻量档，配合 [SKILL.md 的自足 prompt、验收与止损](../SKILL.md)使用 |
| 只读 reviewer（fresh 对抗评审、验收核查） | `xiaomi/mimo-v2.5` + `--tools read,grep,find,ls` | 同上 | 工具面硬白名单是 PISR 的核心增益 |
| 常规 verdict / 中间档（语义审查、verdict、meta 判断） | `xiaomi/mimo-v2.5-pro` | 同上 | MiMo 同 family 升级档；需要跨 family 异构视角时降级或升级到下面两档 |
| **高质量 verdict**（meta-judge、跨 family 异构评议、需要"双强模型对抗"的高 stakes 场景） | `minimax-cn/MiniMax-M3` ↔ `deepseek/deepseek-v4-flash` | 同上 | 异源双视角（minimax-cn vs deepseek），单 family 时优先 MiniMax-M3，需要对抗时二选一并行；与"廉价"默认池定位不同，属高成本档，慎用作大批量 worker |

提醒：OCSR 默认池与 PISR 默认池各自独立配置，互不引用。
