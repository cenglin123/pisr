# 层级指挥模式（orchestrator 无头运行）— 详细参考

> 本文件由 [SKILL.md 的层级指挥入口](../SKILL.md) 按需加载。主文件只保留条件入口；本文件承载完整协议：detached 派发、state 文件 schema、monitor 配套、路径 B 续接、commander 裁决点、验收环、调研二分、跨 Phase 接口契约、verify-ownership。

> **层级指挥**：planner（顶层规划模型）探索全貌、写 plan、终验签字；orchestrator（pro 模型）循环控制 + verdict 裁决 + 任务卡；worker（执行档）承接全部写作类交付物。三层是默认形态而非定数——小任务可塌缩，超大任务可长出第四层。
> **commander**（操作裁决者）与 planner 是同一角色在不同阶段的两个职责面：planner 负责写 plan 与终验；同一角色在中断/预算续接场景下以 commander 身份做操作裁决。
> **触发词**：「层级指挥 `<任务>`」= 启动本节全套流程；与单次派发「pisr 派 `<任务>`」区分。
> **与 converge「层级收敛」区分**：层级收敛是 planner→多个 orchestrator 并行子收敛；层级指挥是任务级的角色分层。**PISR 未接入 converge 预算门**——在 converge 流程中作 Spawn 通道的场景仍走 OCSR。

### detached 派发

编排者由上级（planner）经 pisr_dispatch 无头派发（--watch 或长看门狗）。巡航监控用 `pisr_dispatch.py monitor`（脚本层，框架无关）：持续模式盯 active 目录新鲜度 + 进程存活；`--once` 单次检查可挂任何外部调度器。orchestrator 自身不维持会话上下文，一切状态写入 _orchestrator-state.md / _phase-report.md。

代码类 executor 的任务卡验收须包含**真实数据源自测**：不只跑夹具测试，还要用真实输入实跑目标命令并记录退出码与输出摘要。

### state 文件最小 schema

#### _orchestrator-state.md

必需字段：

| 字段 | 类型 | 说明 |
|------|------|------|
| `current_phase` | string | 当前 Phase 编号，如 `Phase 1` |
| `started` | ISO 8601 | Phase 启动时间戳 |

交付物归属表（6 列）：

| Phase | Deliverable | File | Owner | Spawn Label | Status |
|-------|-------------|------|-------|-------------|--------|
| Phase 1 | 评审报告 | rounds/round-1.md | <configured-reviewer-model> | spawned:r1-reviewer | done |

`Spawn Label` 取值 `spawned:<label>`（对应 ledger 中的派发标签）或 `self-written`。`Status` 取值 `done`、`failed` 或 `done (attempts=N, 原因摘要)`。

spawn budget 计数器：`spawn_budget.total` / `spawn_budget.used` / `spawn_budget.remaining`。

#### _phase-report.md

按 Phase 追加记录：started/completed、spawn_budget、deliverables（path → status (spawned:label)）。

### monitor 配套

```bash
python scripts/pisr_dispatch.py monitor --process-name node.exe --watch-dir <output-dir> --stall-minutes 15 --once
```

`--once` 退出码：0=正常, 1=停滞/进程死亡/目录不可访问（合并语义，查 stderr 详情区分）。注意 node.exe 是 pi 与一切 Node 进程的宿主，仅用于"完全没有任何 node 进程"的兜底判断；精确监视优先 `--watch-dir`。

### 路径 B 续接协议

中断后 commander 发起 fresh orchestrator + resume 任务卡：

1. commander 读取 _orchestrator-state.md / _phase-report.md 确定当前 Phase
2. 换 family 派 fresh orchestrator
3. resume 任务卡骨架：指定 target-phase=N、复述 remaining deliverables、引用状态文件
4. orchestrator 续接时先读 state → 确认从哪开始 → 继续

### commander 裁决点操作规程

- **换模型（换 family / 升档）**：
  1. 查 `pi --list-models` → 按 provider 分组 → 排除当前 family → 选替代模型（须在白名单）
  2. 评估失败模式是否模型相关：同错误重复出现 → 模型能力不足；间歇性 → API 抖动
- **改 brief**：
  1. 失败指纹判定：两轮同模型失败且错误不同 → **brief 缺陷**；错误相同 → **模型能力不足**
  2. 修订 brief：补充缺失路径/禁用清单/术语表后重派
- **终止**：
  - 区分：每 worker 3 次总尝试（SKILL.md 硬停止条件）vs orchestrator 级整体预算
  - 达到上限 / 预算耗尽 / 方向性设计需用户拍板 → 终止并上报

### 验收环

orchestrator 各 Phase 完成后、向 planner 汇报前，必须派**非 executor 族** acceptance-reviewer（与当次执行 executor 不同 family 即可；只读场景叠加 `--tools read,grep,find,ls`），任务是执行确定性验收命令（pytest / CLI / 真实数据源），不是读报告写意见。

- 修复循环由 orchestrator 管理；反复失败直至重试上限、或 verdict=需重新设计时，才升级 planner 介入
- planner 终验 = 证据链核验：复跑核心测试 + 审查 verdict 链 + 机械校验（verify-ownership） + 抽查关键产物

**设计规则——机制兜底优先**：能用防呆机制机械兜底的问题（退出码、schema、归属校验、工具越权审计）由机制兜底；机制兜不住的（幻觉、语义偏差）由独立审计（非族 reviewer）兜底。

### 调研二分

orchestrator 必须亲自读关键一手材料——verdict 裁决是第一手判断，输入验证是它的本职；verdict 裁决所依赖的源码/配置/状态文件为关键一手材料（必亲自读），纯信息收集型读取可委托。不得以"读了摘要"替代对关键源码的亲自阅读。

### 跨 Phase 接口契约

跨 Phase 的数据契约由 planner 在 plan 中定义并签署（「接口 spec」小节）；schema 必须先于实现。未签署前禁止进入 Phase 实现；仅接受 planner/commander 的显式 amendment，不接受 orchestrator 自签。

### verify-ownership（归属遥测机械校验）

```bash
python scripts/pisr_dispatch.py verify-ownership \
  --state <_orchestrator-state.md> --ledger <ledger.jsonl> --repo <path>
```

三查：完整性（git 改动文件都在归属表中）/ 一致性（归属表中的 spawned label 在 ledger 中有记录）/ 合理性（mtime 窗口启发式，不阻断）。退出码：0=全通过, 1=缺漏/虚报, 2=参数/文件错误。

> 传 `--ledger-dir <目录>` 派发时账本才会落盘；否则 verify-ownership 回退全局遥测，合理性检查降级。
