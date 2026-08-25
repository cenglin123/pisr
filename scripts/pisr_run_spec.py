#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""pisr_run_spec.py — 确定性步骤运行器的 spec 解析与离线校验层（pisr：pi 后端）。

对应 ocsr 同名运行器的 D1/D2/D3/D4/D8/D10 设计（本文件为 pi 后端移植版）。
本模块**只做离线校验与确定性展开**，不发起任何模型调用、不启动任何进程。

设计约束（与 SKILL.md 的 `run --spec` 默认边界对齐）：
  ① 机制不执行任务本身 —— 本模块不写 prompt、不判 verdict、不裁决分歧
  ② 不收窄编排空间 —— spec 由调用方撰写；`pause` 步骤可在任意点交回控制权
  ③ 契约违反 fail-closed —— schema/图/引用/取值器任一不合法即拒绝该 spec

**不变量（G2，有回归测试断言）**：本文件**不得出现任何分组键字面值**。
分组键（step 的 `scope` 字段）一律作为不透明字符串处理——runner 只按它分组计数，
不理解其含义。含义归 spec 作者，见 `refs/run-spec.md`。

与 `pisr_dispatch.py` 的依赖方向：**单向**。本模块不 import pisr_dispatch；
需要模型白名单等外部事实时由调用方以参数注入（`validate_spec(..., allowed_models=...)`），
以免形成循环依赖，并使本模块可独立测试。
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any, Iterable

SPEC_VERSION = 1

# 步骤类型集合 —— **封闭**（设计 G1）。
# 新增任何步骤类型都必须重新审视与 docs/overview.md「通用 runner」非目标的关系，
# 并按治理文档变更走独立复查；用户自定义函数 / 任意代码执行类步骤明确禁止。
STEP_DISPATCH = "dispatch"
STEP_HOOK = "hook"
STEP_PAUSE = "pause"
STEP_ASSERT = "assert"
STEP_TYPES = frozenset({STEP_DISPATCH, STEP_HOOK, STEP_PAUSE, STEP_ASSERT})

# 取值器封闭枚举（D1）。没有任何让脚本「判断」的入口：
# 路由键只能来自产物的确定性解析，判断力长在被解析的产物里。
EXTRACTOR_RE = re.compile(r"^(?:yaml:(?P<ykey>[^\s]+)|regex:(?P<pat>.+)|exitcode)$", re.DOTALL)

ROUTE_FALLBACK = "*"

# pause 步骤的保留选项（D6）。它们不是步骤 id，也不构成图的边：
#   abort — 终止整个 run
#   retry — 重跑当前暂停的步骤。**判断权在 agent**：runner 只提供该选项，
#           不自行决定某步是否可安全重跑（副作用是否幂等由 agent 判定）。
#           它是运行期决策，不是静态图上的自环。
PAUSE_RESERVED_OPTIONS = frozenset({"abort", "retry"})

SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

# 模板文法边界（D10）。仅支持以下取值路径形式；
# 数组下标 [<n>] 与命名组捕获是仅有的两种复合形式。
TEMPLATE_RE = re.compile(r"\{\{\s*([^}]+?)\s*\}\}")
_T_RUN = re.compile(r"^run\.(id|workdir)$")
_T_VARS = re.compile(r"^vars\.([A-Za-z0-9_-]+)$")
_T_SCOPE = re.compile(r"^scope\.([^.\s]+)\.next_index$")
_T_HOOKCAP = re.compile(r"^steps\.([A-Za-z0-9._-]+)\.(pre|post)\[(\d+)\]\.([A-Za-z_][A-Za-z0-9_]*)$")
# 独立 `hook` 步骤自身 `expect` 的命名组捕获。内联 hook 用 `pre[n]` / `post[n]` 下标定位，
# 独立 hook 步骤只有一个 `expect`，故无下标。
_T_STEPCAP = re.compile(r"^steps\.([A-Za-z0-9._-]+)\.capture\.([A-Za-z_][A-Za-z0-9_]*)$")
# 独立 hook 捕获在 run context 中的存放键（与内联 hook 的 `pre[n]` / `post[n]` 并列）。
SELF_CAPTURE_KEY = "self"
TEMPLATE_FORMS = ("run.id / run.workdir / vars.<name> / scope.<key>.next_index / "
                  "steps.<id>.(pre|post)[<n>].<name> / steps.<id>.capture.<name>")

# 启发式警告阈值（D8）：提示 spec 作者可能在用正则把判断硬编码进 spec。
# **这是提示，不是阻断。**
REGEX_NAMED_GROUP_WARN = 3
REGEX_LENGTH_WARN = 100


class SpecError(Exception):
    """spec 契约违反。code 供测试与调用方机械判别，不依赖文案。"""

    def __init__(self, code: str, message: str, location: str = "") -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.location = location

    def __str__(self) -> str:
        where = f" [{self.location}]" if self.location else ""
        return f"{self.code}{where}: {self.message}"


def load_spec(path: Path) -> dict:
    """读取并解析 spec 文件（UTF-8）。yaml 缺失时给出可执行的补救提示。"""
    try:
        import yaml  # noqa: PLC0415  — 可选依赖，缺失时需给出明确指引
    except ImportError as exc:  # pragma: no cover - 依赖缺失路径
        raise SpecError(
            "spec-yaml-unavailable",
            "解析 spec 需要 PyYAML，但当前环境未安装。请先 `pip install pyyaml`。",
        ) from exc
    if not path.is_file():
        raise SpecError("spec-not-found", f"spec 文件不存在: {path}")
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise SpecError("spec-encoding", f"spec 必须是 UTF-8: {path} ({exc})") from exc
    try:
        data = yaml.safe_load(text)
    except Exception as exc:
        raise SpecError("spec-parse", f"spec YAML 解析失败: {exc}") from exc
    if not isinstance(data, dict):
        raise SpecError("spec-root", "spec 顶层必须是映射（mapping）")
    return data


# ─── 模板引用 ────────────────────────────────────────────────────────

