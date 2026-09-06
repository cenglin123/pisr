# Reviewer 工具面解冻：运行时验证场景可加 bash（2026-09-06）

> 状态：完成（待提交；分支 agent/reviewer-bash-tool-face）
> 范围：仅 reviewer 工具面规则；驱动器、tests、遥测 schema、退出码契约零改动。

## 背景与动机

独立审计治理闭环中存在"运行时验证"评审视角（跑测试/lint 等只读命令佐证判断）。
现行规则把 reviewer 工具面钉死为 `read,grep,find,ls`，该视角只能拆成
executor 写验证报告 + reviewer 读报告的两次派发——reviewer 拿到的是二手压缩
证据且无法交互式追问。解冻为"默认四件套、验证场景可加 bash"后，该视角可在
一次派发内完成第一手运行时审计。

驱动器 `_audit_tool_calls` 对任意 `--tools` 参数化生效，`PI_BUILTIN_TOOLS` 已含
bash；`--tools read,grep,find,ls,bash` 开箱即用，无需改代码。

## 宪法级不变量（本次不动）

- write 对 reviewer 始终禁（生成/评估分离：可观察运行时，不可改产物或修复）
- `--capture-reply` 机械落盘不变；exit≠0/空回复仍判失败
- `--forbid-paths` 注入 + `reads:` 审计不变；驱动器越权审计 fail-closed 不变
- 三次总尝试上限、预算披露、"非安全沙箱"边界不变
- 默认档位仍是 `read,grep,find,ls`（最小污染面），bash 为按需扩展

## 改动清单

- [x] SKILL.md：七要素第 7 项解冻；派发命令示例加验证型 reviewer；§进阶专题末尾措辞同步
- [x] refs/failure-modes.md：fresh 对抗评审闭环第 2 节工具面段落（含污染面与审计面变化说明）
- [x] refs/dispatch-patterns.md：§通道选择判据措辞 + §fresh 对抗评审 PISR 加成句
- [x] 四套离线验证（verify_pisr_skill / agent_links check / audit check / pytest）全绿
- [x] 独立复查：PISR 只读 reviewer 审 diff（deepseek-v4-flash；MiniMax-M3 通道无 key 切换）——verdict=PASS 零阻断，3 条非阻断建议已采纳（"显式声明/此档位下"限定词、hierarchical-command.md 与 model-defaults.md 交叉引用、bash 间接写入如实表述内联七要素）
- [x] bash 档在线冒烟（deepseek-v4-flash）：bash 可用、toolName="bash" 计量正确、capture-reply 组合正常、模型遵守只读合同
- [x] docs/CURRENT.md、docs/CHANGELOG.md 更新

## 验收

- 四套离线验证全绿（修订后复跑仍全绿：verify 14/14、agent_links ok、audit exit=0、pytest 149 passed）
- 独立复查无阻断性问题（PASS；非阻断建议已全部采纳）
