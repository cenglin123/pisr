# 仓库出生记录（initialization）

- 出生方式：从 OCSR（`github.com/cenglin123/ocsr`）对等移植初始化，后端由 opencode 换为 pi。
- 出生时间：2026-08-26。
- 初始化计划：`docs/plans/active/20260826-pisr-bootstrap.md`（T1–T12 全部完成）。
- 设计文档：`D:\project\pisr\docs\superpowers\specs\2026-08-26-pisr-skill-design.md`（会话工作目录，不入库）。
- 重建边界：本仓库自首个 commit 起即可完整自证——SKILL.md、scripts/、refs/、docs/、tests/ 均在 Git 历史内；无外部未入库依赖（Python 3 标准库 + pi CLI）。
- 首个 seed commit 直达 main（AGENTS.md Git 工作流的初始化例外）；此后一切变更走 feature branch + PR squash。