def iter_template_refs(value: Any) -> Iterable[str]:
    """递归取出任意结构中出现的模板引用（去掉 {{ }} 后的裸表达式）。"""
    if isinstance(value, str):
        for m in TEMPLATE_RE.finditer(value):
            yield m.group(1)
    elif isinstance(value, dict):
        for v in value.values():
            yield from iter_template_refs(v)
    elif isinstance(value, (list, tuple)):
        for v in value:
            yield from iter_template_refs(v)


def _named_groups(pattern: str) -> set[str]:
    try:
        return set(re.compile(pattern).groupindex)
    except re.error:
        return set()


def _hook_list(step: dict, kind: str) -> list:
    hooks = step.get(kind) or []
    return hooks if isinstance(hooks, list) else []


def _resolve_ref(ref: str, spec: dict, steps_by_id: dict[str, dict]) -> str | None:
    """返回 None 表示引用可解析；否则返回错误说明。"""
    if _T_RUN.match(ref):
        return None
    m = _T_VARS.match(ref)
    if m:
        if m.group(1) not in (spec.get("vars") or {}):
            return f"引用了未定义的变量 vars.{m.group(1)}"
        return None
    if _T_SCOPE.match(ref):
        # 分组键是不透明字符串，runner 不校验其取值（G2 不变量）
        return None
    m = _T_HOOKCAP.match(ref)
    if m:
        sid, kind, idx, name = m.group(1), m.group(2), int(m.group(3)), m.group(4)
        target = steps_by_id.get(sid)
        if target is None:
            return f"引用了不存在的步骤 steps.{sid}"
        hooks = _hook_list(target, kind)
        if idx >= len(hooks):
            return f"steps.{sid}.{kind}[{idx}] 越界（该步骤只有 {len(hooks)} 个 {kind} hook）"
        expect = (hooks[idx] or {}).get("expect") if isinstance(hooks[idx], dict) else None
        if not expect:
            return f"steps.{sid}.{kind}[{idx}] 没有 expect，无法捕获命名组 {name}"
        if name not in _named_groups(expect):
            return (f"steps.{sid}.{kind}[{idx}].expect 中不存在命名组 {name}"
                    f"（可用: {sorted(_named_groups(expect)) or '无'}）")
        return None
    m = _T_STEPCAP.match(ref)
    if m:
        sid, name = m.group(1), m.group(2)
        target = steps_by_id.get(sid)
        if target is None:
            return f"引用了不存在的步骤 steps.{sid}"
        if target.get("type") != STEP_HOOK:
            return (f"steps.{sid}.capture.<name> 只适用于 `hook` 步骤，"
                    f"steps.{sid} 的类型是 {target.get('type')}")
        expect = target.get("expect")
        if not expect:
            return f"steps.{sid} 没有 expect，无法捕获命名组 {name}"
        if name not in _named_groups(expect):
            return (f"steps.{sid}.expect 中不存在命名组 {name}"
                    f"（可用: {sorted(_named_groups(expect)) or '无'}）")
        return None
    return f"不在模板文法边界内。支持的形式仅有 {TEMPLATE_FORMS}"


# ─── 校验 ────────────────────────────────────────────────────────────

def _require(cond: bool, code: str, msg: str, loc: str = "") -> None:
    if not cond:
        raise SpecError(code, msg, loc)


def _validate_extract(step: dict, sid: str, warnings: list[str]) -> None:
    extract = step.get("extract")
    if extract is None:
        return
    _require(isinstance(extract, dict) and extract,
             "extract-shape", "extract 必须是非空映射", f"steps.{sid}")
    for name, expr in extract.items():
        loc = f"steps.{sid}.extract.{name}"
        _require(isinstance(expr, str) and bool(expr),
                 "extract-shape", "取值器必须是非空字符串", loc)
        m = EXTRACTOR_RE.match(expr)
        _require(m is not None, "extract-unknown",
                 f"取值器 `{expr}` 不在封闭枚举内。仅支持 yaml:<key> / regex:<pattern> / exitcode", loc)
        pat = m.group("pat") if m else None
        if pat:
            try:
                compiled = re.compile(pat)
            except re.error as exc:
                raise SpecError("extract-regex", f"正则无法编译: {exc}", loc) from exc
            if len(compiled.groupindex) > REGEX_NAMED_GROUP_WARN or len(pat) > REGEX_LENGTH_WARN:
                warnings.append(
                    f"{loc}: regex 取值器较复杂（命名组 {len(compiled.groupindex)} 个、"
                    f"长度 {len(pat)}）。请确认没有在用正则把判断硬编码进 spec —— "
                    f"路由键应来自产物的确定性解析，判断应留在产物里。[启发式提示，非阻断]")


def _validate_route(step: dict, sid: str, ids: set[str], steps_by_id: dict[str, dict]) -> None:
    route = step.get("route")
    if route is None:
        return
    loc = f"steps.{sid}.route"
    _require(isinstance(route, dict) and route, "route-shape", "route 必须是非空映射", loc)

    extract = step.get("extract") or {}
    _require(bool(extract), "route-without-extract",
             "声明了 route 就必须有 extract 提供路由键", loc)
    if len(extract) > 1:
        _require(isinstance(step.get("route_on"), str) and step["route_on"] in extract,
                 "route-ambiguous",
                 f"extract 有 {len(extract)} 个取值，必须用 route_on 指明用哪个做路由键", loc)

    _require(ROUTE_FALLBACK in route, "route-no-fallback",
             '每个 route 必须显式写 "*" 兜底：未预期的取值属判断分歧，'
             "应 fail-open 交回 agent，而不是让 runner 猜", loc)
    for key, target in route.items():
        _require(isinstance(target, str) and target in ids, "route-target-missing",
                 f"路由目标 `{target}` 不是已定义的步骤 id", f"{loc}.{key}")
    fb = steps_by_id[route[ROUTE_FALLBACK]]
    _require(fb.get("type") == STEP_PAUSE, "route-fallback-not-pause",
             f'"*" 兜底目标必须是 pause 步骤（当前是 {fb.get("type")}）：'
             "未匹配的路由键是判断分歧，必须交回 agent 裁决", loc)


