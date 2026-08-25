# 模式 D · 释放/收口执行型（release / closeout executor）— 详细参考

> 本文件由 [SKILL.md 的发布 executor 入口](../SKILL.md) 按需加载；本文件承载 release executor 的完整职责、输入合同、输出 schema 与分工。

### 模式 D · 释放/收口执行型（release / closeout executor）

适合：功能实现或收敛循环产出可发布终态后的文档、归档、清理、staging 收尾——把这类默认可结构化的收尾工作从昂贵主 orchestrator 转移到便宜执行档模型。

**规模边界**：面向批量、长收尾、成本敏感的收口工作；单文件/几行的小规模任务不走模式 D——用框架原生 executor（判据见 [SKILL.md 的"先判断是否派发"](../SKILL.md)）。

**输入合同**（在 [SKILL.md 的自足 prompt 七要素](../SKILL.md)之上，release executor 额外需要以下残差）：

- 最终 diff 或 `changed_files` 清单
- 测试摘要（命令 + exit_code + 证据路径）
- 任务合同（验收标准 / 允许路径）
- 归档状态（当前 active/done 位置、Archive Contract 是否已 `valid-v1`）
- `allowed_paths`（本任务允许改动的路径前缀清单——release executor 产出的 manifest 中任何路径都必须落在此范围内，越界即被下方门禁拒绝）

**默认职责**：

1. 在 manifest 中以 `doc_updates` 字段**提议**对 CURRENT / CHANGELOG / bugfix 类文档的描述性变更；release executor **不直接修改**产品文件或文档，只产出 manifest；
2. 生成收敛 retrospective 并记入 `retrospective_path`；
3. 检查报告中的数字与实际测试结果是否一致，记入 `digit_consistency_check`；
4. 生成精确 cleanup manifest（拟删除的临时/中间产物，逐条给出路径 + reason）；
5. 生成 staging manifest（拟提交/归档的最终产物，逐条给出路径 + reason）；
6. 报告受保护路径和未清理项。

**输出**：一份 JSON manifest，write 直写到指定路径，字段包含 `status` / `allowed_paths` / `doc_updates` / `retrospective_path` / `digit_consistency_check` / `cleanup_manifest` / `staging_manifest` / `protected_paths_reported` / `unclean_items_reported`。`doc_updates` 及类似字段只描述拟执行变更；release executor 不自行执行删除、修改或归档操作。

**与上层驱动器的分工**（本 skill 不重复实现机械校验；权威源另在宿主侧）：

- manifest 的 JSON schema，以及"凭据模式 / `allowed_paths` 越界 / 外部临时目录 / 删除项存在性与保护路径"四项最少机械检查，由调用方执行并 fail closed——本 skill 只负责让 release executor 产出符合该 schema 的 manifest。
- release executor **只生成** manifest，**不自行执行删除**；删除动作由通过四项检查后的调用方按 manifest 精确执行。校验通过前，manifest 视同未验证产物，适用 [SKILL.md 的"不采信完成自述"](../SKILL.md)。

**保护默认**：凭据文件（`.env*`、`*.pem`、路径含 `secret`/`credential`/`api_key`/`sk-` 字面量）与其他任务的临时/收敛目录默认视为不可删除项，不得出现在 `cleanup_manifest` 中——即使看起来像"垃圾文件"。

**模型选择**：仓库默认配置中，机械收尾（文档同步、manifest 生成、数字核对）用执行档 `cc-switch-xiaomi-mi-mo/mimo-v2.5`（见 [模型默认池](model-defaults.md)）；涉及需要判断的一致性核验按判断密集角色升级。PISR 加成：release executor 属产物型 worker（要写 manifest），**不要**加只读白名单；如需限制其读取面，用 `--forbid-paths`。
