# CHANGELOG

## 2026-08-26

### 初始化：PISR 仓库（ocsr 对等移植，pi 后端）

- 以 headless `pi --mode json` 为后端的子代理派发 skill；与 OCSR 互补并存。
- `scripts/pisr_dispatch.py`：dispatch/selftest/telemetry/summary/monitor/verify-ownership/run/preflight 八子命令。执行层 Popen 直启（无 PowerShell launcher），`@file` 注入 prompt，事件流解析（usage/toolcall 真实遥测），工具越权审计（fail-closed），退出码契约 0/1/2/3。
- `scripts/pisr_run_spec.py`：确定性步骤运行器（dispatch/hook/pause/assert，journal 断点续跑，fail-closed 契约；dispatch 步骤新增 tools/thinking 字段）。
- 遥测扩展 usage_input/output/total_tokens/cost、tool_calls、tool_violations 字段（事件流实测值）；cost_estimate 恒 0（pi 目录不含价格）。
- 治理：agent_links/audit/changelog 复用（通用工具）、verify_pisr_skill.py 锚点回归、pre-commit hook、PR squash 工作流。
- 实证基线：pi 0.84.3；`@file` 注入 ✅；`--tools read,grep,find,ls` 硬白名单（模型无 write 可用、零产物）✅。
