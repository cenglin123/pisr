# CHANGELOG

## 2026-08-26（PR-A：防御性加固，独立评议后落地）

- **schema 漂移报警**：`_parse_event_stream` 新增 `recognized` 计数（已识别事件类型数）；exit=0 且事件流非零字节但零个已识别类型时，outcome_detail 记 `error:schema_drift_suspect`（区别于 `exit_0_no_artifact`；空文件不算漂移）。判定条件经独立评审修正：区分"空文件"与"有内容全不识别"。
- **stub-pi 端到端集成测试**（tests/stub_pi.py + test_e2e_stub_pi.py）：PI_BIN 指向测试桩，真实 subprocess 链路（spawn/重定向/poll/taskkill/capture/漂移）离线覆盖，此前全部 mock Popen 无此层。4 用例 ~2s。
- **docs**：dispatch-patterns.md 新增"驱动器中途被杀（孤儿 worker）"小节（指纹/手工处置/预防；自动化按证据门控后置）。
- 评审背景：fresh-context 只读 reviewer（pisr 自驱）评议改进计划，15 项裁定 + PR 重分组；其中 2 项事实断言经代码核实驳回（详见评审报告存档）。

## 2026-08-26

### 初始化：PISR 仓库（ocsr 对等移植，pi 后端）

- 以 headless `pi --mode json` 为后端的子代理派发 skill；与 OCSR 互补并存。
- `scripts/pisr_dispatch.py`：dispatch/selftest/telemetry/summary/monitor/verify-ownership/run/preflight 八子命令。执行层 Popen 直启（无 PowerShell launcher），`@file` 注入 prompt，事件流解析（usage/toolcall 真实遥测），工具越权审计（fail-closed），退出码契约 0/1/2/3。
- `scripts/pisr_run_spec.py`：确定性步骤运行器（dispatch/hook/pause/assert，journal 断点续跑，fail-closed 契约；dispatch 步骤新增 tools/thinking 字段）。
- 遥测扩展 usage_input/output/total_tokens/cost、tool_calls、tool_violations 字段（事件流实测值）；cost_estimate 恒 0（pi 目录不含价格）。
- 治理：agent_links/audit/changelog 复用（通用工具）、verify_pisr_skill.py 锚点回归、pre-commit hook、PR squash 工作流。
- 实证基线：pi 0.84.3；`@file` 注入 ✅；`--tools read,grep,find,ls` 硬白名单（模型无 write 可用、零产物）✅。
- `--capture-reply`：只读 reviewer 产物回收（最终回复机械落盘；越权/reads 审计优先）；冒烟中实地演练 name-mismatch 失败分层。