def _validate_hooks(step: dict, sid: str, kind: str) -> None:
    hooks = step.get(kind)
    if hooks is None:
        return
    loc = f"steps.{sid}.{kind}"
    _require(isinstance(hooks, list) and hooks, "hook-shape", f"{kind} 必须是非空列表", loc)
    for i, hook in enumerate(hooks):
        hloc = f"{loc}[{i}]"
        _require(isinstance(hook, dict), "hook-shape", "内联 hook 必须是映射", hloc)
        argv = hook.get("run")
        _require(isinstance(argv, list) and argv and all(isinstance(a, str) and a for a in argv),
                 "hook-run", "内联 hook 的 run 必须是非空字符串列表（argv）", hloc)
        expect = hook.get("expect")
        if expect is not None:
            _require(isinstance(expect, str) and bool(expect),
                     "hook-expect", "expect 必须是非空字符串", hloc)
            try:
                re.compile(expect)
            except re.error as exc:
                raise SpecError("hook-expect-regex", f"expect 正则无法编译: {exc}", hloc) from exc


def _validate_step_body(step: dict, sid: str, spec_dir: Path,
                        allowed_models: frozenset[str] | set[str] | None) -> None:
    stype = step["type"]
    loc = f"steps.{sid}"
    if stype == STEP_DISPATCH:
        for field in ("model", "prompt", "output"):
            _require(isinstance(step.get(field), str) and bool(step[field]),
                     "dispatch-field", f"dispatch 步骤缺少必填字段 `{field}`", loc)
        if allowed_models is not None and "{{" not in step["model"]:
            _require(step["model"] in allowed_models, "dispatch-model",
                     f"模型 `{step['model']}` 不在 PISR 白名单内", f"{loc}.model")
        prompt = step["prompt"]
        if "{{" not in prompt:
            p = Path(prompt)
            if not p.is_absolute():
                p = spec_dir / p
            _require(p.is_file(), "dispatch-prompt-missing",
                     f"prompt 文件不存在: {p}", f"{loc}.prompt")
        if "timeout_min" in step:
            tm = step["timeout_min"]
            _require(isinstance(tm, int) and not isinstance(tm, bool) and tm > 0,
                     "dispatch-timeout", "timeout_min 必须是正整数", f"{loc}.timeout_min")
        fp = step.get("forbid_paths")
        if fp is not None:
            _require(isinstance(fp, list) and all(isinstance(x, str) and x for x in fp),
                     "dispatch-forbid-paths", "forbid_paths 必须是字符串列表", f"{loc}.forbid_paths")
        tools = step.get("tools")
        if tools is not None:
            _require(isinstance(tools, list) and tools
                     and all(isinstance(x, str) and x.strip() for x in tools),
                     "dispatch-tools", "tools 必须是非空字符串列表（pi --tools 白名单）",
                     f"{loc}.tools")
        thinking = step.get("thinking")
        if thinking is not None:
            _require(isinstance(thinking, str) and bool(thinking),
                     "dispatch-thinking", "thinking 必须是非空字符串（--thinking 档位）",
                     f"{loc}.thinking")
        capture_reply = step.get("capture_reply")
        if capture_reply is not None:
            _require(isinstance(capture_reply, bool),
                     "dispatch-capture-reply", "capture_reply 必须是布尔值",
                     f"{loc}.capture_reply")
        meta = step.get("meta")
        if meta is not None:
            # `meta` 透传给 ocsr 的遥测归因（task_id / plan_ref / scope 等）。
            # 注意它的 `scope` 与步骤的 `scope` 字段是**两回事**：
            # 后者是 runner 的分组计数键（D3），runner 不理解其含义；
            # 前者是 ocsr 遥测的归因字段。二者刻意不混用。
            _require(isinstance(meta, dict)
                     and all(isinstance(k, str) and isinstance(v, str) for k, v in meta.items()),
                     "dispatch-meta", "meta 必须是字符串到字符串的映射", f"{loc}.meta")
    elif stype == STEP_HOOK:
        argv = step.get("run")
        _require(isinstance(argv, list) and argv and all(isinstance(a, str) and a for a in argv),
                 "hook-run", "hook 步骤的 run 必须是非空字符串列表（argv）", loc)
        if step.get("expect") is not None:
            _require(isinstance(step["expect"], str) and bool(step["expect"]),
                     "hook-expect", "expect 必须是非空字符串", loc)
            try:
                re.compile(step["expect"])
            except re.error as exc:
                raise SpecError("hook-expect-regex", f"expect 正则无法编译: {exc}", loc) from exc
    elif stype == STEP_PAUSE:
        _require(isinstance(step.get("question"), str) and bool(step["question"]),
                 "pause-question", "pause 步骤必须有 question", loc)
        opts = step.get("options")
        _require(isinstance(opts, list) and opts and all(isinstance(o, str) and o for o in opts),
                 "pause-options", "pause 步骤必须有非空 options 列表", loc)
    elif stype == STEP_ASSERT:
        conds = step.get("assert")
        _require(isinstance(conds, dict) and conds,
                 "assert-empty", "assert 步骤必须至少声明一个条件", loc)
        unknown = set(conds) - {"file_exists", "non_empty", "matches"}
        _require(not unknown, "assert-unknown",
                 f"未知的 assert 条件: {sorted(unknown)}（支持 file_exists / non_empty / matches）", loc)
        if "matches" in conds:
            try:
                re.compile(str(conds["matches"]))
            except re.error as exc:
                raise SpecError("assert-regex", f"matches 正则无法编译: {exc}", loc) from exc


