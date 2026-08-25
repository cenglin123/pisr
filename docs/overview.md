# PISR 系统主线与设计边界

## 目标

PISR 解决宿主框架内建子代理不能跨厂商换模型、机械任务使用昂贵主模型、评审上下文受主对话污染的问题。它以一次性 headless `pi --mode json` 调用派发自包含任务，以文件系统证据回收产物。相对 OCSR（opencode 后端）的差异化价值：进程级工具白名单（只读评审）、更干净的 fresh context（`-nc -na --no-session`）、结构化 usage/toolcall 遥测。

## 核心设计决策

1. **PISR 是执行后端，不是工作流引擎。** 收敛判定、预算门、轮次和归档由上层流程负责。
2. **默认一次投递、文件回收。** pi 会话续接（`-c/--session/--fork`）是交互能力，不作为派发协议；需要续接的场景归 OCSR。
3. **残差注入优先于上下文搬运。** Prompt 必须明确任务、输入、输出、格式、边界、执行证据与工具面（七要素）。
4. **确定性证据优先于代理自述。** 文件存在、非空、数量和抽样内容共同构成验收；事件流 usage/toolcall 是辅助遥测。
5. **保持文档型实现。** 权威实现为一次性 CLI 工具 `scripts/pisr_dispatch.py`（Python 3 标准库），无常驻 daemon、无通用 runner 野心（`run --spec` 为确定性步骤搬运器，继承 OCSR 同名设计的边界与治理约束：hook 可执行任意声明命令、不是安全沙箱、步骤类型封闭四种）。
6. **执行层直启而非 launcher 链。** Popen 直调 pi（`@file` 注入 prompt、stdout 落盘 events.jsonl），消灭 PowerShell launcher/marker/pid 文件的转义与状态漂移面；这是对 OCSR 实证事故（launcher 路径转义、pid 不刷新）的结构性规避，不是偷懒——修改前先读 `docs/pitfalls.md`。

## 与 OCSR 的关系

互补并存、独立演进、按场景分工（详见 `refs/dispatch-patterns.md` §通道选择判据）。两仓库共享角色 enum、退出码契约、遥测 schema（pisr 扩展 usage_*/tool_* 字段）与治理方法论；不共享代码与隐式语义。**PISR 未接入 converge 预算门**：converge Spawn 通道仍走 OCSR 适配层，本仓库不维护旁路计数。

## 角色边界

- 顶层 orchestrator 负责拆解、预算披露、prompt 残差、产物验收和跨 worker 汇总。
- PISR worker 只处理被授予的自包含任务，不拥有上层计划状态。
- Reviewer 必须是与作者上下文隔离的真实新进程；不能由 orchestrator inline 代写后标记为 fresh。
- 驱动器不做任何自动重派；三次总尝试上限由顶层 agent 执行。

## 非目标

- 不提供文件系统或网络安全隔离（`--tools`/`-nc`/`-na` 是工具面与输入面约束，不是沙箱）。
- 不提供常驻 daemon、通用 runner 或跨模型共享内存。
- 不替代宿主对副作用操作的授权和安全控制。
- 不把模型输出质量转化为无需复核的信任承诺。
