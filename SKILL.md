---
name: pisr
description: Use when host subagents cannot provide cross-vendor models, cheap parallel workers, or fresh-context adversarial reviewers, and a pi-backed second execution backend fits better than opencode — hard tool allowlists, clean fresh context, structured JSON telemetry. Drives headless `pi --mode json` as a framework-independent subagent backend. "pisr" is the PRIMARY trigger (any mention means use this skill); also trigger on "pi 子代理", "pi run 子代理", "pi 驱动多代理", "pi 并行评审", "只读 reviewer". NOT for simple tasks whose prompt cost exceeds the work, tasks needing shared conversational context, or critical judgments that cannot be verified afterwards.
---

# PISR — 默认派发入口

PISR（Pi Subagents Run）以 headless `pi --mode json` 派发可选模型的、一次性 fresh-context 子代理。它是执行后端：任务拆解、预算裁决、verdict 与最终验收仍由顶层 agent 负责。

与 OCSR（opencode 后端）互补并存，按场景分工：需要**硬工具白名单的只读评审**、**绝对干净 fresh context**、**结构化 usage 遥测**时选 pisr；需要 opencode 侧模型池或会话续跑（`--continue`/`--session`/`--fork`）时选 ocsr。

## 先判断是否派发

适用于需要跨厂商模型、低成本批量 worker、或独立 fresh-context 评审的可验证工作。首次在新 harness 使用时，**价值前提**是派发链路已在本机验证；此时主要价值是异构视角与上下文隔离，不应承诺净省 token。

不要派发单次简单工作（prompt 与回收成本超过工作本身）、需要频繁共享上下文的协作，或无法事后验收的关键判断。评审通常需要跨 family；纯确定性、很小的修复可用原生 executor。

子代理没有本对话的隐含约定；把缺失约束作为「上下文残差」写进 prompt。不要相信其"完成"自述，只相信指定文件的验收证据。

**converge 边界**：PISR 未接入 converge 预算门与证据链，**不得在 converge 流程中作为 Spawn 通道使用**（该场景仍走 ocsr 适配层）。

## 默认单 worker 闭环

### 1. 固定模型、调用上限与路径

可用 qualified ID（`provider/model` 二段）由用户可编辑的 [`config/allowed-models.json`](config/allowed-models.json) 唯一决定；仓库默认仅含 `cc-switch-xiaomi-mi-mo/mimo-v2.5` 与 `cc-switch-xiaomi-mi-mo/mimo-v2.5-pro`。修改该 JSON 后重新启动命令即可加载；它必须是非空、无重复、无首尾空白的 `provider/model` 字符串数组，格式错误会 fail-closed。`selftest` 未传 `--model` 时使用配置首项。首次使用先运行 `pi --list-models`，从目录表原样复制 provider 与 model 列；禁止凭记忆拼接或裸名猜测。再用 `python scripts/pisr_dispatch.py preflight --model <provider/model>` 验证选定模型可用性（目录检查零调用；真实探测会消耗一次模型调用）。模型角色资料见 [`refs/model-defaults.md`](refs/model-defaults.md)。注意 `pi --list-models` 目录**不含价格**，价格元数据可能缺失；事件流中的 usage/cost 字段为启发式风险信号，cost=0 不能单独证明模型免费或强弱。

派发前向用户披露模型与调用总上限；未经新鲜授权**不突破**该上限。每个 worker 最多 **3 次总尝试**。有副作用但不能证明幂等性的任务，**禁止自动重派**（驱动器自身也不做任何自动重派，重派决策归顶层 agent）。

为每个 worker 指定唯一、明确的绝对输出路径和唯一 label；`--output-pattern` 不会约束实际写入位置。`--tools` 白名单、`-nc`、`-na`、prompt 禁令和路径审计都**不是安全沙箱**：它们是工具面与协作约束，不能阻止恶意或失控进程访问/改写可访问位置（pi 无内置权限系统）。

### 2. 写自足 prompt（七要素）

每份 prompt 必须包含七项：

1. **任务**：一句话目标与可验证验收标准。
2. **输入**：允许读取的绝对路径；明确其他位置禁读。
3. **输出**：唯一绝对产物路径；优先使用 write 工具，若无 write 工具可回退到受控 shell 的 UTF-8 无 BOM 写入；未实际写入文件即失败。
4. **格式**：schema、模板或示例。
5. **边界与禁区**：不改输入、不写输出路径外；不确定术语保留原文并标 `[UNCERTAIN]`；知识截止可能早于今天；已确认术语不得"矫正"。
6. **执行证据**：返回产物完整路径、字节大小与工具调用情况。
7. **工具面声明**：本次派发的 `--tools` 集合及理由。产出型 worker 默认全工具；只读 reviewer 显式 `--tools read,grep,find,ls`（进程级硬白名单，模型不可调用 write/bash），并在 prompt 中说明"你只有只读工具"。reviewer 报告须含结构化 `reads:` 清单。

路径约束不是安全隔离。长 prompt 写入 UTF-8 文件，由驱动器经 `@file` 注入（无命令行转义问题）；Windows 读取中文一律显式 UTF-8。手工调用（不走驱动器）时的 PowerShell 陷阱：

