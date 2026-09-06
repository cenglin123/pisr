# PISR 当前状态与会话交接

> 每个新会话首先读取本文件；完成后更新本文件。

## 当前任务

仓库初始化已完成（见 [docs/plans/active/20260826-pisr-bootstrap.md](plans/active/20260826-pisr-bootstrap.md)）。

- [x] T1–T9 骨架 / 驱动器 / run_spec / verify / tests / SKILL+refs+docs / 离线验证全绿 / 首个 commit
- [x] T10 在线冒烟：preflight + selftest --model + 产出型 worker + 只读 reviewer
- [x] T11 部署 ~/.claude/skills/pisr + GitHub cenglin123/pisr
- [x] T12 .memory INDEX 记录

2026-09-06 在途（分支 agent/reviewer-bash-tool-face）：reviewer 工具面解冻（[计划](plans/active/20260906-reviewer-bash-tool-face.md)）——规则文档已落地、独立复查 PASS（零阻断）、bash 档在线冒烟通过；待提交。

## 环境基线

- pi 0.84.3；默认白名单：`xiaomi/mimo-v2.5`、`xiaomi/mimo-v2.5-pro`、`minimax-cn/MiniMax-M3`、`deepseek/deepseek-v4-flash`（高质量 verdict 档：MiniMax-M3 ↔ deepseek-v4-flash 异源双视角）
- 实证（2026-08-26）：`@file` prompt 注入 ✅；`--tools read,grep,find,ls` 硬白名单（模型无 write、零产物）✅；`--mode json` 事件流含 usage/toolcall ✅
- 实证（2026-09-06）：运行时验证档 `--tools read,grep,find,ls,bash` 在线冒烟 ✅——bash 工具可用；事件流 `toolName:"bash"`，越权审计正确计量；`--capture-reply` 组合正常
- 通道状态（2026-09-06）：`minimax-cn` 无 API key（auth 失败，未消耗调用）；`deepseek` 可用

## 本轮新增能力（冒烟中固化）

- `--capture-reply`：只读 reviewer 的产物回收模式——驱动器把事件流最终回复机械落盘为产物（exit≠0/空回复不落盘；越权审计与 reads: 审计优先于落盘）。SKILL.md 七要素第 7 项与 refs 已同步。

## reviewer 工具面解冻（2026-09-06）

- reviewer `--tools` 解冻为档位制：默认 `read,grep,find,ls`；审计任务需运行时验证（跑测试/lint 等只读命令佐证）时可加 `bash`——write 仍禁（生成/评估分离），加入 bash 后写入在进程级可达，只读性靠 prompt 钉死 + 报告列明命令与退出码 + 事后审计。SKILL.md 七要素第 7 项、派发示例、refs/failure-modes.md、refs/dispatch-patterns.md、refs/hierarchical-command.md 验收环、refs/model-defaults.md 角色表已同步；驱动器/tests/遥测 schema/退出码契约零改动。
- 独立复查（deepseek-v4-flash 只读 reviewer，PISR 自驱）：verdict=PASS 零阻断，3 条非阻断建议已采纳落地。

## PR-A 防御性加固（2026-08-26）

- [x] schema 漂移报警（`error:schema_drift_suspect`，评审修正判定条件）
- [x] stub-pi e2e 集成测试（4 用例，真实 subprocess 链路零模型调用）
- [x] dispatch-patterns.md 孤儿 worker 处置小节
- 证据门控后置：verify-artifacts/retry 模板（先 checklist 实测）、preflight 缓存（已否决）、converge 集成（独立立项）

## 未决事项

- （无；后续特性走 feature branch + PR squash）
