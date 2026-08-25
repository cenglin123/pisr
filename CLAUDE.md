# PISR 项目协作指南

## 项目定位

PISR（Pi Subagents Run）是以 headless `pi --mode json` 驱动异构、廉价或 fresh-context 子代理的治理型 skill，与 OCSR（opencode 后端）互补并存。`SKILL.md` 是运行规则的唯一事实源；`scripts/verify_pisr_skill.py` 为关键防护提供离线回归验证。

## 开始工作

1. 先读 [docs/CURRENT.md](docs/CURRENT.md)，确认当前任务与未决事项。
2. 再按 [docs/STRUCTURE.md](docs/STRUCTURE.md) 选择需要加载的专题文档。
3. 修改 `SKILL.md` 前，必须先读 [docs/overview.md](docs/overview.md) 与 [docs/pitfalls.md](docs/pitfalls.md)。
4. 复杂任务必须在 `docs/plans/active/` 建立计划；计划本身是跨会话交接协议。

## 硬约束

- 不得把 `--tools` 白名单、`-nc`/`-na`、prompt 禁令或路径审计描述成安全沙箱（pi 无内置权限系统）。
- 不得信任子代理的完成声明；以文件存在性、大小和抽样内容为验收证据。
- 不得静默扩大模型调用预算；派发前披露调用上限和模型，遵守 `SKILL.md` 的 3 次总尝试上限；驱动器不做任何自动重派。
- PISR 未接入 converge 预算门：不得在 converge 流程中作为 Spawn 通道使用（该场景走 OCSR）；不维护旁路计数。
- 不得手工伪造 reviewer、receipt、manifest 或遥测/账本记录。
- 文本文件统一 UTF-8 无 BOM、LF；不要把终端乱码直接判定为文件损坏。
- 修改行为契约（退出码、遥测 schema、白名单语义）必须同步 `tests/` 与 `refs/dispatch-patterns.md`。

## 修改与验证

- 只编辑 `AGENTS.md`；随后运行 `python scripts/agent_links.py repair` 同步 `CLAUDE.md` 和 `GEMINI.md`。
- 修改 PISR 行为规则后运行：`python scripts/verify_pisr_skill.py`。
- 修改文档体系后运行：`python scripts/agent_links.py check` 与 `python scripts/audit.py check`。
- 修改驱动器/运行器后运行：`pytest tests/ -q`（全部离线，不消耗模型调用）。
- 升级 pi 后重验 CLI 行为与事件流 schema（见 docs/deployment.md）。
- 治理规则变更需要独立视角复查；新增、删除或重定义准则段时，按独立审查强度规则处理。
- 提交前检查 `git diff --check` 和 `git ls-files --eol | Select-String "w/crlf"`。

## Git 工作流（统一走 PR squash）

**不直接向 `main` 提交或推送。** 一律：特性分支 → 推分支 → GitHub 上开 PR → **squash 合并**。

1. 开工前从最新 `main` 切分支：`git fetch origin && git checkout -b agent/<主题> origin/main`
2. 在分支上提交；推送：`git push -u origin agent/<主题>`
3. GitHub 上开 PR，用 **Squash and merge** 合入 `main`
4. **合并后本地 `main` 必须跟随远端，不要 merge**：
   ```
   git checkout main && git fetch origin && git reset --hard origin/main
   git branch -d agent/<主题> && git push origin --delete agent/<主题>
   ```

**第 4 步是这条约定的关键。** squash 合并会把整条分支压成一个**新提交**，
它与本地那些原始提交内容相同、提交对象不同。若本地 `main` 去 merge 而不是跟随远端，
两条历史就带着同一份内容分叉——三方合并会在同一片区域产生大量假冲突。

> 实证（ocsr 仓库 2026-08-10，本约定自此确立）：PR 被 squash 后本地 main 未跟随，
> 之后本地又在内容相同的提交之上继续开发，推送时分叉，直接 merge 在 4 个文件上冲突。
> 当时的处置是先证明「远端内容已全在本地历史」再用 `-s ours` 保留本方树。
> **该处置依赖于那次的特殊事实，不是通用解法**；按上面第 4 步做就不会走到这一步。

`-s ours` 只在能证明「对方内容已全部存在于本方历史」时才可用，且必须把证明写进合并说明。
证明不了就不要用——它会丢弃对方的改动。

> 例外：仓库初始化的首个 seed commit 允许直达 main（无 PR 基线可走）。

## 文档维护原则

- 只记录从代码和 Git 历史中无法直接读出的约束、原因、环境陷阱与当前状态。
- 同一事实只保留一个事实源；索引文档只导航，不复制正文。
- 新建文档前先判断能否归入现有文档；无独立长期职责的内容应合并。
- 过时文档应删除或更新，迁移历史交给 Git，不能保留"仅供历史追溯"的考古副本。
- 如无必要，勿增实体；优先采用可机械验证、可随项目演进的通用机制。

### docs/ 文件的治理规则

- 存在条件：文件必须承载稳定且独立的长期职责。
- 合并条件：内容短小、与现有文档同一读者或同一生命周期时合并。
- 创建原则：先在 [docs/STRUCTURE.md](docs/STRUCTURE.md) 登记定位与读取时机。
- 删除原则：确认入口、链接和当前计划同步更新后删除。
- 自免声明：本规则不要求为每次小改动创建新文档；轻量状态写入 `docs/CURRENT.md` 即可。

## 信息导航

- 文档总索引：[docs/STRUCTURE.md](docs/STRUCTURE.md)
- 当前状态与会话交接：[docs/CURRENT.md](docs/CURRENT.md)
- 系统主线与设计边界：[docs/overview.md](docs/overview.md)
- 本地运行与验证：[docs/deployment.md](docs/deployment.md)
- 环境和治理陷阱：[docs/pitfalls.md](docs/pitfalls.md)
- 文档一致性审计：[docs/audit-checklist.md](docs/audit-checklist.md)
- 变更记录：[docs/CHANGELOG.md](docs/CHANGELOG.md)
- 执行计划：docs/plans/（按需创建）

省略声明：本项目无网络 API 和服务部署流程，因此不维护 `docs/api.md`；`deployment.md` 仅记录本地依赖、调用与验证方式。

## 项目记忆

- 偏好中文沟通和证据驱动的工程交付。
- 项目上下文：PISR 用于通过廉价异构模型节省主模型 token、以硬工具白名单支撑只读评审；不要用宿主高成本子代理替代本应由 PISR 执行的工作。
- 分工记忆：需要 opencode 模型池/会话续接/converge Spawn 通道时用 OCSR；需要只读白名单/干净 fresh context/结构化遥测时用 PISR。
- 教训继承：失败不得表现为成功（退出码契约）；看门狗必须真有终止能力（按 PID）；launcher 转义与 pid 漂移类事故由 Popen 直启结构性消除。

## 完工检查

- 目标行为与文档事实源一致。
- 必要测试和文档审计通过（verify / agent_links / audit / pytest 四套全绿）。
- `CURRENT.md` 已更新；复杂任务计划状态准确。
- 治理文档已取得符合风险等级的独立复查证据。
- CHANGELOG 仅记录已完成、对后续维护有价值的变更。
- 未把用户现有改动、失败现场或证据误删。