| 环境 | 默认输出/重定向风险 | 建议 |
|---|---|---|
| PowerShell 5.1 | `*>` 可为 UTF-16LE | 显式 `Get-Content -Encoding UTF8`，写入显式 UTF-8 |
| PowerShell 7 | 默认 UTF-8 | 仍显式 UTF-8，保证一致 |

### 3. 派发与看护

默认使用驱动器：

```powershell
# 产出型 worker（全工具）
python scripts/pisr_dispatch.py dispatch --worker "<prompt-file>|<model>|<label>" --output-dir <dir> --output-pattern <unique-name> --watch

# 只读 reviewer（硬白名单）
python scripts/pisr_dispatch.py dispatch --worker "<prompt-file>|<model>|<label>" --tools read,grep,find,ls --output-dir <dir> --output-pattern <unique-name> --watch
```

驱动器负责错峰、看门狗、输出存在性/快照比对、事件流解析与 telemetry（`dispatch-log`），不替代编排判断。前台 timeout 足够时优先前台运行。派发基线为 `pi --mode json --no-session -nc -na`：不落会话文件、禁 context files、忽略项目本地资源（`-na` 是统一默认；`-a` 属例外须向用户披露）。

看门狗硬事实：默认 15 分钟，可按 `max(10 分钟, 1.5 × 实测耗时)` 调整；模型端静默停滞（进程存活 + 事件流 0 字节）须按完整指纹裁决。`harness 前台超时 < 单轮耗时` 时也不能以无限轮询代替看护。用至少 **≥5 次** 同类遥测样本再调整默认模型或阈值，不能以个例翻转默认。失败切换阶梯（同模型一次 → 切换 family 一次 → 停止交回用户）与通道例外（失败明确归因于通道时先修通道，不计"同模型重派"名额）的操作细则见 [`refs/dispatch-patterns.md`](refs/dispatch-patterns.md)。

驱动器会解析 `--mode json` 事件流做**工具越权审计**：实际 toolcall 超出 `--tools` 白名单即确定性失败（fail-closed）——它兜住 pi 配置漂移或扩展注入工具的场景；白名单本身的进程级约束由 pi 提供。

### 4. 回收并验收

依次检查：

1. 每个期望文件存在；
2. 每个文件非空；
3. 数量与期望一致；
4. 抽样打开 1–2 个文件，核对内容和格式。

任何一项失败都不采信"完成"回复。仅在无副作用或已证明幂等时重派，并把失败原因写入下一份 prompt；遵守三次总尝试上限。

## 按需加载的进阶专题

主文件是全局政策、边界和默认路径的唯一入口；下列文件仅在其委托范围内定义操作细节，不得放宽本文件不变量。

| 当你实际需要 | 读取唯一专题 | 该专题的闭环 |
|---|---|---|
| 失败看护、脱管、失败切换阶梯、并发扇出 | [`refs/dispatch-patterns.md`](refs/dispatch-patterns.md) | 启动/观察、看门狗、退出码契约、回收与终止前置检查 |
| fresh 对抗评审 | [`refs/failure-modes.md`](refs/failure-modes.md) | 输入最小化、布局隔离、禁读、`reads:` 审计、作废或新会话重派 |
| 模型角色分工 | [`refs/model-defaults.md`](refs/model-defaults.md) | 按角色选档；白名单唯一决定 |
| 多步骤、路由、断点续跑 | [`refs/run-spec.md`](refs/run-spec.md) | schema、journal 与确定性路由；仅已成功提取却未命中具名 route 的值经 `"*"` 进入 `pause`，契约失败 fail-closed |
| 层级指挥 | [`refs/hierarchical-command.md`](refs/hierarchical-command.md) | 归属、状态、看护和独立验收 |
| 发布 executor | [`refs/release-executor.md`](refs/release-executor.md) | 输入合同、manifest 与保护默认 |
| 陷阱完整表 | [`refs/pitfalls-reference.md`](refs/pitfalls-reference.md) | 事实/对策速查 |

对抗评审的最小规范：只给完成审查所需的输入；把被审产物与 reviewer 输出隔离；要求结构化 `reads:`；审计发现提前获得答案或作弊性读取时，verdict 默认作废并以新会话重评。具体布局、禁读清单、审计裁定及例外都在 `refs/failure-modes.md`。只读 reviewer 的推荐工具面是 `read,grep,find,ls`（本机 pi 0.84.3 实证：模型无法调用 write，写入请求被拒且零产物）。

`run --spec` 只搬运确定性步骤，不写 prompt、不判 verdict。其 schema、步骤类型、模板、journal 或提取契约失败均 fail-closed；只有已成功提取的值未命中具名 route，才会经必填 `"*"` pause 交回 agent。

## 维护与验证

权威实现为 `scripts/pisr_dispatch.py`。常规验证：

```powershell
python scripts/verify_pisr_skill.py
python scripts/agent_links.py check
python scripts/audit.py check
pytest tests/ -q
```

升级 pi 后重验 `-p`/`--mode json`/`--tools`/`-nc`/`@file` 行为（`selftest` 覆盖主链路）。未经重复、可观察的失误证据，不新增运行时机制。完整背景、编码与诊断资料按上表按需读取。
