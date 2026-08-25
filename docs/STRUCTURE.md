# 文档总索引

按任务需要读取，不要一次性加载全部文档。

| 需要了解 | 文档 | 读取时机 |
|---|---|---|
| 当前工作与交接 | [CURRENT.md](CURRENT.md) | 每个新会话首先读取 |
| PISR 的目标与边界 | [overview.md](overview.md) | 设计或修改 PISR 行为前 |
| 本地运行和验证 | [deployment.md](deployment.md) | 执行命令、排查环境时 |
| 已知陷阱与失败模式 | [pitfalls.md](pitfalls.md)、[../refs/](../refs/) | 派发、审查或故障处理前 |
| 派发模式 / 失败模式 / 模型默认池 / run-spec / 层级指挥 / release executor | [../refs/](../refs/)（dispatch-patterns、failure-modes、model-defaults、run-spec、hierarchical-command、release-executor 等） | SKILL.md 主文件指向具体场景时 |
| 文档审计 | [audit-checklist.md](audit-checklist.md) | 文档体系变更或定期审计时 |
| 已完成变更 | [CHANGELOG.md](CHANGELOG.md) | 需要近期历史时；优先用脚本读取 |
| 仓库起点记录 | [initialization.md](initialization.md) | 需要了解仓库重建边界时 |
| 执行计划 | plans/（按需创建） | 分阶段、跨会话或协作任务时 |

`.converge/` 类流程证据区（如引入）为本地运行产物，gitignored 不入库，不是日常文档入口。
