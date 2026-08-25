# 派发操作模板与通道选择判据 — 详细参考

> 本文件由 [`SKILL.md`](../SKILL.md) 委托，收纳并发、脱管、失败看护与可复制命令的详细操作；不得放宽主文件的预算、证据、重派上限或"非安全沙箱"边界。

## 通道选择判据（PISR / OCSR / 框架原生子代理）

**通道选择判据**（某角色用 PISR、OCSR 还是框架原生子代理）：不枚举任务类型，按通用原则判断——(a) **角色的价值来源**：评审的价值在跨 family 覆盖与上下文隔离；需要**只读硬约束**（防止 reviewer 意外改写现场）时 PISR 的 `--tools read,grep,find,ls` 是唯一提供进程级工具面白名单的通道；需要 opencode 侧模型池或会话续跑时走 OCSR。(b) **协议成本与模型成本的双侧账本**：自足 prompt + 产物回收 + 看门狗是固定协议开销，任务越小占比越高；临界点无固定值，以本文件 `dispatch-log` 遥测（`wall_min` / `artifact_bytes` / `usage_total_tokens`）校准。判别样例（直觉参考，非决策规则）：几行的文字修复用原生 executor；批量转换、长收尾、成本敏感场景用 PISR；需要 opencode 模型池的评审用 OCSR。

## 退出码契约（`pisr_dispatch.py dispatch --watch`）

> **单一事实源。** 本节是 `dispatch` 退出码语义的权威定义，服务一切调用方。
> 实现见 `scripts/pisr_dispatch.py` 的 `EXIT_*` 常量与 `_watch_loop` 收口段。
> 修改本节须同步实现与 `tests/test_execution_layer_integrity.py`。

| 码 | 含义 | 判据 |
|---|---|---|
| `0` | 全部 worker 落盘 | 每个 worker 的期望产物存在、非 0 字节（预存文件还须内容变化），且工具越权审计 clean |
| `1` | 看门狗超时 | 至少一个 worker 到达自己的 deadline 仍未结案 |
| `2` | 确定性失败 | 至少一个 worker 确定性失败：spawn 失败 / pi 非零退出 / **pi 退出码为 0 但期望产物未落盘** / 工具越权审计命中 |
| `3` | 路径碰撞 | 派发前后快照比对发现既有文件被非预期覆盖 |

**混合结局优先级**（同一次调用同时出现多种结局时返回的码）：

```
路径碰撞(3) > 看门狗超时(1) > 确定性失败(2) > 全部成功(0)
```

排序理由：3/1 为既有语义（继承 OCSR），保持最高优先级不变；确定性失败(2) 是**已结案的失败**，排在**未结案失联**(1) 之后——避免已记录在案的失败掩盖仍在消耗预算的失联进程。

