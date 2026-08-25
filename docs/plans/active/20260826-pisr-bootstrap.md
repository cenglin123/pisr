# PISR 仓库初始化实施计划（ocsr 对等移植）

> 状态：执行中。目标：以 pi（`@earendil-works/pi-coding-agent`）替代 opencode 为后端，
> 复刻 ocsr 的完整功能面。权威参照：`C:\Users\Administrator\Documents\Github\ocsr`。
> 设计文档：`D:\project\pisr\docs\superpowers\specs\2026-08-26-pisr-skill-design.md`。

## 全局约束

- 后端命令基线：`pi --mode json --no-session -nc -na --provider P --model M`（一次实测通过）。
- pi 无内置权限系统：`--tools` 白名单是进程级工具面约束但**不是安全沙箱**，全部文档不得误述。
- 退出码契约与 ocsr 一致：0 全落盘 / 1 看门狗超时 / 2 确定性失败 / 3 路径碰撞；优先级 3>1>2>0。
- 遥测日志 `~/.pisr/dispatch-log.jsonl`；账本名 `pisr-dispatch-ledger.jsonl`。
- 环境变量熔断：`PISR_DISABLE_MODEL_CALLS=1`。
- 文本 UTF-8 无 BOM、LF；驱动器仅 Python 3 标准库。
- pisr 不接入 converge 预算门（SKILL.md 明示不得作为 converge Spawn 通道）。

## 与 ocsr 的既定差异（其余全部对等）

| 差异点 | ocsr | pisr |
|---|---|---|
| 执行模型 | PowerShell launcher + Start-Process 脱管 + marker/pid 文件 | `subprocess.Popen` 直启 pi（子进程天然脱离父生命周期）；prompt 经 work-dir 副本以 `@file` 注入；stdout→events.jsonl、stderr→stderr.log |
| 工具面 | prompt 禁令（best-effort） | `--tools`/`-xt` 进程级白名单 + 事件流 toolcall 审计（越权即确定性失败） |
| 遥测用量 | 字节估算 cost | 事件流真实 usage（usage_input/output/total/cost）+ cost_estimate 保留为 0 |
| DB 锁重派 | 30s 后自动重派 1 次 | 无（pi 无共享会话 DB）；驱动器不做任何自动重派 |
| cost 缓存 | `opencode models --verbose` 解析 | 无（`pi --list-models` 不含价格）；preflight 先查目录再真实探测 |
| 模型标识 | qualified ID 单段 | `provider/model` 二段拆分 |
| prompt 七要素 | 六要素 | 六要素 + 第七项「工具面声明」 |
| 会话续接 | `--continue/--session/--fork` | 无（`--no-session` 一次性） |

## 任务清单

- [x] T1 骨架：git init、目录、.gitignore/.gitattributes、config/allowed-models.json（默认 cc-switch-xiaomi-mi-mo/mimo-v2.5[-pro]）、治理三脚本逐字复制（agent_links/audit/changelog 均为通用工具）
- [x] T2 实测 `@file` prompt 注入与 `--tools` 白名单行为（决定驱动器主路径）
- [x] T3 `scripts/pisr_dispatch.py`：常量/白名单 fail-closed/遥测（新增 usage_*/tool_* 字段）/快照碰撞/forbid-paths reads:审计/`_spawn_worker`+`_watch_loop`（Popen 句柄、per-worker deadline、taskkill /F /T /PID、leaf_kill|hierarchical_report）/dispatch|selftest|telemetry|summary|monitor|verify-ownership|run|preflight 八子命令
- [x] T4 `scripts/pisr_run_spec.py`：近乎逐字移植（重命名、白名单文案、resume_hint 路径、dispatch 步骤新增 tools/thinking 可选字段）
- [x] T5 `scripts/verify_pisr_skill.py`：锚点改为 pisr 语义（name/触发词/`pi --list-models`/`--tools 不是沙箱`/3 次/预算/工具面声明/七要素/遥测字段同步/白名单加载）
- [x] T6 tests/：test_pisr_dispatch.py（遥测字段/argv 构造/事件流解析 fixture/工具审计/退出码契约/summary/verify-ownership）、test_execution_layer_integrity.py（0/1/2 契约、混合优先级、kill 语义、无二次 kill）、test_run_spec.py + test_run_engine.py + test_run_dogfood.py（移植）、test_audit.py（逐字）
- [x] T7 SKILL.md（四步闭环+七要素+按需专题表）+ refs/ 7 篇（dispatch-patterns/failure-modes/model-defaults/pitfalls-reference/hierarchical-command/run-spec/release-executor；converge-integration 不移植，SKILL.md 显式排除）
- [x] T8 docs/（CURRENT/STRUCTURE/overview/deployment/pitfalls/audit-checklist/CHANGELOG）+ AGENTS.md（PR squash 工作流全文沿用）+ agent_links repair + .githooks/pre-commit + README.md
- [x] T9 离线验证全绿：`python scripts/verify_pisr_skill.py`、`agent_links.py check`、`audit.py check`、`pytest tests/ -q`；首个 commit
- [x] T10 在线冒烟（真实消耗 3–5 次廉价 mimo 调用，已获用户授权）：preflight → selftest --model → dispatch 产出型 worker → dispatch 只读 reviewer（--tools read,grep,find,ls）→ telemetry/summary 检视
- [x] T11 部署 `~/.claude/skills/pisr`（排除 .git/tests/.githooks/docs/plans）+ GitHub `cenglin123/pisr` 推送
- [x] T12 .memory INDEX 记录事件；设计文档补记差异实证

## 冒烟实证记录（2026-08-26）

- preflight：2 模型 available（目录检查 + 真实探测）。
- selftest --model：产物 16B、tokens=6086、tools=2，内容校验通过。
- 产出型 worker：834B、tokens=7659、tools=3，四查通过。
- **真实事故演练**：首轮 worker prompt 写死输出名与 --output-pattern 不一致 → 驱动器正确判 `error:exit_0_name_mismatch`（勿按 0 产物重派）+ unexpected_write 遥测——失败语义分层实地验证。
- 只读 reviewer（--tools read,grep,find,ls --capture-reply --forbid-paths）：回复机械落盘 4495B、tokens=8902、tool_audit=clean、reads 审计 clean。
- 冒烟中发现并修复：capture 路径补 reads: 审计；新增 `--capture-reply`（只读 reviewer 的产物回收模式，机械落盘非自述采信）。

## 验收标准

1. 四套离线检查全绿。
2. T10 全链路真实走通：产出型 worker 产物四查通过；reviewer 工具审计 clean（tool_calls 仅 read/grep 类）；dispatch-log 含真实 usage。
3. 部署副本与仓库一致；GitHub 远端就绪。
4. 全部文档无「沙箱」误述；SKILL.md 含全部不变量锚点。