def _graph_edges(step: dict, ids: set[str], sid: str) -> list[str]:
    """步骤的出边。

    `pause` 的 options 中指向步骤 id 的项**是真实的出边**：
    某些步骤可能只经由 pause 的裁决到达，若不计入，可达性检查会误判其不可达。
    保留字（PAUSE_RESERVED_OPTIONS）不是步骤 id，也不构成边。
    """
    edges: list[str] = []
    route = step.get("route")
    if isinstance(route, dict):
        edges.extend(t for t in route.values() if isinstance(t, str))
    nxt = step.get("next")
    if nxt is not None:
        _require(isinstance(nxt, str) and nxt in ids, "next-target-missing",
                 f"next 目标 `{nxt}` 不是已定义的步骤 id", f"steps.{sid}.next")
        edges.append(nxt)
    if step.get("type") == STEP_PAUSE:
        for opt in step.get("options") or []:
            if opt in PAUSE_RESERVED_OPTIONS:
                continue
            _require(opt in ids, "pause-option-unknown",
                     f"pause 选项 `{opt}` 既不是保留字 "
                     f"{sorted(PAUSE_RESERVED_OPTIONS)}，也不是已定义的步骤 id",
                     f"steps.{sid}.options")
            edges.append(opt)
    return list(dict.fromkeys(edges))


def _detect_cycle(adj: dict[str, list[str]], entry: str) -> list[str] | None:
    """返回环路径（若存在）。用显式栈做 DFS，避免深图递归超限。"""
    WHITE, GREY, BLACK = 0, 1, 2
    color = {k: WHITE for k in adj}
    parent: dict[str, str | None] = {}
    stack: list[tuple[str, int]] = [(entry, 0)]
    parent[entry] = None
    while stack:
        node, i = stack.pop()
        if i == 0:
            if color[node] != WHITE:
                continue
            color[node] = GREY
        if i < len(adj[node]):
            stack.append((node, i + 1))
            nxt = adj[node][i]
            if color.get(nxt) == GREY:
                path = [nxt]
                cur: str | None = node
                while cur is not None and cur != nxt:
                    path.append(cur)
                    cur = parent.get(cur)
                path.append(nxt)
                return list(reversed(path))
            if color.get(nxt) == WHITE:
                parent[nxt] = node
                stack.append((nxt, 0))
        else:
            color[node] = BLACK
    return None


def validate_spec(spec: dict, *, spec_path: Path,
                  allowed_models: frozenset[str] | set[str] | None = None) -> dict:
    """离线校验 spec。合法则返回结构化摘要；违反契约抛 SpecError（fail-closed）。

    不发起任何模型调用、不启动任何进程、不写任何文件。
    """
    warnings: list[str] = []
    spec_dir = spec_path.parent

    _require(spec.get("version") == SPEC_VERSION, "spec-version",
             f"仅支持 version: {SPEC_VERSION}（实际 {spec.get('version')!r}）")

    run = spec.get("run")
    _require(isinstance(run, dict), "run-shape", "缺少 run 段或其不是映射", "run")
    for field in ("id", "workdir"):
        _require(isinstance(run.get(field), str) and bool(run[field]),
                 "run-field", f"run.{field} 必填", "run")
    _require(bool(SAFE_ID_RE.match(run["id"])), "run-id",
             f"run.id `{run['id']}` 不是合法标识（[A-Za-z0-9][A-Za-z0-9._-]{{0,63}}）", "run.id")

    variables = spec.get("vars") or {}
    _require(isinstance(variables, dict), "vars-shape", "vars 必须是映射", "vars")

    steps = spec.get("steps")
    _require(isinstance(steps, list) and steps, "steps-empty", "steps 必须是非空列表", "steps")

    ids: set[str] = set()
    steps_by_id: dict[str, dict] = {}
    for idx, step in enumerate(steps):
        loc = f"steps[{idx}]"
        _require(isinstance(step, dict), "step-shape", "步骤必须是映射", loc)
        sid = step.get("id")
        _require(isinstance(sid, str) and bool(SAFE_ID_RE.match(sid or "")),
                 "step-id", f"步骤 id 非法: {sid!r}", loc)
        _require(sid not in ids, "step-id-duplicate", f"步骤 id 重复: {sid}", loc)
        stype = step.get("type")
        _require(stype in STEP_TYPES, "step-type",
                 f"未知步骤类型 {stype!r}。类型集合是封闭的，仅支持 "
                 f"{sorted(STEP_TYPES)}；新增类型须走治理文档变更的独立复查", loc)
        ids.add(sid)
        steps_by_id[sid] = step

    for sid, step in steps_by_id.items():
        _validate_step_body(step, sid, spec_dir, allowed_models)
        _validate_hooks(step, sid, "pre")
        _validate_hooks(step, sid, "post")
        _validate_extract(step, sid, warnings)
        _validate_route(step, sid, ids, steps_by_id)

    # 图：边合法性、无环、可达性
    adj: dict[str, list[str]] = {}
    for sid, step in steps_by_id.items():
        adj[sid] = _graph_edges(step, ids, sid)
    entry = steps[0]["id"]
    cycle = _detect_cycle(adj, entry)
    _require(cycle is None, "graph-cycle",
             f"步骤图存在环: {' -> '.join(cycle or [])}", "steps")

    reachable = {entry}
    frontier = [entry]
    while frontier:
        cur = frontier.pop()
        for nxt in adj[cur]:
            if nxt not in reachable:
                reachable.add(nxt)
                frontier.append(nxt)
    unreachable = sorted(ids - reachable)
    _require(not unreachable, "graph-unreachable",
             f"以下步骤从入口 `{entry}` 不可达: {unreachable}。"
             "不可达步骤通常是路由写错的征兆，fail-closed。", "steps")

    # 模板引用可解析性
    refs: dict[str, list[str]] = {}
    for sid, step in steps_by_id.items():
        for ref in iter_template_refs(step):
            refs.setdefault(ref, []).append(sid)
    for ref in iter_template_refs(run):
        refs.setdefault(ref, []).append("run")
    for ref, users in sorted(refs.items()):
        err = _resolve_ref(ref, spec, steps_by_id)
        _require(err is None, "template-unresolvable",
                 f"模板引用 {{{{{ref}}}}} {err}", f"引用处: {sorted(set(users))}")

    return build_summary(spec, steps_by_id, adj, entry, refs, warnings)


# ─── 结构化摘要（D8 / S3）────────────────────────────────────────────

