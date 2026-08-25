# `run --spec` 步骤运行器 — 完整 schema 与语义

> 由 [`SKILL.md`](../SKILL.md) 委托。实现：`scripts/pisr_run_spec.py`（校验 + 执行）、`pisr_dispatch.py run`（CLI）。设计继承 OCSR 同名运行器（D1–D10），本文件为 pi 后端移植版。

## 它解决什么

多步骤协议由 agent 逐步手工搬运时，会出现参数推导错误（把某个分组的序号填成另一个分组的轮次号）、时序与记账错误、资源归属错误。这些**不需要判断力，只需要不出错地重复**——正是脚本该干的。

**立场：把搬运交给脚本，判断留给 agent。**
运行器不写 prompt、不判 verdict、不裁决分歧；它只做步骤序列、参数推导、路由、记账、断点续跑。
**它防不住内容事实错误**——那类错误的对治是评审，不是调度。

## 命令

```bash
# 离线干跑：只校验 + 输出结构化摘要，不发起任何模型调用
python scripts/pisr_dispatch.py run --spec <file> --validate [--format json]

# 执行
python scripts/pisr_dispatch.py run --spec <file>

# 断点续跑 / 回答暂停点
python scripts/pisr_dispatch.py run --spec <file> --resume [--answer <step-id>=<option>]
```

### 退出码

| 码 | 含义 |
|---|---|
| `0` | run 完成 |
| `1` | 步骤失败（hook 非零/`expect` 不匹配、assert 不成立、dispatch 非零、被 `abort`） |
| `2` | spec 非法或用法错误 |
| `10` | **暂停待裁决**，已写 `pause-request.json` |
| `11` | **续跑状态不确定**，停机 |

> 与 `dispatch` 子命令的退出码契约（见 `dispatch-patterns.md` §退出码契约）是**两套**，勿混用。

## Spec schema（version: 1）

```yaml
version: 1
run:
  id: <标识，[A-Za-z0-9][A-Za-z0-9._-]{0,63}>
  workdir: <runner 独占的目录，可用模板>
vars:                      # 可选，字符串到字符串
  k: v
steps:                     # 非空；**第一个步骤是入口**
  - id: <唯一标识>
    type: dispatch | hook | pause | assert
    ...
```

### 步骤类型（**封闭**为四种）

新增任何步骤类型，都必须重新审视 `docs/overview.md`「通用 runner」非目标的关系，并按治理文档变更走独立复查——**不得作为普通功能增强顺手加入**。

> ⚠️ **边界的真实位置**：`hook` 步骤执行 spec 显式声明的 argv，**这本身就足以执行任意命令**。校验层不做命令白名单。因此**不得声称运行器「不执行任意用户代码」**；准确表述是：运行器不提供 inline eval/exec 式步进内代码解释语法，hook 的 argv 由 spec 显式声明、可审计；**它不是安全沙箱，也不宣称是**。spec 及其调用的命令必须当作可信输入对待。

#### `dispatch` — 派发 PISR worker

| 字段 | 必填 | 说明 |
|---|---|---|
| `model` | ✅ | PISR 白名单内的 provider/model ID |
| `prompt` | ✅ | prompt 文件路径（相对路径按 spec 文件所在目录解析） |
| `output` | ✅ | 期望产物路径，**必须在 `run.workdir` 之内**（D7） |
| `tools` | | pi 工具白名单列表（如 `[read, grep, find, ls]`）；缺省全工具（pisr 扩展） |
| `thinking` | | `--thinking` 档位字符串（pisr 扩展） |
| `capture_reply` | | 布尔；产物=最终回复机械落盘（只读 reviewer，pisr 扩展） |
| `role` | | 遥测角色 |
| `scope` | | **runner 的分组计数键**，见下方专节 |
| `timeout_min` | | 看门狗阈值（正整数） |
| `forbid_paths` | | 禁读路径列表，注入 prompt 副本 |
| `ledger_dir` | | 派发账本目录 |
| `meta` | | 透传给 pisr 遥测的归因字段（`task_id` / `plan_ref` / `scope` / `blocking_chain`） |
| `pre` / `post` | | **内联 hook**，见下 |

进程内调用 `_dispatch_batch`（不起子进程）。派发前会**重新校验**模型白名单、prompt 存在性与输出目录——不以「已经 `--validate` 过」为由跳过。

> ⚠️ `step.scope`（runner 分组键）与 `step.meta.scope`（pisr 遥测归因）是**两回事**，刻意不混用。

