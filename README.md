# PISR

PISR（Pi Subagents Run）把 headless `pi --mode json` 用作独立于宿主框架的子代理执行后端，适合可配置模型的批处理 worker、带进程级只读白名单的 fresh-context 对抗评审与结构化 usage 遥测。与 [OCSR](https://github.com/cenglin123/ocsr)（opencode 后端）互补并存。

## 快速开始

前置条件：Windows PowerShell、Python 3、pi CLI（`@earendil-works/pi-coding-agent` ≥ 0.84.3）及已配置的模型 provider。

```powershell
pi --version
pi --list-models
# <provider/model> 必须存在于 config/allowed-models.json
python scripts/pisr_dispatch.py selftest
python scripts/pisr_dispatch.py preflight --model cc-switch-xiaomi-mi-mo/mimo-v2.5
```

运行规则、prompt 七要素、产物回收和模型分工以 [SKILL.md](SKILL.md) 为准。

## 与 OCSR 的差异（一屏速览）

| | OCSR | PISR |
|---|---|---|
| 后端 | `opencode run` | `pi --mode json` |
| 工具面 | prompt 禁令（best-effort） | `--tools` 进程级白名单 + 事件流越权审计 |
| 用量遥测 | 字节估算 | 事件流真实 usage |
| 执行层 | PowerShell launcher 脱管链 | Popen 直启 + `@file` 注入 |
| 会话续跑 / converge Spawn 通道 | ✅ | ❌（一次性派发；未接 converge） |

## 文档

- Agent 协作入口：[AGENTS.md](AGENTS.md)
- 文档导航：[docs/STRUCTURE.md](docs/STRUCTURE.md)
- 详细参考（渐进式披露，按需加载）：[refs/](refs/) — 派发模式、失败模式、模型默认池、run-spec、层级指挥、release executor、陷阱参考
- 当前状态与交接：[docs/CURRENT.md](docs/CURRENT.md)
- 设计边界：[docs/overview.md](docs/overview.md)
- 本地运行：[docs/deployment.md](docs/deployment.md)
- 已知陷阱：[docs/pitfalls.md](docs/pitfalls.md)

## 验证

```powershell
python scripts/verify_pisr_skill.py
python scripts/agent_links.py check
python scripts/audit.py check
pytest tests/ -q
```

本仓库的核心产物是 skill 文档和机械验证脚本，不提供安全沙箱、常驻服务或通用运行时 wrapper。