def build_summary(spec: dict, steps_by_id: dict[str, dict], adj: dict[str, list[str]],
                  entry: str, refs: dict[str, list[str]], warnings: list[str]) -> dict:
    """对 spec 的确定性展开，供 orchestrator 在派发前肉眼复核。

    这里**不做任何判断**，只是把 spec 里已经写明的东西重新组织后呈现。
    """
    step_rows = []
    for sid, step in steps_by_id.items():
        route = step.get("route") or {}
        step_rows.append({
            "id": sid,
            "type": step["type"],
            "model": step.get("model", ""),
            "scope": step.get("scope", ""),
            "pre_hooks": len(_hook_list(step, "pre")),
            "post_hooks": len(_hook_list(step, "post")),
            "extract": sorted((step.get("extract") or {}).keys()),
            "edges": adj[sid],
            "fallback": route.get(ROUTE_FALLBACK, ""),
        })
    scope_keys = sorted({s.get("scope") for s in steps_by_id.values() if s.get("scope")})
    return {
        "run_id": spec["run"]["id"],
        "workdir": spec["run"]["workdir"],
        "entry": entry,
        "step_count": len(steps_by_id),
        "steps": step_rows,
        "route_graph": {sid: adj[sid] for sid in steps_by_id},
        "scope_keys": scope_keys,
        "var_refs": {ref: sorted(set(users)) for ref, users in sorted(refs.items())},
        "warnings": warnings,
    }


def format_summary(summary: dict) -> str:
    """人读格式。摘要本身不判断对错，只把 spec 展开给人看。"""
    out: list[str] = []
    out.append(f"run.id     : {summary['run_id']}")
    out.append(f"run.workdir: {summary['workdir']}")
    out.append(f"入口步骤   : {summary['entry']}    步骤数: {summary['step_count']}")
    if summary["scope_keys"]:
        out.append(f"分组键     : {', '.join(summary['scope_keys'])}"
                   "    （runner 按其分组计数，不理解含义）")
    out.append("")
    out.append("步骤：")
    head = f"  {'id':<16}{'type':<10}{'model':<28}{'pre/post':<10}{'extract'}"
    out.append(head)
    out.append("  " + "-" * (len(head) - 2))
    for s in summary["steps"]:
        out.append(f"  {s['id']:<16}{s['type']:<10}{(s['model'] or '-'):<28}"
                   f"{str(s['pre_hooks']) + '/' + str(s['post_hooks']):<10}"
                   f"{','.join(s['extract']) or '-'}")
    out.append("")
    out.append("路由图：")
    for sid, edges in summary["route_graph"].items():
        out.append(f"  {sid} -> {', '.join(edges) if edges else '（终止）'}")
    out.append("")
    out.append("模板引用：")
    for ref, users in summary["var_refs"].items():
        out.append(f"  {{{{{ref}}}}}  ← {', '.join(users)}")
    if not summary["var_refs"]:
        out.append("  （无）")
    if summary["warnings"]:
        out.append("")
        out.append("启发式提示（非阻断）：")
        for w in summary["warnings"]:
            out.append(f"  ⚠️ {w}")
    return "\n".join(out)


def validate_file(spec_path: Path, *,
                  allowed_models: frozenset[str] | set[str] | None = None) -> dict:
    return validate_spec(load_spec(spec_path), spec_path=spec_path, allowed_models=allowed_models)


def summary_json(summary: dict) -> str:
    return json.dumps(summary, ensure_ascii=False, indent=2)


# ═══════════════════════════════════════════════════════════════════
# 执行引擎（阶段 3）：journal / resume / pause + hook / assert
# ═══════════════════════════════════════════════════════════════════

import hashlib
import subprocess
import datetime as _dt

# 退出码契约（与 refs/dispatch-patterns.md 的 dispatch 退出码是两套，勿混用）
EXIT_OK = 0
EXIT_STEP_FAILED = 1
EXIT_SPEC_INVALID = 2
EXIT_PAUSED = 10            # D6：等待 agent 裁决
EXIT_RESUME_AMBIGUOUS = 11  # D5：started 无 completed，不确定即停机

JOURNAL_NAME = "journal.jsonl"
PAUSE_REQUEST_NAME = "pause-request.json"
DEFAULT_HOOK_TIMEOUT_SEC = 300


def _now_iso() -> str:
    return _dt.datetime.now().astimezone().isoformat()


def _sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def _sha256_file(p: Path) -> str:
    try:
        return _sha256_bytes(p.read_bytes())
    except OSError:
        return ""


class RunHalt(Exception):
    """需要停机并把控制权交回 agent。code 决定进程退出码。"""

    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


# ─── journal ────────────────────────────────────────────────────────