#### `hook` — 执行外部命令

```yaml
- id: reserve
  type: hook
  run: [python, "{{vars.gate}}", reserve, --role, outer-reviewer]
  expect: '^PROCEED:(?P<rid>\w+)$'      # 可选；不匹配即失败
  timeout_sec: 300                       # 可选
```

`rc ≠ 0` 或 `expect` 不匹配即步骤失败（契约违反 fail-closed）。

#### `pause` — 把决策交回 agent

```yaml
- id: ask
  type: pause
  question: verdict 非预期，请裁决
  options: [fix, closeout, abort]
```

写 `pause-request.json` 并以 **exit 10** 退出。`--answer <step-id>=<option>` 续跑。保留字 `abort`/`retry` 不是步骤 id；指向步骤 id 的选项是图的真实出边。

#### `assert` — 确定性校验

```yaml
- id: check
  type: assert
  assert:
    file_exists: "{{run.workdir}}/report.md"
    non_empty: true
    matches: 'verdict'
```

条件仅支持 `file_exists` / `non_empty` / `matches`；后两者需与 `file_exists` 同时给出。

### 取值与路由

```yaml
    extract:
      verdict: "yaml:verdict"
    route_on: verdict
    route:
      "可执行": closeout
      "阻断需修复": fix
      "*": ask                 # 必填，且必须指向 pause 步骤
```

**取值器封闭为三种**：`yaml:<key>`（第一个 fenced YAML 块顶层键）、`regex:<pattern>`（首个命名组/分组/整匹配）、`exitcode`。

**取值来源按步骤类型确定**：`hook` → 进程 stdout+stderr；`dispatch` → **产物文件内容**（不采信 stdout，只信文件系统证据）；`assert` → 只能用 `exitcode`。

仅当 extract 已成功产出 route_on 的值、但未命中任何具名 route 时，才走必填的 `"*"` 兜底；兜底目标必须是 `pause`。提取失败、schema 非法、模板不可解析等契约失败均 fail-closed，不得借 `"*"` 转为 pause。

无 `extract` 的步骤用 `next: <step-id>` 单向推进；二者都没有即为终止步骤。

### 模板文法（封闭）

| 形式 | 说明 |
|---|---|
| `{{run.id}}` / `{{run.workdir}}` | run 元信息 |
| `{{vars.<name>}}` | `vars` 段的变量 |
| `{{scope.<key>.next_index}}` | runner 按分组键派生的单调序号 |
| `{{steps.<id>.(pre\|post)[<n>].<name>}}` | 内联 hook 的命名组捕获 |
| `{{steps.<id>.capture.<name>}}` | 独立 `hook` 步骤自身 `expect` 的命名组捕获 |

引用不可解析即 fail-closed。**YAML 陷阱**：正则必须用单引号标量（`'^PROCEED:(?P<rid>\w+)$'`），双引号标量会按 YAML 转义规则报 `unknown escape character`。

## `scope`：分组计数键

`{{scope.<key>.next_index}}` 由运行器按分组键维护单调计数器，**调用方不写数字**。运行器把 `scope` 当作不透明字符串（有回归测试断言实现文件不得出现具体分组键字面值）。含义归 spec 作者。

## journal 与断点续跑

workdir 下的 `journal.jsonl` 是 append-only 执行日志，采用 **started / completed 两段式**。

**四条 fail-closed，共同的立场是「不确定就停机，绝不猜」**：

| 情形 | 处置 |
|---|---|
| `step-started` 无 `completed`/`paused` | **停机(11)，禁止自动重跑**——该步可能已消耗真实模型调用 |
| 已有 journal 却未加 `--resume` | 拒绝覆盖既有执行记录 |
| spec 的 sha256 与上次不符 | 拒绝把新 spec 语义套到旧 journal 上 |
| journal 存在无法解析的行 | 状态不确定 |

## workdir 独占

运行器创建并**独占** `run.workdir`。dispatch 步骤的产物路径若落在其外，**在派发前**即 fail-closed。run 期间调用方不得写 workdir。

## `--validate` 能查什么、查不到什么

**能查**：spec schema、步骤 id 唯一性、类型合法性、route 目标存在性与 `"*"` 兜底、兜底指向 pause、图无环与全可达、模板引用可解析、取值器封闭枚举、正则可编译、模型在白名单、prompt 文件存在、pause 选项合法、`tools`/`thinking` 字段形状。

**查不到**：**语义错误**（如两条路由映射对调——spec 依旧合法）。这类错误只有端到端比对能抓。
