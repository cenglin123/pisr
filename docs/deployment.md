# 本地运行与验证

PISR 没有服务部署。这里的"运行"指在本机调用 pi CLI，以及执行离线文档回归检查。

## 依赖

- Windows PowerShell（兼容 Windows PowerShell 5.1）；Python 3（标准库）。
- pi CLI（`@earendil-works/pi-coding-agent` ≥ 0.84.3）及至少一个已配置、可用的模型 provider。

## 基线检查

```powershell
pi --version
pi --list-models
python scripts/pisr_dispatch.py selftest
python scripts/verify_pisr_skill.py
```

在线冒烟（消耗一次真实模型调用）：

```powershell
python scripts/pisr_dispatch.py preflight --model xiaomi/mimo-v2.5
python scripts/pisr_dispatch.py selftest --model xiaomi/mimo-v2.5
```

模型可用性和 CLI 参数是实时环境事实。升级 pi 后，应重新验证 `--mode json`、`--tools`、`-nc`、`-na`、`--no-session`、`@file` 注入与事件流 schema（`session`/`message_end`/`tool_execution_start`/`agent_end` 事件类型与 usage 字段）——`selftest` 覆盖主链路。

## 文档体系检查

```powershell
python scripts/agent_links.py check
python scripts/audit.py check
git diff --check
git ls-files --eol | Select-String "w/crlf"
```

## 部署到 ~/.claude/skills/pisr

发布时手动复制（排除仓库件）：

```powershell
robocopy C:\Users\Administrator\Documents\Github\pisr C:\Users\Administrator\.claude\skills\pisr `
  /MIR /XD .git tests .githooks __pycache__ .pytest_cache docs\plans
```

部署后核对副本与仓库一致（ocsr 曾因手动复制漂移落后仓库——以仓库为唯一事实源）。

## Git hook

仓库使用 `.githooks/pre-commit` 检查三份 agent 入口一致性和 PISR 回归脚本。`core.hooksPath` 应指向 `.githooks`：

```powershell
git config core.hooksPath .githooks
```