class Journal:
    """append-only 步骤日志。started/completed 两段式（D5）。

    先写 `step-started`（含解析后的输入），副作用发生后才写 `step-completed`。
    resume 时遇到 started 而无 completed/paused，一律停机交回 agent、**禁止自动重跑**：
    该步可能已经消耗了一次真实模型调用与一次预算 reservation，重跑会双花且破坏账本。
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self._seq = 0

    def read(self) -> list[dict]:
        if not self.path.is_file():
            return []
        rows = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                raise RunHalt(EXIT_RESUME_AMBIGUOUS,
                              f"journal 存在无法解析的行，状态不确定，停机：{self.path}")
        self._seq = max((r.get("seq", 0) for r in rows), default=0)
        return rows

    def append(self, event: str, **fields) -> dict:
        self._seq += 1
        row = {"event": event, "seq": self._seq, "ts": _now_iso(), **fields}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
        return row


def replay(rows: list[dict]) -> dict:
    """从 journal 重建执行状态。

    返回 {position, awaiting_answer, captures, scope_counters, completed, spec_sha256}
    position 为 None 表示尚未开始（从入口步骤起跑）。
    """
    captures: dict[str, dict] = {}
    scope_counters: dict[str, int] = {}
    completed: list[str] = []
    position: str | None = None
    awaiting: str | None = None
    spec_sha: str | None = None
    open_step: str | None = None

    for r in rows:
        ev = r.get("event")
        if ev == "run-started":
            spec_sha = r.get("spec_sha256")
        elif ev == "step-started":
            if open_step is not None:
                raise RunHalt(EXIT_RESUME_AMBIGUOUS,
                              f"步骤 `{open_step}` 有 step-started 但无 step-completed/paused，"
                              "无法判断其副作用是否已发生。**禁止自动重跑**——"
                              "该步可能已消耗真实模型调用与预算 reservation。"
                              "请人工核实后决定：补记结果、或改用 --answer 处理。")
            open_step = r.get("step")
            idx = r.get("scope_index")
            key = r.get("scope_key")
            if key and isinstance(idx, int):
                scope_counters[key] = max(scope_counters.get(key, 0), idx)
        elif ev == "step-completed":
            open_step = None
            sid = r.get("step")
            completed.append(sid)
            if r.get("captures"):
                captures[sid] = r["captures"]
            if r.get("status") == "failed":
                position = None
                awaiting = None
                continue
            position = r.get("next")
        elif ev == "step-paused":
            open_step = None
            awaiting = r.get("step")
            position = r.get("step")
        elif ev == "run-finished":
            position = None
            awaiting = None

    if open_step is not None:
        raise RunHalt(EXIT_RESUME_AMBIGUOUS,
                      f"步骤 `{open_step}` 有 step-started 但无 step-completed/paused，"
                      "无法判断其副作用是否已发生。**禁止自动重跑**——"
                      "该步可能已消耗真实模型调用与预算 reservation。"
                      "请人工核实后决定：补记结果、或改用 --answer 处理。")

    return {"position": position, "awaiting_answer": awaiting, "captures": captures,
            "scope_counters": scope_counters, "completed": completed, "spec_sha256": spec_sha}


# ─── 模板渲染（运行期）──────────────────────────────────────────────

def render(value: Any, ctx: dict) -> Any:
    """按 D10 的文法边界渲染模板。校验期已保证引用可解析，此处不再做判断。"""
    if isinstance(value, str):
        def sub(m: "re.Match[str]") -> str:
            return str(_resolve_value(m.group(1).strip(), ctx))
        return TEMPLATE_RE.sub(sub, value)
    if isinstance(value, list):
        return [render(v, ctx) for v in value]
    if isinstance(value, dict):
        return {k: render(v, ctx) for k, v in value.items()}
    return value


def _resolve_value(ref: str, ctx: dict) -> Any:
    m = _T_RUN.match(ref)
    if m:
        return ctx["run"][m.group(1)]
    m = _T_VARS.match(ref)
    if m:
        return ctx["vars"][m.group(1)]
    m = _T_SCOPE.match(ref)
    if m:
        return ctx["scope_index"].get(m.group(1), "")
    m = _T_HOOKCAP.match(ref)
    if m:
        sid, kind, idx, name = m.group(1), m.group(2), int(m.group(3)), m.group(4)
        return ctx["captures"].get(sid, {}).get(f"{kind}[{idx}]", {}).get(name, "")
    m = _T_STEPCAP.match(ref)
    if m:
        sid, name = m.group(1), m.group(2)
        return ctx["captures"].get(sid, {}).get(SELF_CAPTURE_KEY, {}).get(name, "")
    raise RunHalt(EXIT_SPEC_INVALID, f"运行期无法解析模板引用 {{{{{ref}}}}}")


# ─── 取值与路由 ──────────────────────────────────────────────────────

def extract_value(expr: str, *, text: str, exitcode: int) -> str | None:
    """按封闭枚举取值。**不做任何判断**——只是确定性解析。

    取值来源（`text`）按步骤类型确定，二者刻意不同：

    - `hook` 步骤   → 进程的 **stdout + stderr**（外部命令的自然产出）
    - `dispatch` 步骤 → **产物文件的内容**（子代理的报告写在文件里，不靠 stdout 回传，
      这正是 SKILL.md「默认单 worker 闭环」中“不采信自我报告、只信文件系统证据”的直接体现）

    `assert` 步骤只能用 `exitcode`（它不产生文本产出）。返回 ``None`` 表示
    无法得到一个明确、非空的值；这不是未知路由值，调用方必须 fail-closed。
    """
    if expr == "exitcode":
        return str(exitcode)
    if expr.startswith("yaml:"):
        key = expr[len("yaml:"):]
        return _first_yaml_top_key(text, key)
    if expr.startswith("regex:"):
        m = re.search(expr[len("regex:"):], text, re.MULTILINE | re.DOTALL)
        if not m:
            return None
        if m.groupdict():
            value = next((v for v in m.groupdict().values() if v is not None), "")
        else:
            value = m.group(1) if m.groups() else m.group(0)
        return value if value else None
    raise RunHalt(EXIT_SPEC_INVALID, f"未知取值器: {expr}")


def _first_yaml_top_key(text: str, key: str) -> str | None:
    """只取第一个 fenced YAML 块的顶层键；其余块和全文都不参与兜底。"""
    import yaml  # 校验期已确认可用
    blocks = re.findall(r"```(?:ya?ml)?\s*\n(.*?)```", text, re.DOTALL)
    if not blocks:
        return None
    try:
        data = yaml.safe_load(blocks[0])
    except Exception:
        return None
    if not isinstance(data, dict) or key not in data:
        return None
    value = "" if data[key] is None else str(data[key])
    return value if value else None


def route_next(step: dict, value: str) -> tuple[str | None, str]:
    """按路由表决定下一步。未匹配走 `"*"`（校验期已保证它指向 pause）。"""
    route = step.get("route")
    if not route:
        return step.get("next"), ""
    if value in route:
        return route[value], value
    return route[ROUTE_FALLBACK], ROUTE_FALLBACK


# ─── 步骤执行 ────────────────────────────────────────────────────────

def _inside(path: Path, root: Path) -> bool:
    try:
        Path(path).resolve().relative_to(Path(root).resolve())
        return True
    except (ValueError, OSError):
        return False


def _run_hook(hook: dict, ctx: dict, label: str) -> tuple[bool, dict, str]:
    """执行一个 hook（独立步骤或内联 pre/post）。

    返回 (ok, captures, output_text)。`expect` 不匹配或 rc≠0 即失败——契约违反 fail-closed。
    """
    argv = [str(a) for a in render(hook["run"], ctx)]
    timeout = hook.get("timeout_sec", DEFAULT_HOOK_TIMEOUT_SEC)
    try:
        proc = subprocess.run(argv, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=timeout)
    except FileNotFoundError:
        return (False, {}, f"{label}: 可执行文件不存在: {argv[0]}")
    except subprocess.TimeoutExpired:
        return (False, {}, f"{label}: 超时 {timeout}s: {' '.join(argv[:4])}")
    out = (proc.stdout or "") + (proc.stderr or "")
    if proc.returncode != 0:
        return (False, {}, f"{label}: rc={proc.returncode} :: {out.strip()[:300]}")
    expect = hook.get("expect")
    if expect:
        m = re.search(expect, out, re.MULTILINE)
        if not m:
            return (False, {}, f"{label}: expect 未匹配 `{expect}` :: {out.strip()[:300]}")
        return (True, dict(m.groupdict()), out)
    return (True, {}, out)


def _run_inline_hooks(step: dict, kind: str, ctx: dict, sid: str) -> tuple[bool, str]:
    """执行 dispatch 步骤的内联 hook。

    IMP-1：`pre` / `post` **不是独立步骤类型**——语法（`run` argv + 可选 `expect`）
    与语义都同 `hook` 步骤，区别只在生命周期：内联 hook 绑定在宿主步骤的前/后，
    独立 `hook` 步骤是图上的节点。
    """
    for i, hook in enumerate(_hook_list(step, kind)):
        ok, caps, detail = _run_hook(hook, ctx, f"steps.{sid}.{kind}[{i}]")
        ctx.setdefault("captures", {}).setdefault(sid, {})[f"{kind}[{i}]"] = caps
        if not ok:
            return (False, detail)
    return (True, "")


def _run_assert(step: dict, ctx: dict) -> tuple[bool, str]:
    conds = render(step["assert"], ctx)
    target = conds.get("file_exists")
    if target:
        p = Path(str(target))
        if not p.is_file():
            return (False, f"assert file_exists 失败: {p}")
        if conds.get("non_empty") and p.stat().st_size == 0:
            return (False, f"assert non_empty 失败（0 字节）: {p}")
        pat = conds.get("matches")
        if pat:
            body = p.read_text(encoding="utf-8", errors="replace")
            if not re.search(str(pat), body, re.MULTILINE | re.DOTALL):
                return (False, f"assert matches 失败: {p} 不含 /{pat}/")
    elif conds.get("non_empty") or conds.get("matches"):
        return (False, "assert non_empty/matches 需要同时给出 file_exists")
    return (True, "")


def _write_pause_request(workdir: Path, spec_path: Path, step: dict,
                         sid: str, ctx: dict) -> Path:
    req = {
        "run_id": ctx["run"]["id"],
        "step": sid,
        "question": render(step["question"], ctx),
        "options": step["options"],
        "reserved_options": sorted(PAUSE_RESERVED_OPTIONS),
        "journal": str(workdir / JOURNAL_NAME),
        "context_snapshot": {
            "scope_index": dict(ctx.get("scope_index", {})),
            "captures": ctx.get("captures", {}),
            "completed": list(ctx.get("completed", [])),
        },
        "resume_hint": (f"python scripts/pisr_dispatch.py run --spec {spec_path} "
                        f"--resume --answer {sid}=<option>"),
        "note": ("判断权在 agent：runner 只提供选项，不自行裁决。"
                 "abort 表示终止本次 run；其余选项为下一步骤 id。"),
    }
    p = workdir / PAUSE_REQUEST_NAME
    p.write_text(json.dumps(req, ensure_ascii=False, indent=2), encoding="utf-8")
    return p


def _execute_dispatch(step: dict, sid: str, ctx: dict, workdir: Path,
                      dispatch_fn) -> tuple[bool, str, str, int]:
    """dispatch 步骤。dispatch_fn 是阶段 5 的接线点。

    未注入 dispatch_fn 时 fail-closed —— **不假装成功**。
    """
    if dispatch_fn is None:
        return (False,
                f"steps.{sid}: dispatch 步骤的执行能力属阶段 5，尚未接线。"
                "见 docs/plans/active/20260810-deterministic-run-spec.md 阶段表。",
                "", 1)
    out_path = Path(render(step["output"], ctx))
    if not _inside(out_path, workdir):
        return (False,
                f"steps.{sid}: 产物路径 `{out_path}` 在 runner 独占的 workdir "
                f"`{workdir}` 之外（D7）", "", 1)
    ok, detail = _run_inline_hooks(step, "pre", ctx, sid)
    if not ok:
        return (False, detail, "", 1)
    rc = dispatch_fn(step, sid, ctx, out_path)
    text = out_path.read_text(encoding="utf-8", errors="replace") if out_path.is_file() else ""
    if rc != 0:
        return (False, f"steps.{sid}: dispatch 返回 rc={rc}", text, rc)
    ok, detail = _run_inline_hooks(step, "post", ctx, sid)
    if not ok:
        return (False, detail, text, rc)
    return (True, "", text, rc)


# ─── 主循环 ──────────────────────────────────────────────────────────

def execute(spec: dict, spec_path: Path, *, resume: bool = False,
            answers: dict[str, str] | None = None,
            dispatch_fn=None, allowed_models=None) -> int:
    """执行 spec。runner **独占** workdir（D7）。

    dispatch_fn 由调用方注入（依赖倒置：本模块不 import pisr_dispatch，
    避免循环依赖，同时让引擎可脱离派发能力独立测试）。
    """
    validate_spec(spec, spec_path=spec_path, allowed_models=allowed_models)
    answers = answers or {}
    steps_by_id = {s["id"]: s for s in spec["steps"]}

    ctx: dict = {
        "run": {"id": spec["run"]["id"], "workdir": ""},
        "vars": dict(spec.get("vars") or {}),
        "scope_index": {},
        "captures": {},
        "completed": [],
    }
    ctx["run"]["workdir"] = render(spec["run"]["workdir"], ctx)
    workdir = Path(ctx["run"]["workdir"])
    workdir.mkdir(parents=True, exist_ok=True)

    journal = Journal(workdir / JOURNAL_NAME)
    rows = journal.read()
    spec_sha = _sha256_bytes(spec_path.read_bytes())

    if rows and not resume:
        raise RunHalt(EXIT_RESUME_AMBIGUOUS,
                      f"workdir 已存在 journal（{journal.path}）。"
                      "续跑请加 --resume；重跑请换 run.workdir 或清理该目录。"
                      "runner 不会自行覆盖既有执行记录。")

    state = replay(rows)
    if rows:
        if state["spec_sha256"] and state["spec_sha256"] != spec_sha:
            raise RunHalt(EXIT_RESUME_AMBIGUOUS,
                          "spec 自上次执行后已改变（sha256 不符）。"
                          "续跑会把新 spec 的语义套到旧 journal 上，停机。")
        ctx["captures"] = dict(state["captures"])
        ctx["completed"] = list(state["completed"])
        ctx["scope_index"] = dict(state["scope_counters"])
    else:
        journal.append("run-started", run_id=ctx["run"]["id"], spec_sha256=spec_sha,
                       workdir=str(workdir))

    scope_counters: dict[str, int] = dict(state["scope_counters"])
    cur: str | None = state["position"] or spec["steps"][0]["id"]
    awaiting = state["awaiting_answer"]

    while cur:
        step = steps_by_id[cur]
        sid = step["id"]
        stype = step["type"]

        # 处于暂停态：必须由 agent 给出裁决才能继续
        if awaiting == sid:
            ans = answers.get(sid)
            if not ans:
                raise RunHalt(EXIT_PAUSED,
                              f"步骤 `{sid}` 处于暂停态，需 --answer {sid}=<option>。"
                              f"可选：{step['options']}")
            if ans not in step["options"]:
                raise RunHalt(EXIT_SPEC_INVALID,
                              f"`{ans}` 不在 `{sid}` 的 options 内：{step['options']}")
            nxt = None if ans in PAUSE_RESERVED_OPTIONS else ans
            journal.append("step-completed", step=sid, status="ok", answer=ans, next=nxt)
            (workdir / PAUSE_REQUEST_NAME).unlink(missing_ok=True)
            ctx["completed"].append(sid)
            awaiting = None
            if ans == "abort":
                journal.append("run-finished", status="aborted")
                print(f"[run] 🛑 按 agent 裁决终止：{sid} → abort")
                return EXIT_STEP_FAILED
            if ans == "retry":
                raise RunHalt(EXIT_SPEC_INVALID,
                              "retry 表示重跑**触发本次暂停的上游步骤**，当前实现尚未支持；"
                              "请改选具体步骤 id，或 abort 后人工处理。")
            cur = nxt
            continue

        # scope 计数由 runner 按分组键单调派生——调用方不写数字（D3）
        scope_key = step.get("scope")
        scope_index = None
        if scope_key:
            scope_counters[scope_key] = scope_counters.get(scope_key, 0) + 1
            scope_index = scope_counters[scope_key]
            ctx["scope_index"][scope_key] = scope_index

        journal.append("step-started", step=sid, type=stype,
                       scope_key=scope_key, scope_index=scope_index)
        print(f"[run] ▶ {sid} ({stype})")

        if stype == STEP_PAUSE:
            _write_pause_request(workdir, spec_path, step, sid, ctx)
            journal.append("step-paused", step=sid, options=step["options"])
            print(f"[run] ⏸ 暂停于 `{sid}`：{render(step['question'], ctx)}")
            print(f"[run]    选项 {step['options']}；请求已写入 {workdir / PAUSE_REQUEST_NAME}")
            return EXIT_PAUSED

        ok, detail, text, rc = True, "", "", 0
        if stype == STEP_HOOK:
            ok, caps, out = _run_hook(step, ctx, f"steps.{sid}")
            if ok:
                ctx["captures"].setdefault(sid, {})[SELF_CAPTURE_KEY] = caps
                text = out
            else:
                detail = out
                rc = 1
        elif stype == STEP_ASSERT:
            ok, detail = _run_assert(step, ctx)
            rc = 0 if ok else 1
        elif stype == STEP_DISPATCH:
            ok, detail, text, rc = _execute_dispatch(step, sid, ctx, workdir, dispatch_fn)

        caps_for_journal = ctx["captures"].get(sid, {})
        if not ok:
            journal.append("step-completed", step=sid, status="failed",
                           detail=detail[:1000], captures=caps_for_journal)
            journal.append("run-finished", status="failed")
            print(f"[run] ❌ {sid} 失败: {detail[:300]}", file=sys.stderr)
            return EXIT_STEP_FAILED

        route_key = ""
        extract = step.get("extract") or {}
        if extract:
            name = step.get("route_on") or next(iter(extract))
            route_key = extract_value(extract[name], text=text, exitcode=rc)
            if route_key is None:
                detail = (f"路由键 `{name}` 无法从 `{extract[name]}` 得到非空、明确的值；"
                          "抽取失败不得走 `*` 兜底路由")
                journal.append("step-completed", step=sid, status="failed",
                               detail=detail, captures=caps_for_journal)
                journal.append("run-finished", status="failed")
                print(f"[run] ❌ {sid} 失败: {detail}", file=sys.stderr)
                return EXIT_STEP_FAILED
        nxt, matched = route_next(step, route_key)
        journal.append("step-completed", step=sid, status="ok", next=nxt,
                       route_key=route_key, route_matched=matched,
                       captures=caps_for_journal)
        ctx["completed"].append(sid)
        print(f"[run] ✅ {sid}"
              + (f"  route={route_key!r} → {nxt or '（终止）'}" if extract else ""))
        cur = nxt

    journal.append("run-finished", status="ok")
    print(f"[run] ✅ run 完成（{len(ctx['completed'])} 步）")
    return EXIT_OK


def execute_file(spec_path: Path, **kw) -> int:
    return execute(load_spec(spec_path), spec_path, **kw)
