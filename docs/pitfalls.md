# 已知陷阱

## 安全边界

`--tools` 白名单、`-nc`/`-na`、prompt 禁令和输出路径审计只能降低误访问风险，不能阻止恶意模型越界读取或外发数据（pi 无内置权限系统；README 明示更强边界需容器化）。敏感材料需要宿主层低权限账户、文件系统隔离和出站网络限制。

## 证据真实性

- 子代理说"完成"不等于文件已写入。
- reviewer 报告由 orchestrator 代写，不构成独立评审。
- `usage.cost=0` 常见于 custom provider——是价格元数据缺失，不是免费证明。
- exit=0 且零产物 = 越界写入或模型拒绝写入的指纹，不是成功。

## Windows 编码

驱动器主链路全 UTF-8（Python subprocess 直调 Node）。PowerShell 5.1、PowerShell 7、终端显示和文件字节是不同层，仅在手工调用路径上构成风险。先以 UTF-8 显式重读并检查原始字节，再决定是否修复文件。

## 调用成本

主模型的内建子代理通常继承主模型。需要廉价执行或异构 reviewer 时使用 PISR 指定模型；派发前披露调用数上限，失败不能无限重派；驱动器不做任何自动重派。

harness 前台超时不足、后台通道 kill、模型端静默停滞的具名故障模式与止损协议见 [`refs/dispatch-patterns.md`](../refs/dispatch-patterns.md)——本文件不重复正文。

## 上层流程对齐

PISR 未接入 converge 预算门与证据链；在 converge 流程中作为 Spawn 通道属于越权使用（该场景走 OCSR）。旁路 spawn、inline reviewer 或未进入 ledger 的调用都会破坏审计完整性。

## 执行层历史教训（继承自 OCSR 的事故类）

- 失败曾对外表现为成功（单一 landed 集合混淆落盘与失败）→ 三集合结案语义 + 退出码契约（test_execution_layer_integrity.py 守护）。
- 看门狗按窗口标题 kill 匹配不到进程（空操作止损）→ PISR 直接持有 Popen PID，kill 按 PID + 校验退出码。
- launcher 多层转义秒退 → PISR 无 launcher，`@file` 注入无转义面。
- 重派不刷新 pid → PISR 无自动重派（结构性消除）。
