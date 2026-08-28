# PISR 当前状态与会话交接

> 每个新会话首先读取本文件；完成后更新本文件。

## 当前任务

仓库初始化已完成（见 [docs/plans/active/20260826-pisr-bootstrap.md](plans/active/20260826-pisr-bootstrap.md)）。

- [x] T1–T9 骨架 / 驱动器 / run_spec / verify / tests / SKILL+refs+docs / 离线验证全绿 / 首个 commit
- [x] T10 在线冒烟：preflight + selftest --model + 产出型 worker + 只读 reviewer
- [x] T11 部署 ~/.claude/skills/pisr + GitHub cenglin123/pisr
- [x] T12 .memory INDEX 记录

## 环境基线

- pi 0.84.3；默认白名单：`cc-switch-xiaomi-mi-mo/mimo-v2.5`、`cc-switch-xiaomi-mi-mo/mimo-v2.5-pro`
- 实证（2026-08-26）：`@file` prompt 注入 ✅；`--tools read,grep,find,ls` 硬白名单（模型无 write、零产物）✅；`--mode json` 事件流含 usage/toolcall ✅

## 本轮新增能力（冒烟中固化）

- `--capture-reply`：只读 reviewer 的产物回收模式——驱动器把事件流最终回复机械落盘为产物（exit≠0/空回复不落盘；越权审计与 reads: 审计优先于落盘）。SKILL.md 七要素第 7 项与 refs 已同步。

## PR-A 防御性加固（2026-08-26）

- [x] schema 漂移报警（`error:schema_drift_suspect`，评审修正判定条件）
- [x] stub-pi e2e 集成测试（4 用例，真实 subprocess 链路零模型调用）
- [x] dispatch-patterns.md 孤儿 worker 处置小节
- 证据门控后置：verify-artifacts/retry 模板（先 checklist 实测）、preflight 缓存（已否决）、converge 集成（独立立项）

## 未决事项

- （无；后续特性走 feature branch + PR squash）