**`exit=0 且零产物` 为何算失败**：pi 正常退出但期望产物没落盘，是 [`failure-modes.md` 的"越界写入 / 路径碰撞"`](failure-modes.md#越界写入--路径碰撞)的典型指纹（子代理自选文件名写到了别处），或模型拒绝/未能写入（如只读工具面下要求写文件）。该情形的遥测 `outcome_detail` 为 `error:exit_0_no_artifact`，驱动器会附带模型末段文本辅助归因；疑似改名落盘时为 `error:exit_0_name_mismatch`（产物或已有效落盘，勿按 0 产物重派）。

**未启用 `--watch` 时不适用**：不做产物回收与路径碰撞检测，仅记 launched 并返回 0。

## fresh 对抗评审

评审的布局隔离、`--forbid-paths` 注入、`reads:` 审计及作废/新会话重派，完整且唯一地定义在 [`failure-modes.md`](failure-modes.md#fresh-对抗评审完整闭环)。本文件不重复该规则。PISR 加成：reviewer 通道默认 `--tools read,grep,find,ls`，写入在进程级不可达——布局污染的风险面只剩"读了不该读的"，由 `reads:` 审计兜底。

## 失败看护与切换

### 静默停滞、阈值与终止

**静默停滞**仅在三条同时满足时成立：pi 进程仍存活、事件流（events.jsonl）为 0 字节、已超过看门狗阈值。三条缺一不可：进程已被终止则是通道 kill 指纹，不是静默停滞。

每个后台进程必须设硬阈值：有本机实测时为 `max(10 分钟, 1.5 × 该模型该角色实测单轮耗时)`；无样本时默认 15 分钟；单一大规模长任务可设 60 分钟。至少积累 5 次同类遥测样本才可调整默认模型或阈值。阈值到期后按 `--timeout-policy` 的解析结果处理（`leaf_kill` = taskkill /F /T /PID 进程树；`hierarchical_report` = 报告/alive 留 commander 裁决），并如实记录；不得以无限轮询代替看护。

手动终止前必须逐项记录：已达到该进程阈值、事件流尾部不显示正在执行、以及已评估中断副作用。只按目标 PID 终止，禁止按镜像名批量杀（node.exe 是 pi 与一切 Node 进程的宿主，`/IM node.exe` 会杀死无辜进程）；中断后扫描残留并实跑项目验证。

### 失败切换阶梯

驱动器**不做任何自动重派**（pi 无共享会话 DB，无通道级锁重试需求）。仅对无副作用或已证明幂等的 worker，且三次总尝试上限内，由顶层 agent 执行：

1. 第 1 次失败：同模型重派一次，以排除偶发 API 抖动。
2. 第 2 次失败：第 3 次尝试切换到不同 `family`；先以 `pi --list-models` 和 `preflight --model <provider/model>` 验证目标，再把前两次失败原因写进新 prompt 的"边界与禁区"。
3. 第 3 次失败：停止、保留失败日志和产物证据，交回用户选择换模型、缩小任务、提高预算或终止。

若失败明确归因于通道（如 pi CLI 升级后行为漂移、provider 网关 5xx），先修通道而不换模型；这次修复仍计入三次总尝试上限，但不计作"同模型重派一次"（通道例外）。不得借通道问题无限重试。

## 脱管派发模式（前台超时不够用时）

> **入口条件**：当 `scripts/pisr_dispatch.py` 可用时优先用驱动器 `dispatch --watch`；仅当驱动器不可用时回退本节手写模式。

当 harness 前台 shell 工具的超时上限**小于**预计单轮耗时（判断密集角色单轮常 20–30 分钟，多数 harness 前台上限 ≤10 分钟）时，用**脱管派发**：让 pi 脱离 harness 任务生命周期独立运行，harness 侧只跑纯 shell 观察器等产物。

三步模板（驱动器内部即此结构：Popen 直启 + stdout 重定向 + 轮询观察）：

1. 把命令写入启动脚本（prompt 经 `@file` 注入，无转义问题）：
   ```bash
   # run-worker.sh（或直接 Popen）
   pi --mode json --no-session -nc -na \
      --provider <provider> --model <model> \
      [@]"C:/path/prompt.txt" > "C:/work/events.jsonl" 2> "C:/work/stderr.log"
   ```
2. 脱离 harness 生命周期启动（`Start-Process` / `nohup` / Popen，不挂 harness 后台通道）。
3. harness 侧跑纯 shell 观察器，**双监视**（产物落盘 **且** pi 进程存活）：
   ```bash
   until [ -f artifact.md ]; do
     if ! kill -0 $PID 2>/dev/null && [ ! -f artifact.md ]; then
        echo "pi exited WITHOUT artifact → 确定性失败"; exit 1
     fi
     sleep 15
   done
   echo "artifact landed"
   ```

要点：
- 观察器本身是纯 shell 循环，不受 harness 后台 kill 影响。
- 产物一律由子代理 write 直写文件，不依赖 stdout 回收。
- 观察器必须双监视：只盯产物不盯进程，模型端静默停滞时会无限空等；只盯进程不盯产物，进程正常退出但 0 产物时会误判成功。
- 观察器自身不限时；超时由 orchestrator 按"静默停滞、阈值与终止"处理底层 pi 进程。

## 并行扇出 — 完整脚本模板

```bash
# 先 1 个试点，再从 2 并发上探，遇 429/超时回退
for f in prompts/worker-*.txt; do
  pi --mode json --no-session -nc -na \
     --provider <provider> --model <model> \
     "@$PWD/$f" > "logs/$(basename "$f" .txt).jsonl" 2>&1 &
done
wait
```

```powershell
# Start-Job 是 PowerShell 进程内后台作业，主控进程用 Wait-Job 存活等待——安全
$jobs = foreach ($w in $workers) {   # $workers: 每项含 promptFile / log
  Start-Job -ScriptBlock {
    param($promptFile, $log)
    pi --mode json --no-session -nc -na --provider <p> --model <m> "@$promptFile" *> $log
  } -ArgumentList $w.promptFile, $w.log
}
# 有限超时等待（硬超时；超时停止、记录警告、保留日志）
$deadline = (Get-Date).AddSeconds(600)
while ((Get-Date) -lt $deadline -and ($jobs | Where-Object State -eq 'Running')) {
  Wait-Job -Job $jobs -Any -Timeout 30 | Out-Null
}
$jobs | Where-Object State -eq 'Running' | Stop-Job -PassThru | Out-Null
# 后续按主文件"回收并验收"逐文件验证；不因作业"完成"即判成功
$jobs | Remove-Job
```

并发纪律：每个 worker 独立日志与独立产物路径，失败定位靠文件验证而非解析 stdout；**先派 1 个试点 worker 走通链路，再逐步从 2 个并发上探**；遇 429/超时回退；扇出前向用户报预计调用数上限与所选模型，未经新鲜授权不突破已披露上限，不静默烧钱。

## 多轮续接

pi 的 PISR 派发统一 `--no-session`（不落 `~/.pi/agent/sessions/`，不污染用户交互会话）。默认设计为**一次投递、文件回收**，不依赖会话续接；需要会话续接的场景属于 OCSR 的分工面（opencode `--continue`/`--session`/`--fork`）。pi 本身具备 `pi -c` / `--session` / `--fork` 能力，但 PISR 不把它们纳入派发协议——手工使用时以 `pi --help` 实测为准。

## 基本命令与长 prompt 文件处理

> 由主文件的默认派发闭环按需链接的可复制命令模板。

```bash
# 基本非交互（文本输出）
pi -p --no-session -nc -na --provider <p> --model <m> "你的 prompt"

# 事件流输出（驱动器基线；usage/toolcall 可机械审计）
pi --mode json --no-session -nc -na --provider <p> --model <m> "你的 prompt"

# 只读工具面（reviewer）
pi --mode json --no-session -nc -na --provider <p> --model <m> \
   --tools read,grep,find,ls "审查这个文件并输出报告"

# 思考档位
pi --mode json --no-session -nc -na --provider <p> --model <m> --thinking high "..."

# prompt 文件注入（长 prompt 唯一推荐方式；无命令行长度与转义问题）
pi --mode json --no-session -nc -na --provider <p> --model <m> "@C:/path/prompt.txt"

# 管道 stdin（print 模式合并进初始 prompt）
cat prompt.txt | pi -p --no-session -nc -na --provider <p> --model <m>
```

长 prompt 别在命令行里拼（Windows 命令行 32K 上限 + 转义地狱）——写入 UTF-8 文件用 `@file` 注入。手工写 prompt 文件时显式 UTF-8（Windows PowerShell 5.1 的 `Set-Content` 默认 ANSI，会写坏中文）：

```powershell
Set-Content -Path .\prompts\worker-01.txt -Value $prompt -Encoding UTF8
```

## Windows 中文编码策略细节

> 由主文件的默认派发闭环按需链接；本节承载策略展开与失败诊断。

驱动器主链路无 PowerShell：Python subprocess 直调 pi（Node，stdout UTF-8），事件流/产物均 UTF-8 落盘。手工调用时的两种策略：

1. 重定向到文件再读：注意 PS5.1 `*>` 落盘为 UTF-16LE BOM，须 `Get-Content -Encoding UTF8` 读；PS7 落盘为 UTF-8 无 BOM。
2. **（推荐）让子代理用 write 工具直接把报告写到指定路径**，完全不依赖 stdout 回传。

失败诊断不依赖 stdout：成败判定以主文件"回收并验收"的**期望产物文件是否落盘**为准，退出码为辅。日志文件出现乱码时，先区分显示问题还是文件损坏——用 UTF-8 方式重读文件；若文件字节无误则仅为显示层乱码。

## 派发遥测记录片段

> 由主文件按需链接的遥测 schema（驱动器 `~/.pisr/dispatch-log.jsonl` 每行一个 JSON 对象；本模板为字段权威定义，与 `pisr_dispatch.py` 的 `TELEMETRY_FIELDS` 同步）。

```json
{
  "ts": "<ISO8601>",
  "model": "<provider/model>",
  "role": "<executor|reviewer|...>",
  "harness": "<cli>",
  "channel": "<foreground|detached|background>",
  "outcome": "<success|stall|killed|error|path_collision|unexpected_write>",
  "wall_min": 0.0,
  "artifact_bytes": 0,
  "task_id": "<task_id>",
  "plan_ref": "<plan_ref>",
  "scope": "<scope>",
  "prompt_size_bytes": 0,
  "response_size_bytes": 0,
  "model_cost_input": 0.0,
  "model_cost_output": 0.0,
  "cost_estimate": 0.0,
  "blocking_chain": ["<blk1>", "<blk2>"],
  "outcome_detail": "<success:completed|error:exit_code_N|error:exit_0_no_artifact|tool_violation:...|killed:failed|...>",
  "failure_retry_index": 0,
  "usage_input": 0,
  "usage_output": 0,
  "usage_total_tokens": 0,
  "usage_cost": 0.0,
  "tool_calls": 0,
  "tool_violations": 0,
  "label": "<label>",
  "note": "<note>",
  "timeout_policy_requested": "<auto|leaf_kill|hierarchical_report>",
  "timeout_policy_resolved": "<leaf_kill|hierarchical_report>",
  "forbid_paths": 0,
  "read_audit": "<clean|violated|unavailable>",
  "tool_audit": "<clean|violated|unenforced>"
}
```

字段语义：`usage_*` 与 `tool_calls` 来自 `--mode json` 事件流的真实计数（OCSR 为字节估算，PISR 为实测值）；`model_cost_*`/`cost_estimate` 恒 0（`pi --list-models` 目录不含价格，保留字段以维持 schema 兼容）；`tool_violations > 0` 即越权审计命中，`outcome_detail` 同步为 `tool_violation:<names>`。
