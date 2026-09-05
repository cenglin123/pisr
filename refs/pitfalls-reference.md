# 陷阱清单（完整表）— 详细参考

> 本表由 [SKILL.md 的维护与验证入口](../SKILL.md) 按需加载。主文件保留速查摘要与 verify 锚点；本文件承载完整陷阱表（事实/对策原文）。

## 陷阱清单

> 本表及全文"实测"的环境基线：pi 0.84.3（`@earendil-works/pi-coding-agent`）· Windows 10 · Python 3（驱动器）· Git Bash / PowerShell。版本升级后关键行为需复验（推荐调用方派发脚本的 selftest）。

| 陷阱 | 事实 | 对策 |
|------|------|------|
| `--tools` 不是沙盒 | `--tools` 是进程级**工具面白名单**（实测：只读白名单下模型无法 write，写入请求被拒且零产物），但 pi 无内置权限系统：已授权工具（如 bash、read）仍可访问任意可达路径/外发数据 | 三层缓解：信息最小化 + prompt 明令禁读界外 + 回收时审计 `reads:` 与 toolcall。注意：工具面白名单、prompt 禁令和事后审计都是 best-effort，不构成安全隔离 |
| `-nc`/`-na` 不是沙盒 | `-nc` 禁 context files、`-na` 忽略项目本地资源，只影响**输入面**；子代理仍可用绝对路径读任意位置 | 同上；含秘密/prompt-injection 风险的材料不得交给不可信模型，条件不满足则停止并报告安全边界 |
| pi 无内置权限系统 | 与 opencode 同类的执行边界缺失；README 明示需要更强边界时用容器化（Docker/Gondolin/OpenShell） | 不可信任务先容器化再派发；本仓库不提供沙箱 |
| 框架内建子代理换不了厂商 | 宿主 `task` 无 per-spawn model 参数；多数 harness 仅同家族档位 | 跨厂商异构一律 pisr/ocsr 显式 `--provider/--model` |
| pi 会话概念与派发无关 | PISR 统一 `--no-session`（不落 `~/.pi/agent/sessions/`）；pi 的 `-c/--session/--fork` 是交互会话能力，不是 live 子代理句柄 | 不要当上下文继承机制用；需要续接的场景归 OCSR |
| stdout 中文显示乱码（Windows） | GBK/UTF-8 编码不匹配导致显示层乱码，不意味文件内容损坏 | 驱动器主链路全 UTF-8 落盘；手工 `*>` 重定向注意 PS5.1 落盘 UTF-16LE；日志乱码时用 UTF-8 重读区分显示问题与文件损坏 |
| 子代理自我报告不可信 | 会生成看似成功的报告而实际 0 产物；会反向"矫正"正确术语 | [自足 prompt](../SKILL.md) + [四步验收](../SKILL.md) + 事件流 usage/toolcall 审计 |
| `usage.cost=0` 不自动等于免费 | 事件流 cost 字段常为 0——价格元数据可能缺失（任何 provider 都可能，非仅 custom 网关），不是免费证明 | 排除模型时结合 context 限制、toolcall 能力、试点证据综合判断，标为启发式裁决 |
| harness 超时截断 / 后台通道 kill | 单次派发常超 2 分钟；部分 harness 的后台任务机制会终止 pi 进程 | 前台执行 + 调大 harness 侧 timeout；后台失败指纹 = 事件流 0 字节 + 无产物文件 |
| 静默烧钱 + 无界重派 | 扇出 N 个 worker = N 次计费调用；无上限重派可形成计费循环 | 派发前报数量上限与所选模型，每 worker 最多 3 次尝试，未经新鲜授权不突破；先派 1 个试点再从 2 并发上探，遇 429/超时回退 |
| harness 前台超时 < 单轮耗时 | 判断密集角色单轮 20–30 min，多数 harness 前台上限 ≤10 min | 改用 [脱管派发模式](dispatch-patterns.md)（Popen/Start-Process + 双监视观察器） |
| 模型端静默停滞 | 进程存活 + 事件流 0 字节 + 超过看门狗阈值 | [失败看护与切换](dispatch-patterns.md)中的看门狗硬阈值到期即终止，禁止无阈值人工轮询；记录后按失败切换阶梯重派 |
| `@file` 路径解析 | `@` 后接相对路径时按 **cwd** 解析；驱动器以进程 cwd=work-dir 运行 | 手工调用用绝对路径（推荐正斜杠 `C:/...`）；驱动器已统一绝对路径注入 |
| prompt 文件大小上限 | 驱动器 prompt 副本上限 256KB（fail-closed）；命令行直接传 prompt 另有 Windows 32K 字符上限 | 长 prompt 一律 `@file`；超 256KB 说明任务粒度过大，先拆分 |
| **并发限流礼貌** | pi 进程彼此独立无共享 DB 锁，但 provider 网关有并发/速率限制（429） | 多 worker 错峰 ≥5s（驱动器默认）；429 属通道问题，按失败切换阶梯的通道例外处理 |
| **越界写入覆盖既有产物** | `--output-pattern` 只约束看门狗等待哪个文件，**不约束子代理往哪写**。prompt 输出路径含占位符时，子代理自行发明文件名，可覆盖同目录他人产物；指纹 = exit=0 + 期望产物缺失 + 同目录既有文件 mtime/size 变化 | prompt【输出】节写死唯一绝对路径且说明覆盖后果；派发前备份 `--output-dir`；驱动器派发前后快照比对，检出即退出码 3（详见 refs/failure-modes.md §越界写入） |
| **嵌套派发失账** | 下层 orchestrator 自行发起的 `pi` 调用不经上层的预算 gate，账本只记外层——治理看到的开销可能只有真实值的一小部分 | 需要预算门对接的上层流程暂用 OCSR（已接 converge）；PISR 未接 converge 预算门，不得在 converge 流程中作为 Spawn 通道使用（SKILL.md 边界） |

---
