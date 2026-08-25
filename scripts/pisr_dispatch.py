#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
pisr_dispatch.py — pisr (Pi Subagents Run) 派发后端（库内执行层，项目无关版）

把"prompt 副本注入 → Popen 直启 pi → 双监视看门狗 → 产物验证 → 遥测"整链固化为脚本。
脚本不做编排判断（选模型/选模式/prompt 内容仍由 agent 决定），仅提供可复用的执行层。

与 ocsr（opencode 后端）的执行层差异（设计既定，勿"修复"成 ocsr 形态）：
  - pi 经 `subprocess.Popen` 直启，无 PowerShell launcher / marker / pid.txt；
    prompt 以 `@file` 注入（无命令行长度与转义问题），stdout→events.jsonl、stderr→stderr.log。
  - `--tools` 是 pi 的进程级工具白名单；本驱动额外解析事件流做 toolcall 越权审计，
    越权即确定性失败（fail-closed）。注意：白名单不是安全沙箱。
  - 事件流提供真实 usage（usage_input/output/total/cost）；cost_estimate 恒 0（保留字段，schema 兼容）。
  - 无共享会话 DB → 无 DB 锁重派；驱动器**不做任何自动重派**（重派归 orchestrator，3 次上限）。

用法:
  python scripts/pisr_dispatch.py dispatch \
    --worker prompt-r1.txt|provider/model|R1 \
    --output-dir ./evidence \
    --watch --timeout 15

  python scripts/pisr_dispatch.py selftest            # 离线自检
  python scripts/pisr_dispatch.py selftest --model cc-switch-xiaomi-mi-mo/mimo-v2.5  # 在线冒烟
  python scripts/pisr_dispatch.py telemetry

遥测日志: ~/.pisr/dispatch-log.jsonl（本机共享，预期行为）
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import shutil
import subprocess
import sys
import textwrap
import time
from pathlib import Path

# ─── Windows UTF-8 ───────────────────────────────────────────────────
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except AttributeError:
        pass

# ─── 常量 ────────────────────────────────────────────────────────────
DEFAULT_STAGGER = 5       # 秒，多 worker 错峰间隔
DEFAULT_TIMEOUT = 15      # 分钟，单个 worker 看门狗阈值
CHECK_INTERVAL = 10       # 秒，watch 循环轮询间隔
DISPATCH_LOG = Path.home() / ".pisr" / "dispatch-log.jsonl"
PROMPT_ARG_MAX_BYTES = 262144  # prompt 副本 256KB 上限（@file 注入，超限 fail-closed）

# pi 内置工具名（文档基线；扩展可增项，审计不做硬枚举）
PI_BUILTIN_TOOLS = frozenset({"read", "bash", "powershell", "edit", "write", "grep", "find", "ls"})

# dispatch 退出码契约（单一事实源见 refs/dispatch-patterns.md §退出码契约）
#   0 = 全部 worker 落盘
#   1 = 看门狗超时（既有语义）
#   2 = 至少一个 worker 确定性失败（pi 非零退出 / pi 退出码为 0 但期望产物未落盘 /
#       toolcall 越权审计命中）
#   3 = 路径碰撞（既有语义）
# 混合结局优先级：3 > 1 > 2 > 0
EXIT_DETERMINISTIC_FAILURE = 2  # 至少一个 worker 确定性失败
EXIT_PATH_COLLISION = 3   # 既有文件被非预期覆盖时的退出码

# dispatch-log 角色 enum（与 ocsr 值域一致，便于跨后端汇总）
ROLE_EXECUTOR = "executor"
ROLE_REVIEWER = "reviewer"
ROLE_RELEASE_EXECUTOR = "release-executor"
ROLE_ULTRAVERGE_INITIAL = "ultraverge-initial"
ROLE_OUTER_REVIEWER = "outer-reviewer"
ROLE_BLIND_REVIEWER = "blind-reviewer"
ROLE_DESIGN_REVIEWER = "design-reviewer"
ROLE_ARBITER = "arbiter"
ROLE_FRESH_VERIFIER = "fresh-verifier"
ROLE_ORCHESTRATOR = "orchestrator"
ROLE_PLANNER = "planner"
ROLE_COMMANDER = "commander"
ROLE_WORKER = "worker"
ROLE_LEGACY = "legacy"
ROLE_VALUES = frozenset({
    ROLE_EXECUTOR, ROLE_REVIEWER, ROLE_RELEASE_EXECUTOR,
    ROLE_ULTRAVERGE_INITIAL, ROLE_OUTER_REVIEWER, ROLE_BLIND_REVIEWER,
    ROLE_DESIGN_REVIEWER, ROLE_ARBITER,
    ROLE_FRESH_VERIFIER, ROLE_ORCHESTRATOR, ROLE_PLANNER,
    ROLE_COMMANDER, ROLE_WORKER,
})

# scope enum
SCOPE_TASK_ENVELOPE = "task-envelope"
SCOPE_OUTER = "outer"
SCOPE_BLIND = "blind"
SCOPE_ULTRAVERGE = "ultraverge"
SCOPE_NONE = "none"
SCOPE_VALUES = frozenset({SCOPE_TASK_ENVELOPE, SCOPE_OUTER, SCOPE_BLIND,
                          SCOPE_ULTRAVERGE, SCOPE_NONE})

# 超时策略
TIMEOUT_POLICY_LEAF_KILL = "leaf_kill"        # 到期自动 kill 进程树
TIMEOUT_POLICY_HIERARCHICAL_REPORT = "hierarchical_report"  # 层级 orchestrator：到期报告/alive
TIMEOUT_POLICY_AUTO = "auto"                  # 默认：按角色自动解析
TIMEOUT_POLICY_VALUES = frozenset({TIMEOUT_POLICY_LEAF_KILL, TIMEOUT_POLICY_HIERARCHICAL_REPORT, TIMEOUT_POLICY_AUTO})

# 层级角色：auto 策略下这些角色自动解析为 hierarchical_report
_HIERARCHICAL_ROLE_PREFIXES = ("orchestrator", "planner", "commander", "ultraverge-initial", "arbiter")

# 遥测字段集合（供验证脚本引用，与 _append_telemetry 写出的字段保持一致）
# "required" = 始终存在, "optional" = 仅条件存在
TELEMETRY_FIELDS: dict[str, str] = {
    "ts": "required",
    "model": "required",
    "role": "required",
    "harness": "required",
    "channel": "required",
    "outcome": "required",
    "wall_min": "required",
    "artifact_bytes": "required",
    "task_id": "required",
    "plan_ref": "required",
    "scope": "required",
    "prompt_size_bytes": "required",
    "response_size_bytes": "required",
    "model_cost_input": "required",
    "model_cost_output": "required",
    "cost_estimate": "required",
    "blocking_chain": "required",
    "outcome_detail": "required",
    "failure_retry_index": "required",
    "usage_input": "required",
    "usage_output": "required",
    "usage_total_tokens": "required",
    "usage_cost": "required",
    "tool_calls": "required",
    "tool_violations": "required",
    "label": "optional",
    "note": "optional",
    "timeout_policy_requested": "optional",
    "timeout_policy_resolved": "optional",
    "forbid_paths": "optional",
    "read_audit": "optional",
    "tool_audit": "optional",
}

# 用户主目录前缀（路径隐私用）
_USER_HOME = str(Path.home())
_USER_HOME_FORWARD = _USER_HOME.replace("\\", "/")

# ─── pi 可执行文件解析 ───────────────────────────────────────────────
# shutil.which 解析 PATH 上的 pi / pi.cmd / pi.exe；找不到时保留裸名（报错信息更友好）。
PI_BIN = shutil.which("pi") or "pi"


# ─── 模型白名单 ───────────────────────────────────────────────────────
ALLOWED_MODELS_PATH = Path(__file__).resolve().parents[1] / "config" / "allowed-models.json"


def _load_allowed_models(path: Path = ALLOWED_MODELS_PATH) -> tuple[str, ...]:
    """Load the user-editable model allowlist and fail closed on bad configuration."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Cannot load PISR model allowlist from {path}: {exc}") from exc
    if not isinstance(raw, list) or not raw or any(
        not isinstance(item, str)
        or (parts := item.split("/")) != [part.strip() for part in parts]
        or len(parts) != 2
        or not all(parts)
        for item in raw
    ):
        raise RuntimeError(
            f"PISR model allowlist at {path} must be a non-empty JSON array of provider/model IDs."
        )
    if len(raw) != len(set(raw)):
        raise RuntimeError(f"PISR model allowlist at {path} contains duplicate model IDs.")
    return tuple(raw)


_CONFIGURED_MODELS = _load_allowed_models()
ALLOWED_MODELS = frozenset(_CONFIGURED_MODELS)
DEFAULT_MODEL = _CONFIGURED_MODELS[0]


def _validate_model_allowed(model: str) -> None:
    """Validate model is in the PISR allowlist. Raises ValueError with clear error."""
    if model not in ALLOWED_MODELS:
        allowed = ", ".join(sorted(ALLOWED_MODELS))
        raise ValueError(
            f"Model '{model}' is not in the PISR allowlist.\n"
            f"Allowed models: {allowed}\n"
            f"Run 'pi --list-models' to check whether any allowed model is configured locally."
        )


def _check_model_calls_disabled() -> None:
    """Fail-fast tripwire: if PISR_DISABLE_MODEL_CALLS=1, exit before any model call."""
    if os.environ.get("PISR_DISABLE_MODEL_CALLS") == "1":
        print(
            "PISR_DISABLE_MODEL_CALLS=1: model calls disabled. "
            "Set to 0 or unset to allow.",
            file=sys.stderr,
        )
        sys.exit(1)


def _sanitize_path(val: str) -> str:
    """Replace user-home prefix with <user-home> for path privacy in Git-tracked files."""
    if val.startswith(_USER_HOME):
        return "<user-home>" + val[len(_USER_HOME):]
    if val.startswith(_USER_HOME_FORWARD):
        return "<user-home>" + val[len(_USER_HOME_FORWARD):]
    return val


def _sanitize_ledger_row(row: dict) -> dict:
    """Sanitize path-like string values in a ledger row."""
    PATH_KEYS = {"prompt_file", "expected_output", "work_dir", "output", "detail"}
    result = {}
    for k, v in row.items():
        if isinstance(v, str) and k in PATH_KEYS:
            result[k] = _sanitize_path(v)
        else:
            result[k] = v
    return result


def _resolve_timeout_policy(policy: str, role: str) -> str:
    """Resolve auto policy to concrete policy based on role.

    - Auto resolves to hierarchical_report for orchestrator-like roles,
      leaf_kill for all others.
    - Explicit policies (leaf_kill, hierarchical_report) pass through unchanged.
    """
    if policy != TIMEOUT_POLICY_AUTO:
        return policy
    role_lower = role.lower()
    if any(role_lower.startswith(p) for p in _HIERARCHICAL_ROLE_PREFIXES):
        return TIMEOUT_POLICY_HIERARCHICAL_REPORT
    return TIMEOUT_POLICY_LEAF_KILL


# ─── 工具 ────────────────────────────────────────────────────────────
def _write_utf8(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")


def _parse_frontmatter(path: Path) -> dict | None:
    """读取 markdown 文件 YAML frontmatter。"""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return None
    m = re.match(r"^---\s*\n(.*?)\n---", text, re.DOTALL)
    if not m:
        return None
    try:
        import yaml
        return yaml.safe_load(m.group(1))
    except Exception:
        return None


# 模块级变量：由 dispatch 子命令在启动时设置，供 _append_telemetry 使用
harness_tag: str = "cli"


def _normalize_role(role_val: str) -> str:
    if role_val in ROLE_VALUES:
        return role_val
    return ROLE_LEGACY


def _estimate_cost(prompt_bytes: int, response_bytes: int, cost_input: float, cost_output: float) -> float:
    # pisr：cost 元数据不可得（pi --list-models 不含价格），恒返回 0。
    # 真实用量记录在 usage_* 字段（来自事件流）。保留本函数以维持 schema 兼容。
    return 0.0


def _resolve_prompt_size(prompt_path: str | None, inline_text: str | None = None) -> int:
    if prompt_path:
        try:
            return os.path.getsize(prompt_path)
        except OSError:
            pass
    if inline_text is not None:
        return len(inline_text.encode("utf-8"))
    return 0


def _parse_outcome_detail(outcome: str, exit_code: int | None = None, log_text: str = "") -> str:
    if outcome == "killed" and "timeout" in log_text.lower():
        return "killed:harness-timeout"
    if outcome == "killed":
        return "killed:unknown"
    if outcome == "stall" and "no progress" in log_text.lower():
        return "stall:no-progress"
    if outcome == "stall":
        return "stall:watchdog-timeout"
    if outcome == "error" and exit_code is not None:
        return f"error:exit_code_{exit_code}"
    if outcome == "error":
        return "error:unknown"
    if outcome == "success":
        return "success:completed"
    return f"{outcome}:unknown"


def _append_telemetry(
    model: str,
    role: str,
    channel: str,
    outcome: str,
    wall_min: float,
    artifact_bytes: int,
    note: str = "",
    task_id: str = "",
    label: str = "",
    plan_ref: str = "",
    scope: str = "",
    prompt_size_bytes: int = 0,
    response_size_bytes: int = 0,
    model_cost_input: float = 0.0,
    model_cost_output: float = 0.0,
    cost_estimate: float | None = None,
    blocking_chain: list[str] | None = None,
    outcome_detail: str = "",
    failure_retry_index: int = 0,
    timeout_policy_requested: str = "",
    timeout_policy_resolved: str = "",
    forbid_paths: int = 0,
    read_audit: str = "",
    usage_input: int = 0,
    usage_output: int = 0,
    usage_total_tokens: int = 0,
    usage_cost: float = 0.0,
    tool_calls: int = 0,
    tool_violations: int = 0,
    tool_audit: str = "",
) -> None:
    now_ts = datetime.datetime.now().astimezone().isoformat()
    row = {
        "ts": now_ts,
        "model": model,
        "role": role,
        "harness": harness_tag,
        "channel": channel,
        "outcome": outcome,
        "wall_min": wall_min,
        "artifact_bytes": artifact_bytes,
        "task_id": task_id or f"dispatch_{int(time.time())}",
        "plan_ref": plan_ref or "",
        "scope": scope or "",
        "prompt_size_bytes": prompt_size_bytes,
        "response_size_bytes": response_size_bytes,
        "model_cost_input": model_cost_input,
        "model_cost_output": model_cost_output,
        "cost_estimate": cost_estimate if cost_estimate is not None else 0.0,
        "blocking_chain": blocking_chain or [],
        "outcome_detail": outcome_detail or _parse_outcome_detail(outcome),
        "failure_retry_index": failure_retry_index,
        "usage_input": usage_input,
        "usage_output": usage_output,
        "usage_total_tokens": usage_total_tokens,
        "usage_cost": usage_cost,
        "tool_calls": tool_calls,
        "tool_violations": tool_violations,
    }
    if label:
        row["label"] = label
    if note:
        row["note"] = note
    if timeout_policy_requested:
        row["timeout_policy_requested"] = timeout_policy_requested
    if timeout_policy_resolved:
        row["timeout_policy_resolved"] = timeout_policy_resolved
    if forbid_paths:
        row["forbid_paths"] = forbid_paths
    if read_audit:
        row["read_audit"] = read_audit
    if tool_audit:
        row["tool_audit"] = tool_audit
    DISPATCH_LOG.parent.mkdir(parents=True, exist_ok=True)
    with DISPATCH_LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


# ─── pi 命令构造与事件流解析 ─────────────────────────────────────────

def _split_model(model: str) -> tuple[str, str]:
    """provider/model 二段拆分（白名单校验已保证恰好一个 /）。"""
    provider, _, model_id = model.partition("/")
    return provider, model_id


def _build_pi_argv(model: str, *, tools: list[str] | None = None,
                   thinking: str = "", prompt_at: str | None = None,
                   inline_prompt: str | None = None) -> list[str]:
    """构造 pi 非交互调用 argv。

    基线：--mode json（事件流遥测+审计） --no-session（不落会话） -nc（禁 context files）
    -na（忽略项目本地资源）。prompt 经 @file 注入（优先，无长度/转义问题）；
    inline_prompt 仅用于短探测（preflight）。
    """
    provider, model_id = _split_model(model)
    argv = [PI_BIN, "--mode", "json", "--no-session", "-nc", "-na",
            "--provider", provider, "--model", model_id]
    if thinking:
        argv += ["--thinking", thinking]
    if tools:
        argv += ["--tools", ",".join(tools)]
    if prompt_at is not None:
        argv += ["@" + str(prompt_at)]
    elif inline_prompt is not None:
        argv += [inline_prompt]
    return argv


def _parse_event_stream(path: Path) -> dict:
    """解析 pi --mode json 事件流，提取验收与遥测所需字段。

    返回 {"final_text", "usage", "tool_names", "stop_reason", "session_id"}。
    无法解析的行跳过（容忍流中断）；完全没有事件返回空结构（调用方按失败处理）。
    """
    result: dict = {"final_text": "", "usage": {}, "tool_names": [],
                    "stop_reason": "", "session_id": ""}
    tool_names: list[str] = []
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return result
    for line in text.split("\n"):
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        etype = ev.get("type", "")
        if etype == "session":
            result["session_id"] = ev.get("id", "")
        elif etype == "message_end":
            msg = ev.get("message") or {}
            if msg.get("role") == "assistant":
                parts = [b.get("text", "") for b in (msg.get("content") or [])
                         if isinstance(b, dict) and b.get("type") == "text"]
                result["final_text"] = "\n".join(p for p in parts if p)
                usage = msg.get("usage")
                if isinstance(usage, dict):
                    result["usage"] = usage
                result["stop_reason"] = msg.get("stopReason", "")
        elif etype == "tool_execution_start":
            name = ev.get("toolName", "")
            if name:
                tool_names.append(name)
    result["tool_names"] = tool_names
    return result


def _usage_fields(usage: dict) -> tuple[int, int, int, float]:
    """从事件 usage 对象提取 (input, output, total_tokens, cost_total)。"""
    if not isinstance(usage, dict):
        return (0, 0, 0, 0.0)
    cost = usage.get("cost") or {}
    return (
        int(usage.get("input", 0) or 0),
        int(usage.get("output", 0) or 0),
        int(usage.get("totalTokens", 0) or 0),
        float(cost.get("total", 0.0) or 0.0) if isinstance(cost, dict) else 0.0,
    )


def _audit_tool_calls(seen: list[str], allowed: list[str] | None) -> tuple[str, list[str]]:
    """工具越权审计：seen ⊄ allowed 即违规。

    返回 (状态, 违规名列表)；状态 ∈ clean / violated / unenforced。
    allowed 为 None 表示本次派发未设 --tools（审计记录 unenforced，不作失败判据）。
    """
    if allowed is None:
        return ("unenforced", [])
    allowed_set = {t.strip() for t in allowed if t.strip()}
    violations = sorted({t for t in seen if t not in allowed_set})
    return ("violated", violations) if violations else ("clean", [])


# ─── 派发账本 + 路径碰撞检测 ─────────────────────────────────────────

def _snapshot_dir(path: Path) -> dict[str, tuple[int, int]]:
    """快照目录顶层文件的 (size, mtime_ns)，供事后比对非预期变更。"""
    snap: dict[str, tuple[int, int]] = {}
    try:
        for p in path.iterdir():
            if p.is_file():
                st = p.stat()
                snap[p.name] = (st.st_size, st.st_mtime_ns)
    except (FileNotFoundError, NotADirectoryError, PermissionError):
        pass
    return snap


def _detect_name_mismatch(output: Path, snapshot_before: dict, label: str) -> list[str]:
    """产物命名与 --output-pattern 不符的候选检测（watcher 失败语义分层）。

    当期望产物 output 缺失时，在输出目录的快照差异中找「疑似产物」：
    新增文件（不在 snapshot_before）且（与期望产物同后缀 或 文件名含 label）。
    把「产物未落盘」与「产物落盘为其他文件名」区分为两类失败——后者
    产物其实有效，不应按 0 产物重派。
    """
    candidates: list[str] = []
    try:
        for p in output.parent.iterdir():
            if not p.is_file():
                continue
            if p.name == output.name or p.name == LEDGER_NAME:
                continue
            if p.name in snapshot_before:
                continue
            if p.suffix == output.suffix or label.lower() in p.name.lower():
                candidates.append(p.name)
    except (FileNotFoundError, NotADirectoryError, PermissionError):
        pass
    return sorted(candidates)


LEDGER_NAME = "pisr-dispatch-ledger.jsonl"


def _converge_ledger_path(output_dir: Path, explicit: str | None) -> Path | None:
    """定位派发账本。仅支持显式 --ledger-dir；不执行自动路径探测。"""
    if explicit:
        d = Path(explicit)
        d.mkdir(parents=True, exist_ok=True)
        return d / LEDGER_NAME
    return None


def _append_dispatch_ledger(ledger: Path | None, row: dict) -> None:
    """向派发账本追加一行（append-only，失败不阻断派发）。"""
    if ledger is None:
        return
    sanitized = _sanitize_ledger_row(row)
    payload = {"ts": datetime.datetime.now().astimezone().isoformat(), **sanitized}
    try:
        ledger.parent.mkdir(parents=True, exist_ok=True)
        with ledger.open("a", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False) + "\n")
    except Exception as e:
        print(f"⚠️ 派发账本写入失败 ({ledger}): {e}", file=sys.stderr)


def _collision_report(
    output_dir: Path,
    before: dict[str, tuple[int, int]],
    expected: set[str],
    ledger: Path | None,
) -> bool:
    """比对派发前后快照。返回 True 表示**既有文件被非预期覆盖**（真实损失）。"""
    after = _snapshot_dir(output_dir)
    ignore = expected | {LEDGER_NAME}
    overwritten = sorted(
        n for n, meta in after.items()
        if n in before and before[n] != meta and n not in ignore
    )
    unexpected_new = sorted(n for n in after if n not in before and n not in ignore)

    if overwritten:
        print(f"[pisr] ❌ {len(overwritten)} 个既有文件被非预期覆盖：", file=sys.stderr)
        for n in overwritten:
            print(f"        {n}  {before[n][0]}B → {after[n][0]}B", file=sys.stderr)
        print("        子代理写到了 --output-pattern 之外的路径。"
              "首查 prompt 的输出路径是否含未解析占位符。", file=sys.stderr)
    if unexpected_new:
        print(f"[pisr] ⚠️ {len(unexpected_new)} 个非预期新增文件："
              f"{', '.join(unexpected_new[:5])}", file=sys.stderr)

    if overwritten or unexpected_new:
        _append_dispatch_ledger(ledger, {
            "event": "path_anomaly",
            "overwritten": overwritten,
            "unexpected_new": unexpected_new,
        })
        _append_telemetry("-", "pisr-dispatch", "detached",
                          "path_collision" if overwritten else "unexpected_write",
                          0, 0, f"overwritten={overwritten} new={unexpected_new}",
                          outcome_detail=f"{'overwritten' if overwritten else 'unexpected_write'}:path_anomaly")
    return bool(overwritten)


# ─── monitor 工具 ────────────────────────────────────────────────────

def _dir_stall_check(path: Path, stall_minutes: int) -> tuple[bool, float]:
    now = time.time()
    newest = 0.0
    try:
        if not path.is_dir():
            return (True, -1.0)
        for p in path.rglob("*"):
            if p.is_file():
                mtime = p.stat().st_mtime_ns / 1e9
                if mtime > newest:
                    newest = mtime
    except (FileNotFoundError, PermissionError):
        return (True, -1.0)
    if newest == 0.0:
        return (True, -1.0)
    elapsed = (now - newest) / 60
    return (elapsed > stall_minutes, elapsed)


def _is_process_running(process_name: str) -> bool:
    if sys.platform == "win32":
        try:
            proc = subprocess.run(
                ["tasklist", "/FI", f"IMAGENAME eq {process_name}"],
                capture_output=True, text=True, timeout=15,
            )
            return len(proc.stdout.strip().split("\n")) > 1
        except (subprocess.TimeoutExpired, FileNotFoundError):
            return False
    else:
        try:
            proc = subprocess.run(
                ["pgrep", process_name],
                capture_output=True, timeout=15,
            )
            return proc.returncode == 0
        except (subprocess.TimeoutExpired, FileNotFoundError):
            return False


# ─── 评审锚定污染对治：--forbid-paths ────────────────────────────────

def _build_forbid_block(forbid_paths: list[str]) -> str:
    """生成禁止读取块（追加到 prompt 副本末尾，不改动原 prompt 文件）。"""
    norm = [fp.replace("\\", "/").rstrip("/") for fp in forbid_paths]
    listed = "\n".join(f"  - {p}" for p in norm)
    return (
        "【边界与禁区：禁止读取】\n"
        "- 以下路径及其子路径下的任何内容一律禁止读取（read/grep/find/ls 等一切方式均不允许）：\n"
        f"{listed}\n"
        "- 若意外接触到上述路径的内容，不得将其结论、措辞或结构纳入本报告。\n"
        "\n"
        "【执行证据】报告的执行证据段必须包含结构化顶层 YAML 列表 `reads:`，逐项列出本次实际读取的全部文件路径，格式示例：\n"
        "reads:\n"
        "  - C:/path/to/file-a.md\n"
        "  - C:/path/to/file-b.py\n"
    )


def _normalize_audit_path(p: str) -> str:
    """审计用路径归一化：去引号/反引号、正斜杠、去尾斜杠、大小写不敏感。"""
    return p.strip().strip("`").strip('"').strip("'").replace("\\", "/").rstrip("/").casefold()


def _parse_reads_list(text: str) -> list[str] | None:
    """宽松解析顶层 `reads:` 列表：找到行首 reads: 行后收集后续 `- ` 列表项。

    容忍缩进与 Windows 路径（条目中的反斜杠/盘符冒号不影响解析）。
    找不到 reads: 行返回 None（审计判 unavailable）。
    """
    reads: list[str] | None = None
    for line in text.split("\n"):
        if reads is None:
            m = re.match(r"^\s*reads\s*:\s*(.*)$", line)
            if m:
                rest = m.group(1).strip()
                if rest.startswith("[") and rest.endswith("]"):
                    inner = rest[1:-1].strip()
                    reads = [x.strip() for x in inner.split(",") if x.strip()] if inner else []
                else:
                    reads = []
            continue
        if line.strip() == "":
            continue  # 容忍列表项之间的空行
        m = re.match(r"^\s*[-*]\s+(.+?)\s*$", line)
        if m:
            reads.append(m.group(1))
        else:
            break
    return reads


def _audit_output_reads(output: Path, forbid_paths: list[str]) -> tuple[str, str]:
    """读路径审计：产物 reads 列表逐条与禁止路径对照（子路径算命中）。

    返回 (状态, 违规路径)；状态 ∈ clean / violated / unavailable。
    审计是报告机制，不影响退出码——裁决归 orchestrator。
    """
    try:
        text = output.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return ("unavailable", "")
    reads = _parse_reads_list(text)
    if reads is None:
        return ("unavailable", "")
    forbids = [_normalize_audit_path(fp) for fp in forbid_paths]
    for entry in reads:
        norm = _normalize_audit_path(entry)
        if not norm:
            continue
        for fp in forbids:
            if norm == fp or norm.startswith(fp + "/"):
                return ("violated", entry.strip().strip("`"))
    return ("clean", "")


# ─── worker 启动（Popen 直启 pi）─────────────────────────────────────

def _spawn_worker(w: dict) -> dict:
    """启动单个 worker：生成 prompt 副本（注入占位符/禁区块）→ Popen 直启 pi。

    在 worker 字典上补充 work_dir / proc / prompt_copy 字段并返回。
    stdout 重定向到 work-dir/events.jsonl（事件流遥测+审计），
    stderr 重定向到 work-dir/stderr.log（管道零依赖，无死锁面）。
    """
    wd: Path = w["work_dir"]
    wd.mkdir(parents=True, exist_ok=True)

    # 复制 prompt（注入输出路径占位符；与 ocsr 语义一致）
    prompt_content = Path(w["prompt_file"]).read_text(encoding="utf-8")
    prompt_content = prompt_content.replace("{{OUTPUT_PATH}}", str(w["output"]))
    prompt_content = prompt_content.replace("{{OUTPUT_NAME}}", Path(w["output"]).name)
    prompt_content = prompt_content.replace("{{OUTPUT_DIR}}", str(w["output"].parent))
    forbid_paths = w.get("forbid_paths") or []
    if forbid_paths:
        prompt_content = prompt_content.rstrip("\n") + "\n\n" + _build_forbid_block(forbid_paths)
    prompt_copy = wd / "prompt.txt"
    _write_utf8(prompt_copy, prompt_content)
    if prompt_copy.stat().st_size > PROMPT_ARG_MAX_BYTES:
        raise ValueError(
            f"prompt 副本超过 {PROMPT_ARG_MAX_BYTES}B 上限: {prompt_copy} "
            f"（{prompt_copy.stat().st_size}B）。拆分任务或精简 prompt。"
        )
    w["prompt_copy"] = prompt_copy

    argv = _build_pi_argv(w["model"], tools=w.get("tools"),
                          thinking=w.get("thinking", ""),
                          prompt_at=prompt_copy.resolve().as_posix())
    events_path = wd / "events.jsonl"
    stderr_path = wd / "stderr.log"
    out_fh = events_path.open("wb")
    err_fh = stderr_path.open("wb")
    creationflags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    proc = subprocess.Popen(
        argv, stdout=out_fh, stderr=err_fh,
        stdin=subprocess.DEVNULL,
        cwd=str(wd), creationflags=creationflags,
    )
    out_fh.close()
    err_fh.close()
    w["proc"] = proc
    w["events_path"] = events_path
    w["stderr_path"] = stderr_path
    return w


def _kill_worker(label: str, w: dict) -> bool:
    """按 PID 终止 worker 的 pi 进程树。返回 True 仅当终止操作报告成功。

    只杀目标 PID（taskkill /T 连带子进程），不按镜像名批量杀——
    那会连带杀死正在正常工作的兄弟 worker。
    """
    proc = w.get("proc")
    pid = proc.pid if proc is not None else None
    if pid is None:
        print(f"[pisr] ⚠️ {label} 无可用 PID，无法终止", file=sys.stderr)
        return False
    if sys.platform == "win32":
        try:
            r = subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(pid)],
                capture_output=True, text=True, timeout=15,
            )
        except Exception as e:
            print(f"[pisr] ⚠️ {label} taskkill PID={pid} 异常: {e}", file=sys.stderr)
            return False
        if r.returncode != 0:
            detail = ((r.stdout or "") + (r.stderr or "")).strip()[:200]
            print(f"[pisr] ⚠️ {label} taskkill PID={pid} 失败 (rc={r.returncode}): {detail}",
                  file=sys.stderr)
            return False
    else:
        try:
            proc.kill()
        except Exception as e:
            print(f"[pisr] ⚠️ {label} kill PID={pid} 异常: {e}", file=sys.stderr)
            return False
    try:
        proc.wait(timeout=10)
    except Exception:
        pass
    return True


def _check_output_landed(
    output: Path,
    snapshot_before: dict[str, tuple[int, int]] | None,
) -> tuple[bool, dict]:
    """检查产物是否有效落盘。"""
    if not output.is_file():
        return (False, {"pre_existed": False, "change": False})
    st = output.stat()
    before_meta = snapshot_before.get(output.name) if snapshot_before else None
    if before_meta is None:
        landed = st.st_size > 0
        return (landed, {"pre_existed": False, "change": landed})
    else:
        changed = (st.st_size, st.st_mtime_ns) != before_meta
        meta = {
            "pre_existed": True,
            "change": changed,
            "size_before": before_meta[0],
            "mtime_ns_before": before_meta[1],
        }
        landed = st.st_size > 0 and changed
        return (landed, meta)


# ─── 派发内核 ─────────────────────────────────────────────────────────

def _dispatch_batch(
    workers: list[dict],
    *,
    output_dir: Path,
    work_dir: Path | None = None,
    stagger: int = DEFAULT_STAGGER,
    timeout_min: int = DEFAULT_TIMEOUT,
    timeout_policy: str = TIMEOUT_POLICY_AUTO,
    watch: bool = False,
    progress: bool = False,
    ledger_dir: str | None = None,
    forbid_paths: list[str] | None = None,
    role: str = "pisr-dispatch",
    task_id: str = "",
    plan_ref: str = "",
    scope: str = "",
    blocking_chain: list[str] | None = None,
) -> int:
    """派发内核：**已解析完毕**的 worker 批次 → 退出码。

    这是 `dispatch` 子命令与 `run` 的 dispatch 步骤**共用**的唯一执行路径。
    不做 CLI 参数解析、不读 argparse Namespace，因此 `run` 可在进程内调用。

    worker 字典契约（权威定义 —— 两个调用方必须按此构造）::

        {
          "prompt_file": str,          # 必填：已存在的 prompt 文件路径
          "model": str,                # 必填：PISR 白名单内的 provider/model ID
          "label": str,                # 必填：worker 标识（用于 work-dir 名与遥测）
          "output": Path,              # 必填：期望产物路径
          "tools": list[str] | None,   # 可选：--tools 白名单（None=全工具）
          "thinking": str,             # 可选：--thinking 档位
          "capture_reply": bool,       # 可选：产物=最终回复的机械落盘（只读 reviewer）
          "prompt_size_bytes": int,    # 可选：遥测用，缺省 0
        }

    入口重校验（S1）：`--validate` 是离线干跑，与真正执行之间存在窗口期。
    本函数在产生任何副作用前**重新校验**模型白名单、prompt 存在性与输出目录，
    不以「上游已经校验过」为由跳过。

    返回值遵循 `refs/dispatch-patterns.md` §退出码契约：0/1/2/3，优先级 3 > 1 > 2 > 0。
    """
    _check_model_calls_disabled()
    forbid_paths = forbid_paths or []
    blocking_chain = blocking_chain or []

    if not workers:
        print("❌ _dispatch_batch: worker 列表为空", file=sys.stderr)
        return 1
    for i, w in enumerate(workers):
        for key in ("prompt_file", "model", "label", "output"):
            if not w.get(key):
                print(f"❌ _dispatch_batch: worker[{i}] 缺少必填字段 `{key}`", file=sys.stderr)
                return 1
        try:
            _validate_model_allowed(w["model"])
        except ValueError as e:
            print(f"❌ {e}", file=sys.stderr)
            return 1
        if not Path(w["prompt_file"]).is_file():
            print(f"❌ prompt 文件不存在: {w['prompt_file']}", file=sys.stderr)
            return 1
        w.setdefault("prompt_size_bytes", _resolve_prompt_size(w["prompt_file"]))
        w.setdefault("tools", None)
        w.setdefault("thinking", "")
        w.setdefault("capture_reply", False)
        w["forbid_paths"] = forbid_paths
    if not output_dir.is_dir():
        print(f"❌ 输出目录不存在: {output_dir}", file=sys.stderr)
        return 1

    parsed = workers
    ledger = _converge_ledger_path(output_dir, ledger_dir)
    snapshot_before = _snapshot_dir(output_dir)
    expected_names = {Path(p["output"]).name for p in parsed}
    if progress and ledger is not None:
        print(f"[pisr] 派发账本: {ledger}")

    # 创建工作目录（加 uuid4 后缀防同秒碰撞）
    import uuid
    base = Path(work_dir) if work_dir else Path(os.environ.get("TEMP", "/tmp"))
    batch_id = f"{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"
    batch_dir = base / f"pisr_dispatch_{batch_id}"
    batch_dir.mkdir(parents=True, exist_ok=True)

    for i, p in enumerate(parsed):
        p["work_dir"] = batch_dir / str(p["label"]).replace("/", "-").replace(" ", "_")
        if progress:
            tools_desc = ",".join(p["tools"]) if p.get("tools") else "all"
            print(f"[pisr] [{i+1}/{len(parsed)}] 已就绪: {p['label']} ({p['model']}, tools={tools_desc}) → {p['output']}")

    # 启动（带 stagger；pi 无共享 DB，错峰仅用于 API 侧限流礼貌）
    print(f"[pisr] 开始启动 ({len(parsed)} workers, stagger={stagger}s, timeout={timeout_min}min)")
    start_times: list[float] = []
    launch_errors = 0
    for i, p in enumerate(parsed):
        if i > 0 and stagger > 0:
            if progress:
                print(f"[pisr] 等待 {stagger}s（错峰启动）...")
            time.sleep(stagger)
        try:
            _spawn_worker(p)
        except Exception as e:
            launch_errors += 1
            _append_telemetry(p["model"], _normalize_role(role), "detached", "error", 0, 0,
                              f"spawn failed: {str(e)[:200]}",
                              prompt_size_bytes=p.get("prompt_size_bytes", 0),
                              task_id=task_id, label=p["label"],
                              plan_ref=plan_ref, scope=scope,
                              blocking_chain=blocking_chain,
                              outcome_detail="error:spawn_failed")
            _append_dispatch_ledger(ledger, {
                "event": "failed", "reason": "spawn_failed", "label": p["label"],
                "model": p["model"], "detail": str(e)[:200],
            })
            print(f"[pisr] ❌ {p['label']} 启动失败: {e}", file=sys.stderr)
            p["proc"] = None
            p.setdefault("events_path", p["work_dir"] / "events.jsonl")
            p.setdefault("stderr_path", p["work_dir"] / "stderr.log")
            p["spawn_failed"] = True
        start_times.append(time.time())
        _append_dispatch_ledger(ledger, {
            "event": "launched",
            "batch_id": batch_id,
            "label": p["label"],
            "model": p["model"],
            "harness": harness_tag,
            "prompt_file": str(p["prompt_file"]),
            "expected_output": str(p["output"]),
            "work_dir": str(p["work_dir"]),
            "tools": p.get("tools") or "all",
            "pid": (p["proc"].pid if p.get("proc") else None),
        })

    print("[pisr] 全部 worker 已启动，等待产物落盘...")

    if watch:
        requested_policy = timeout_policy
        resolved_policy = _resolve_timeout_policy(timeout_policy, role)
        rc = _watch_loop(parsed, start_times, timeout_min, progress, ledger,
                         task_id=task_id, role=role, plan_ref=plan_ref,
                         scope=scope, blocking_chain=blocking_chain,
                         snapshot_before=snapshot_before,
                         timeout_policy=resolved_policy,
                         timeout_policy_requested=requested_policy,
                         forbid_paths=forbid_paths)
        if _collision_report(output_dir, snapshot_before, expected_names, ledger):
            return EXIT_PATH_COLLISION
        return rc

    if progress:
        print("[pisr] 未启用 --watch：跳过产物回收与路径碰撞检测（仅记 launched）")
    return 0


def cmd_dispatch(args) -> int:
    """`dispatch` 子命令的薄 CLI 包装：解析参数 → 构造 worker 列表 → 调 `_dispatch_batch`。"""
    _check_model_calls_disabled()
    global harness_tag
    harness_tag = args.harness or "cli"
    workers = args.worker
    output_dir = Path(args.output_dir)
    stagger = args.stagger if args.stagger is not None else DEFAULT_STAGGER
    timeout_min = args.timeout if args.timeout is not None else DEFAULT_TIMEOUT
    timeout_policy = args.timeout_policy or TIMEOUT_POLICY_AUTO
    watch = args.watch
    progress = args.progress
    output_pattern = args.output_pattern or "{label}.md"
    date_str = datetime.date.today().strftime("%Y-%m-%d")

    if not workers:
        print("❌ 至少需要一个 --worker", file=sys.stderr)
        return 1

    # 解析 --meta 元数据
    meta: dict[str, str] = {}
    if hasattr(args, "meta") and args.meta:
        for kv in args.meta:
            if "=" not in kv:
                print(f"⚠️ --meta 格式错误（忽略）: {kv}", file=sys.stderr)
                continue
            k, v = kv.split("=", 1)
            meta[k.strip()] = v.strip()
    task_id = meta.get("task_id", "")
    role = meta.get("role", "pisr-dispatch")
    plan_ref = meta.get("plan_ref", "")
    scope = meta.get("scope", "")
    bc_raw = meta.get("blocking_chain", "")
    blocking_chain = [x.strip() for x in bc_raw.split(",") if x.strip()] if bc_raw else []

    # 解析 --forbid-paths（评审锚定污染对治）
    forbid_paths = [fp.strip() for fp in (getattr(args, "forbid_paths", None) or [])
                    if fp and fp.strip()]

    # 解析 --tools / --thinking（工具面声明：本次派发的硬白名单与思考档位）
    tools = None
    if getattr(args, "tools", None):
        tools = [t.strip() for t in args.tools.split(",") if t.strip()]
    thinking = getattr(args, "thinking", "") or ""
    capture_reply = bool(getattr(args, "capture_reply", False))

    # 解析 workers（分隔符 | 避免与 Windows 盘符 C: 及模型 ID 中的 / 冲突）
    # 格式：PROMPT_PATH|MODEL|LABEL
    parsed = []
    for w in workers:
        parts = w.split("|", 2)
        if len(parts) != 3:
            print(f"❌ worker 格式错误: `{w}`（应为 PROMPT_PATH|MODEL|LABEL，| 分隔）", file=sys.stderr)
            return 1
        prompt_file, model, label = parts
        try:
            _validate_model_allowed(model)
        except ValueError as e:
            print(f"❌ {e}", file=sys.stderr)
            return 1
        if not Path(prompt_file).is_file():
            print(f"❌ prompt 文件不存在: {prompt_file}", file=sys.stderr)
            return 1
        output_name = output_pattern.format(date=date_str, label=label, model=model.replace("/", "-"))
        prompt_size = _resolve_prompt_size(prompt_file)
        parsed.append({"prompt_file": prompt_file, "model": model, "label": label,
                       "output": output_dir / output_name, "prompt_size_bytes": prompt_size,
                       "tools": tools, "thinking": thinking, "capture_reply": capture_reply})
    # 检查 output 路径冲突
    outputs = [p["output"] for p in parsed]
    if len(outputs) != len(set(str(o) for o in outputs)):
        print("❌ output 路径冲突（output-pattern 产生了重复文件名）", file=sys.stderr)
        return 1

    return _dispatch_batch(
        parsed,
        output_dir=output_dir,
        work_dir=Path(args.work_dir) if args.work_dir else None,
        stagger=stagger,
        timeout_min=timeout_min,
        timeout_policy=timeout_policy,
        watch=watch,
        progress=progress,
        ledger_dir=getattr(args, "ledger_dir", None),
        forbid_paths=forbid_paths,
        role=role,
        task_id=task_id,
        plan_ref=plan_ref,
        scope=scope,
        blocking_chain=blocking_chain,
    )


def _worker_exit_code(w: dict) -> int | None:
    """读取 worker 的 pi 进程退出码；未退出返回 None。"""
    proc = w.get("proc")
    if proc is None:
        # 启动即失败（spawn_failed）在 watch 循环里单独处理
        return None
    try:
        return proc.poll()
    except Exception:
        return None


def _watch_loop(
    parsed: list[dict],
    start_times: list[float],
    timeout_min: int,
    progress: bool,
    ledger: Path | None = None,
    task_id: str = "",
    role: str = "pisr-dispatch",
    plan_ref: str = "",
    scope: str = "",
    blocking_chain: list[str] | None = None,
    snapshot_before: dict[str, tuple[int, int]] | None = None,
    timeout_policy: str = TIMEOUT_POLICY_LEAF_KILL,
    timeout_policy_requested: str = "",
    forbid_paths: list[str] | None = None,
) -> int:
    """双监视：产物落盘 + 进程存活（Popen poll）。

    结案语义：每个 worker 最终落入且仅落入三个集合之一——
    `landed`（产物有效落盘）/ `failed`（确定性失败，已结案）/ `timed_out`（看门狗到期）。
    循环在三者之和覆盖全部 worker 时结束。

    确定性失败来源（exit=2 判据）：
      - spawn 失败（Popen 抛异常）
      - pi 非零退出
      - pi 退出码为 0 但期望产物未落盘
      - 工具越权审计命中（tool_calls ⊄ --tools 白名单）

    退出码：0/1/2；路径碰撞(3) 由 `cmd_dispatch` 在收口时覆盖。
    """
    landed: set[int] = set()
    failed: set[int] = set()
    timed_out: set[int] = set()
    warned_stall: set[int] = set()
    # 每 worker 独立 deadline；Popen 句柄随本进程存活。
    deadlines: list[float] = [st + timeout_min * 60 for st in start_times]

    def _settled() -> int:
        return len(landed) + len(failed) + len(timed_out)

    while True:
        now = time.time()

        for i, p in enumerate(parsed):
            if i in landed or i in failed or i in timed_out:
                continue

            output = p["output"]
            events_path = p.get("events_path") or (p.get("work_dir") / "events.jsonl") if p.get("work_dir") else None

            # spawn 即失败：确定性失败
            if p.get("spawn_failed"):
                failed.add(i)
                continue

            # 检查产物（新文件要求存在+size>0；预存文件要求内容变化）
            is_landed, land_meta = _check_output_landed(output, snapshot_before)
            exit_code = _worker_exit_code(p)
            if is_landed and exit_code is None:
                # 产物已落盘但进程仍在跑：等它自然退出再结案，
                # 以便遥测拿到完整 usage 与工具审计。
                pass
            elif is_landed and exit_code is not None:
                elapsed = (now - start_times[i]) / 60
                ev = _parse_event_stream(events_path) if events_path else {}
                u_in, u_out, u_tot, u_cost = _usage_fields(ev.get("usage", {}))
                tool_names = ev.get("tool_names", [])
                tool_audit, tool_violations = _audit_tool_calls(tool_names, p.get("tools"))
                verdict = ""
                fm = _parse_frontmatter(output)
                if fm and fm.get("verdict"):
                    verdict = f", verdict={fm['verdict']}"
                # 读路径审计（--forbid-paths 指定时）：报告机制，不改变退出码
                read_audit = ""
                if forbid_paths:
                    read_audit, violation = _audit_output_reads(output, forbid_paths)
                    if read_audit == "violated":
                        audit_detail = f"violated({violation})"
                    elif read_audit == "unavailable":
                        audit_detail = "unavailable(报告未含 reads 段)"
                    else:
                        audit_detail = "clean"
                    print(f"[pisr] 读路径审计: {p['label']} {audit_detail}")
                # 工具越权审计：命中即确定性失败（fail-closed）
                if tool_audit == "violated":
                    print(f"[pisr] ❌ {p['label']} 工具越权审计命中: {tool_violations}"
                          f"（白名单: {p.get('tools')}）——产物虽落盘仍判失败", file=sys.stderr)
                    _append_telemetry(p["model"], _normalize_role(role), "detached", "error",
                                      round(elapsed, 1), output.stat().st_size,
                                      f"tool violation: {tool_violations}",
                                      prompt_size_bytes=p.get("prompt_size_bytes", 0),
                                      response_size_bytes=output.stat().st_size,
                                      task_id=task_id, label=p.get("label", ""),
                                      plan_ref=plan_ref, scope=scope,
                                      blocking_chain=blocking_chain,
                                      outcome_detail=f"tool_violation:{','.join(tool_violations)}",
                                      usage_input=u_in, usage_output=u_out,
                                      usage_total_tokens=u_tot, usage_cost=u_cost,
                                      tool_calls=len(tool_names),
                                      tool_violations=len(tool_violations),
                                      tool_audit=tool_audit)
                    _append_dispatch_ledger(ledger, {
                        "event": "failed", "reason": "tool_violation", "label": p["label"],
                        "model": p["model"], "wall_min": round(elapsed, 1),
                        "violations": tool_violations,
                    })
                    failed.add(i)
                    continue
                _append_telemetry(p["model"], _normalize_role(role or ""), "detached", "success",
                                  round(elapsed, 1), output.stat().st_size,
                                  prompt_size_bytes=p.get("prompt_size_bytes", 0),
                                  response_size_bytes=output.stat().st_size,
                                  task_id=task_id or "", label=p.get("label", ""),
                                  plan_ref=plan_ref or "",
                                  scope=scope or "", blocking_chain=blocking_chain or [],
                                  timeout_policy_requested=timeout_policy_requested,
                                  timeout_policy_resolved=timeout_policy,
                                  forbid_paths=len(forbid_paths or []),
                                  read_audit=read_audit,
                                  usage_input=u_in, usage_output=u_out,
                                  usage_total_tokens=u_tot, usage_cost=u_cost,
                                  tool_calls=len(tool_names), tool_violations=0,
                                  tool_audit=tool_audit)
                landed_row: dict[str, object] = {
                    "event": "landed", "label": p["label"], "model": p["model"],
                    "output": str(output), "bytes": output.stat().st_size,
                    "wall_min": round(elapsed, 1),
                    "pre_existed": land_meta.get("pre_existed", False),
                    "change": land_meta.get("change", True),
                    "usage_input": u_in, "usage_output": u_out,
                    "usage_total_tokens": u_tot, "tool_calls": len(tool_names),
                }
                if land_meta.get("pre_existed"):
                    landed_row["size_before"] = land_meta["size_before"]
                    landed_row["mtime_ns_before"] = land_meta["mtime_ns_before"]
                if fm and fm.get("verdict"):
                    landed_row["verdict"] = fm["verdict"]
                _append_dispatch_ledger(ledger, landed_row)
                print(f"[pisr] ✅ {p['label']} 落盘 ({output.stat().st_size}B, {elapsed:.1f}min, "
                      f"tokens={u_tot}, tools={len(tool_names)}{verdict})")
                landed.add(i)
                continue

            # 进程已退出而产物未落盘 —— 无论退出码是否为 0 都是确定性失败，
            # 除非启用了 --capture-reply 且最终回复非空（只读 reviewer 的产物
            # 由驱动器从事件流最终回复**机械落盘**：产物即回复的忠实捕获，
            # 不存在"自述完成"信任面）。
            if exit_code is not None:
                elapsed = (now - start_times[i]) / 60
                stderr_text = ""
                stderr_path = p.get("stderr_path")
                if stderr_path and stderr_path.is_file():
                    stderr_text = stderr_path.read_text(encoding="utf-8", errors="replace")[:500]
                ev = _parse_event_stream(events_path) if events_path else {}
                u_in, u_out, u_tot, u_cost = _usage_fields(ev.get("usage", {}))
                tool_names = ev.get("tool_names", [])
                tool_audit, tool_violations = _audit_tool_calls(tool_names, p.get("tools"))
                final_text = (ev.get("final_text") or "").strip()

                if tool_audit == "violated":
                    od = f"tool_violation:{','.join(tool_violations)}"
                    reason = "tool_violation"
                    human = (f"[pisr] ❌ {p['label']} 工具越权审计命中: {tool_violations}"
                             f"（白名单: {p.get('tools')}）")
                    _append_telemetry(p["model"], _normalize_role(role), "detached", "error",
                                      round(elapsed, 1), 0,
                                      f"tool violation: {tool_violations}",
                                      prompt_size_bytes=p.get("prompt_size_bytes", 0),
                                      task_id=task_id, label=p.get("label", ""),
                                      plan_ref=plan_ref, scope=scope,
                                      blocking_chain=blocking_chain,
                                      outcome_detail=od,
                                      usage_input=u_in, usage_output=u_out,
                                      usage_total_tokens=u_tot, usage_cost=u_cost,
                                      tool_calls=len(tool_names),
                                      tool_violations=len(tool_violations),
                                      tool_audit=tool_audit)
                    _append_dispatch_ledger(ledger, {
                        "event": "failed", "reason": reason, "label": p["label"],
                        "model": p["model"], "wall_min": round(elapsed, 1),
                        "violations": tool_violations, "exit_code": exit_code,
                    })
                    print(human, file=sys.stderr)
                    failed.add(i)
                    continue

                if p.get("capture_reply") and exit_code == 0 and final_text:
                    # 机械落盘：驱动器把最终回复原样写入期望产物路径。
                    # 只读 reviewer（--tools read,grep,find,ls）没有 write 工具，
                    # 其报告天然以回复形态存在——这不是"采信自述"，而是
                    # 事件流证据的确定性物化。
                    output.parent.mkdir(parents=True, exist_ok=True)
                    _write_utf8(output, final_text + "\n")
                    # 读路径审计（--forbid-paths 指定时）：与自写产物同一标准
                    read_audit = ""
                    if forbid_paths:
                        read_audit, violation = _audit_output_reads(output, forbid_paths)
                        if read_audit == "violated":
                            audit_detail = f"violated({violation})"
                        elif read_audit == "unavailable":
                            audit_detail = "unavailable(报告未含 reads 段)"
                        else:
                            audit_detail = "clean"
                        print(f"[pisr] 读路径审计: {p['label']} {audit_detail}")
                    _append_telemetry(p["model"], _normalize_role(role or ""), "detached", "success",
                                      round(elapsed, 1), output.stat().st_size,
                                      prompt_size_bytes=p.get("prompt_size_bytes", 0),
                                      response_size_bytes=output.stat().st_size,
                                      task_id=task_id or "", label=p.get("label", ""),
                                      plan_ref=plan_ref or "",
                                      scope=scope or "", blocking_chain=blocking_chain or [],
                                      timeout_policy_requested=timeout_policy_requested,
                                      timeout_policy_resolved=timeout_policy,
                                      usage_input=u_in, usage_output=u_out,
                                      usage_total_tokens=u_tot, usage_cost=u_cost,
                                      tool_calls=len(tool_names), tool_violations=len(tool_violations),
                                      tool_audit=tool_audit,
                                      read_audit=read_audit,
                                      note="artifact captured from final reply")
                    _append_dispatch_ledger(ledger, {
                        "event": "landed", "label": p["label"], "model": p["model"],
                        "output": str(output), "bytes": output.stat().st_size,
                        "wall_min": round(elapsed, 1), "captured_from_reply": True,
                        "pre_existed": False, "change": True,
                        "usage_input": u_in, "usage_output": u_out,
                        "usage_total_tokens": u_tot, "tool_calls": len(tool_names),
                    })
                    print(f"[pisr] ✅ {p['label']} 落盘（回复捕获，{output.stat().st_size}B, "
                          f"{elapsed:.1f}min, tokens={u_tot}, tools={len(tool_names)}）")
                    landed.add(i)
                    continue

                if exit_code == 0:
                    od = "error:exit_0_no_artifact"
                    reason = "pi_exit_0_no_artifact"
                    tail = (ev.get("final_text") or stderr_text or "").strip().replace("\n", " ")[:200]
                    human = (f"[pisr] ❌ {p['label']} pi 正常退出 (exit=0) 但期望产物未落盘"
                             f" → 优先怀疑写入路径错误或模型拒绝写入；末段输出: {tail}")
                    # 失败语义分层：产物可能落盘为其他文件名
                    mismatch = _detect_name_mismatch(output, snapshot_before or {}, p["label"])
                    if mismatch:
                        od = "error:exit_0_name_mismatch"
                        reason = "pi_exit_0_name_mismatch"
                        human = (f"[pisr] ⚠️ {p['label']} 期望产物未落盘，但检测到疑似产物"
                                 f"命名与 pattern 不符：{', '.join(mismatch[:5])}"
                                 f"（产物或已有效落盘——先核对文件名再判定，勿按 0 产物重派）")
                else:
                    od = _parse_outcome_detail("error", exit_code=exit_code, log_text=stderr_text)
                    reason = f"pi_exit_{exit_code}"
                    human = (f"[pisr] ❌ {p['label']} pi 退出 exit={exit_code}"
                             f"{'; stderr: ' + stderr_text[:200] if stderr_text else ''}")
                _append_telemetry(p["model"], _normalize_role(role), "detached", "error",
                                  round(elapsed, 1), 0,
                                  f"pi exit={exit_code}, final={ev.get('final_text', '')[:150]}",
                                  prompt_size_bytes=p.get("prompt_size_bytes", 0),
                                  task_id=task_id, label=p.get("label", ""),
                                  plan_ref=plan_ref,
                                  scope=scope, blocking_chain=blocking_chain,
                                  outcome_detail=od,
                                  usage_input=u_in, usage_output=u_out,
                                  usage_total_tokens=u_tot, usage_cost=u_cost,
                                  tool_calls=len(tool_names),
                                  tool_violations=len(tool_violations),
                                  tool_audit=tool_audit)
                _append_dispatch_ledger(ledger, {
                    "event": "failed", "reason": reason,
                    "label": p["label"], "model": p["model"], "wall_min": round(elapsed, 1),
                    "exit_code": exit_code,
                })
                print(human)
                failed.add(i)
                continue

            # 静默停滞检测（仅警告）：事件流 0 字节且进程存活超过阈值
            if events_path is not None:
                ev_size = events_path.stat().st_size if events_path.is_file() else 0
                stall_threshold = min(8, timeout_min / 2)
                stalled_min = (now - start_times[i]) / 60
                if ev_size == 0 and stalled_min >= stall_threshold and i not in warned_stall:
                    print(f"[pisr] ⚠️ {p['label']} 事件流 0 字节已 {stalled_min:.0f}min（疑似静默停滞）")
                    warned_stall.add(i)

        # 看门狗：逐 worker 用各自的 deadline 判定（非全局 deadline）。
        # 已结案的 worker 一律跳过——否则会对已结案的失败做二次 kill、二次遥测。
        for i, p in enumerate(parsed):
            if i in landed or i in failed or i in timed_out:
                continue
            if now <= deadlines[i]:
                continue
            elapsed = (now - start_times[i]) / 60
            events_path = p.get("events_path")
            log_size = events_path.stat().st_size if events_path and events_path.is_file() else 0
            if timeout_policy == TIMEOUT_POLICY_LEAF_KILL:
                killed_ok = _kill_worker(p["label"], p)
                if killed_ok:
                    outcome_detail_val = _parse_outcome_detail(
                        "stall", log_text=f"watchdog timeout {timeout_min}min")
                    note_text = f"watchdog timeout {timeout_min}min, killed"
                    progress_text = (f"[pisr] ⏰ {p['label']} 超时 ({timeout_min}min)"
                                     f"→已 kill，事件流 {log_size}B")
                else:
                    # `killed:failed` = kill 操作本身失败，目标进程可能仍在运行；
                    # 不得降级记为普通 stall——那会掩盖「看门狗已放弃止损、
                    # 而 worker 仍在消耗模型调用」这一事实。
                    outcome_detail_val = "killed:failed"
                    note_text = (f"watchdog timeout {timeout_min}min, "
                                 f"kill FAILED (target process may still be running)")
                    progress_text = (f"[pisr] ⏰ {p['label']} 超时 ({timeout_min}min)"
                                     f"→kill 失败，进程可能仍在运行，事件流 {log_size}B")
                event_result = "failed"
                fail_reason = "watchdog_timeout"
            else:
                outcome_detail_val = "reported:alive"
                note_text = f"watchdog timeout {timeout_min}min, reported/alive"
                progress_text = (f"[pisr] ⏰ {p['label']} 超时 ({timeout_min}min)"
                                 f"→报告/alive（进程保留），事件流 {log_size}B")
                event_result = "reported"
                fail_reason = "watchdog_reported"
            _append_telemetry(p["model"], _normalize_role(role), "detached", "stall",
                              round(elapsed, 1), log_size,
                              note_text,
                              prompt_size_bytes=p.get("prompt_size_bytes", 0),
                              task_id=task_id, label=p.get("label", ""),
                              plan_ref=plan_ref,
                              scope=scope, blocking_chain=blocking_chain,
                              outcome_detail=outcome_detail_val,
                              timeout_policy_requested=timeout_policy_requested,
                              timeout_policy_resolved=timeout_policy)
            _append_dispatch_ledger(ledger, {
                "event": event_result, "reason": fail_reason, "label": p["label"],
                "model": p["model"], "wall_min": round(elapsed, 1),
                "timeout_min": timeout_min, "log_bytes": log_size,
                "timeout_policy_requested": timeout_policy_requested,
                "timeout_policy_resolved": timeout_policy,
            })
            print(progress_text)
            timed_out.add(i)

        # 结案判定：三个集合之和覆盖全部 worker 即收口
        if _settled() == len(parsed):
            break

        time.sleep(CHECK_INTERVAL)

    # ── 收口与退出码 ────────────────────────────────────────────────
    # 混合结局优先级：看门狗超时(1) > 确定性失败(2) > 全部成功(0)。
    if timed_out:
        print(f"[pisr] ❌ 看门狗超时 ({timeout_min}min)：{len(timed_out)}/{len(parsed)} 个 worker 未落盘")
        return 1
    if failed:
        print(f"[pisr] ❌ {len(failed)}/{len(parsed)} 个 worker 确定性失败，未落盘")
        return EXIT_DETERMINISTIC_FAILURE
    print("[pisr] ✅ 全部 worker 完成")
    return 0


# ─── selftest ─────────────────────────────────────────────────────────

SELFTEST_OFFLINE_CHECKS = ("pi_version", "config", "argv_construction", "pattern_format")


def _selftest_offline() -> bool:
    """离线自检：pi --version、白名单加载、argv 构造、output-pattern 格式化。"""
    ok = True
    # 1. pi --version
    try:
        r = subprocess.run([PI_BIN, "--version"], capture_output=True, text=True, timeout=30)
        if r.returncode == 0:
            print(f"[selftest] ✅ pi --version: {(r.stdout or '').strip()[:60]}")
        else:
            print(f"[selftest] ❌ pi --version rc={r.returncode}: {r.stderr[:200]}", file=sys.stderr)
            ok = False
    except Exception as e:
        print(f"[selftest] ❌ pi 不可执行: {e}", file=sys.stderr)
        ok = False
    # 2. config 白名单
    try:
        models = ", ".join(sorted(ALLOWED_MODELS))
        print(f"[selftest] ✅ 白名单加载: {models}")
    except Exception as e:
        print(f"[selftest] ❌ 白名单异常: {e}", file=sys.stderr)
        ok = False
    # 3. argv 构造回归
    argv = _build_pi_argv("prov/model-x", tools=["read", "grep"], thinking="low",
                          prompt_at="C:/t/p.txt")
    expect_frags = ["--mode", "json", "--no-session", "-nc", "-na",
                    "--provider", "prov", "--model", "model-x",
                    "--thinking", "low", "--tools", "read,grep", "@C:/t/p.txt"]
    for frag in expect_frags:
        if frag not in argv:
            print(f"[selftest] ❌ argv 构造缺少 `{frag}`: {argv}", file=sys.stderr)
            ok = False
    argv2 = _build_pi_argv("prov/model-x", inline_prompt="hi")
    for frag in ["--mode", "json", "-nc", "-na", "--provider", "prov", "--model", "model-x", "hi"]:
        if frag not in argv2:
            print(f"[selftest] ❌ argv(inline) 缺少 `{frag}`: {argv2}", file=sys.stderr)
            ok = False
    if ok:
        print("[selftest] ✅ argv 构造回归通过")
    # 4. output-pattern 格式化
    name = "{label}.md".format(date="2026-01-01", label="L", model="a-b")
    if name == "L.md":
        print("[selftest] ✅ output-pattern 格式化通过")
    else:
        print(f"[selftest] ❌ output-pattern 异常: {name}", file=sys.stderr)
        ok = False
    return ok


def cmd_selftest(args) -> int:
    """自检。无 --model：纯离线（不消耗模型调用）。带 --model：在线冒烟（一次真实调用）。"""
    if not _selftest_offline():
        return 1
    if not args.model:
        print("[selftest] 离线自检通过（未发起任何模型调用；在线冒烟加 --model <provider/model>）")
        return 0

    _check_model_calls_disabled()
    model = args.model
    try:
        _validate_model_allowed(model)
    except ValueError as e:
        print(f"❌ {e}", file=sys.stderr)
        return 1
    work_dir = Path(args.work_dir or os.environ.get("TEMP", "/tmp")) / "pisr_selftest"
    work_dir.mkdir(parents=True, exist_ok=True)

    output_dir = Path(args.output_dir) if args.output_dir else work_dir / "output"
    output_dir.mkdir(parents=True, exist_ok=True)
    label = "selftest"
    prompt_path = work_dir / "prompt.txt"
    output_path = output_dir / "pisr-selftest.md"
    output_path.unlink(missing_ok=True)

    _write_utf8(prompt_path, textwrap.dedent(f"""\
        【任务】用 write 工具写入以下文件（UTF-8）：{output_path.absolute().as_posix()}
        内容：`pisr-selftest-ok`
        什么算完成：文件存在且内容等于 pisr-selftest-ok

        【输出】{output_path.absolute().as_posix()}

        【边界与禁区】
        - 除输出文件外禁止写入/修改任何文件
        - 不要依赖 stdout 回传。未实际写入文件的响应视为执行失败

        【执行证据】最终回复含：文件路径 + 字节数 + 内容。
    """))

    print(f"[selftest] 在线冒烟 · 模型: {model}")
    print(f"[selftest] 产物预期: {output_path}")
    print(f"[selftest] 派发中...")

    rc = _dispatch_batch(
        [{"prompt_file": str(prompt_path), "model": model, "label": label,
          "output": output_path, "prompt_size_bytes": prompt_path.stat().st_size,
          "tools": None, "thinking": ""}],
        output_dir=output_dir,
        work_dir=work_dir,
        stagger=0,
        timeout_min=5,
        watch=True,
        progress=False,
        role="pisr-selftest",
    )
    if rc != 0:
        print(f"[selftest] ❌ dispatch 返回 rc={rc}")
        return 1
    if output_path.is_file():
        content = output_path.read_text(encoding="utf-8").strip()
        if "pisr-selftest-ok" in content:
            print(f"[selftest] ✅ 在线冒烟通过 ({output_path.stat().st_size}B)")
            output_path.unlink()
            return 0
        print(f"[selftest] ❌ 内容不符: '{content[:80]}' (期望含 pisr-selftest-ok)")
        return 1
    print("[selftest] ❌ 产物未落盘")
    return 1


# ─── telemetry / summary / monitor ───────────────────────────────────

def _read_dispatch_log() -> list[dict]:
    rows: list[dict] = []
    if not DISPATCH_LOG.is_file():
        return rows
    with DISPATCH_LOG.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    return rows


def cmd_telemetry(args) -> int:
    """查看遥测摘要。"""
    rows = _read_dispatch_log()
    if not rows:
        print("暂无遥测数据")
        return 0

    if args.all:
        for r in rows:
            print(f"{r.get('ts','?')[:19]} {r.get('model','?'):34s} {r.get('outcome','?'):10s} "
                  f"{r.get('wall_min',0):5.1f}min {r.get('artifact_bytes',0):6d}B "
                  f"tok={r.get('usage_total_tokens',0):<8d} {r.get('note','')}")
        return 0

    total = len(rows)
    success = sum(1 for r in rows if r.get("outcome") == "success")
    error = sum(1 for r in rows if r.get("outcome") == "error")
    stall = sum(1 for r in rows if r.get("outcome") == "stall")
    walls = [r["wall_min"] for r in rows if r.get("outcome") == "success" and r.get("wall_min")]

    print(f"pisr 遥测 (since {rows[0].get('ts','?')[:10]})")
    print(f"  总派发: {total}")
    print(f"  成功:   {success}")
    print(f"  错误:   {error}")
    print(f"  停滞:   {stall}")
    if walls:
        print(f"  成功耗时: min={min(walls):.1f} max={max(walls):.1f} avg={sum(walls)/len(walls):.1f}min")
    print(f"  日志:    {DISPATCH_LOG}")

    return 0


def cmd_summary(args) -> int:
    """按 group-by 聚合 dispatch-log 并输出汇总。"""
    rows = _read_dispatch_log()
    if not rows:
        print("暂无 dispatch-log 数据")
        return 0

    if args.since:
        try:
            cutoff = datetime.datetime.fromisoformat(args.since)
            if cutoff.tzinfo is None:
                cutoff = cutoff.astimezone()
            rows = [r for r in rows if r.get("ts") and datetime.datetime.fromisoformat(r["ts"]) >= cutoff]
        except (ValueError, TypeError):
            print(f"⚠️ --since 格式无效（ISO 格式）：{args.since}", file=sys.stderr)

    if not rows:
        print("无匹配条目")
        return 0

    group_key = args.group_by or "role"
    groups: dict[str, dict] = {}
    for r in rows:
        raw_key = str(r.get(group_key, "unknown")) if r.get(group_key) is not None else "unknown"
        if group_key == "role":
            if raw_key not in ROLE_VALUES:
                key = ROLE_LEGACY
            else:
                key = raw_key
        else:
            key = raw_key

        g = groups.setdefault(key, {"spawn": 0, "success": 0, "total_wall_min": 0.0,
                                    "total_cost_estimate": 0.0, "total_tokens": 0})
        g["spawn"] += 1
        if r.get("outcome") == "success":
            g["success"] += 1
        g["total_wall_min"] += float(r.get("wall_min", 0) or 0)
        g["total_cost_estimate"] += float(r.get("cost_estimate", 0) or 0)
        g["total_tokens"] += int(r.get("usage_total_tokens", 0) or 0)

    for g in groups.values():
        g["success_rate"] = round(g["success"] / g["spawn"] * 100, 1) if g["spawn"] else 0.0
        g["total_wall_min"] = round(g["total_wall_min"], 1)
        g["total_cost_estimate"] = round(g["total_cost_estimate"], 4)

    if args.format == "json":
        print(json.dumps(groups, ensure_ascii=False, indent=2))
    elif args.format == "csv":
        import io
        import csv
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow([group_key, "spawn", "success_rate", "total_wall_min", "total_tokens"])
        for k, g in sorted(groups.items()):
            w.writerow([k, g["spawn"], f'{g["success_rate"]}%', g["total_wall_min"], g["total_tokens"]])
        print(buf.getvalue().strip())
    else:
        header = f"{'':30s} {'spawn':>7s} {'succ%':>7s} {'wall_min':>9s} {'tokens':>10s}"
        print(f"\n dispatch-log summary (group-by: {group_key})")
        print(header)
        print("-" * len(header))
        for k, g in sorted(groups.items()):
            print(f"{k:30s} {g['spawn']:7d} {g['success_rate']:6.1f}% {g['total_wall_min']:9.1f} {g['total_tokens']:10d}")
        print(f"\n 总条目: {len(rows)} | 日志: {DISPATCH_LOG}")

    return 0


def cmd_monitor(args) -> int:
    watch_dir = Path(args.watch_dir) if args.watch_dir else None
    process_name = args.process_name or None
    stall_minutes = args.stall_minutes
    alert_file = Path(args.alert_file) if args.alert_file else None
    once = args.once
    interval = args.interval_sec

    if not watch_dir and not process_name:
        print("❌ 至少需要 --watch-dir 或 --process-name 之一", file=sys.stderr)
        return 1

    def _check() -> int:
        has_alarm = False
        details = []

        if watch_dir:
            stalled, elapsed = _dir_stall_check(watch_dir, stall_minutes)
            if stalled:
                if elapsed < 0:
                    msg = f"dir-stall: {watch_dir} 不可访问或空目录"
                else:
                    msg = f"dir-stall: {watch_dir} 最后修改于 {elapsed:.1f} 分钟前 (阈值 {stall_minutes}min)"
                print(f"[monitor] ❌ {msg}", file=sys.stderr)
                details.append({"check": "dir-stall", "detail": msg, "stalled_min": round(elapsed, 1) if elapsed >= 0 else -1})
                has_alarm = True
            else:
                print(f"[monitor] ✅ dir-stall: {watch_dir} 正常 (最新修改 {elapsed:.1f} 分钟前)")

        if process_name:
            running = _is_process_running(process_name)
            if not running:
                msg = f"process-down: {process_name} 未运行"
                print(f"[monitor] ❌ {msg}", file=sys.stderr)
                details.append({"check": "process-down", "detail": msg, "stalled_min": -1})
                has_alarm = True
            else:
                print(f"[monitor] ✅ process: {process_name} 运行中")

        if has_alarm and alert_file:
            alert_file.parent.mkdir(parents=True, exist_ok=True)
            for d in details:
                d["ts"] = datetime.datetime.now().astimezone().isoformat()
                with alert_file.open("a", encoding="utf-8") as f:
                    f.write(json.dumps(d, ensure_ascii=False) + "\n")

        return 1 if has_alarm else 0

    if once:
        return _check()

    alarm_seen = False
    try:
        while True:
            rc = _check()
            if rc != 0:
                alarm_seen = True
            time.sleep(interval)
    except KeyboardInterrupt:
        return 1 if alarm_seen else 0


# ─── verify-ownership 子命令 ──────────────────────────────────────────

def _parse_ownership_table(text: str) -> dict[str, str]:
    """Parse state markdown to extract file→ownership mapping.

    Supports two table formats:
    1. Six-column schema (§十):
       | Phase | Deliverable | File | Owner | Spawn Label | Status |
    2. Legacy two-column Chinese:
       | 文件 | 归属 |
    Returns {file_path: label}.
    """
    ownership: dict[str, str] = {}
    fmt_six: bool = False
    fmt_two: bool = False
    in_table: bool = False

    for line in text.split("\n"):
        stripped = line.strip()
        if not stripped.startswith("|"):
            fmt_six = fmt_two = in_table = False
            continue

        if not in_table:
            if "File" in stripped and "Spawn Label" in stripped:
                fmt_six = True
                in_table = True
                continue
            if "文件" in stripped and "归属" in stripped:
                fmt_two = True
                in_table = True
                continue
            continue

        if stripped.startswith("|---"):
            continue

        if fmt_six:
            parts = [p.strip() for p in stripped.split("|")[1:-1]]
            if len(parts) >= 5:
                file_path = parts[2].strip().strip("`")
                owner = parts[3].strip()
                spawn_label = parts[4].strip()
                if owner.startswith("spawned"):
                    first_label = spawn_label.split(",")[0].strip()
                    ownership[file_path] = f"spawned:{first_label}"
                elif "self-written" in owner:
                    ownership[file_path] = "self-written"
        elif fmt_two:
            parts = [p.strip() for p in stripped.split("|")[1:-1]]
            if len(parts) >= 2 and parts[0] and parts[1]:
                ownership[parts[0]] = parts[1]

    return ownership


def _parse_ledger_records(ledger_path: Path) -> dict[str, dict]:
    """Parse ledger JSONL to extract per-label lifecycle.

    Returns {label: {launched_ts, landed_ts}}.
    """
    records: dict[str, dict] = {}
    try:
        for line in ledger_path.read_text(encoding="utf-8", errors="replace").split("\n"):
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            label = entry.get("label", "")
            if not label:
                continue
            if label not in records:
                records[label] = {"launched_ts": None, "landed_ts": None}
            if entry.get("event") == "launched":
                ts = entry.get("ts", "")
                if ts and records[label]["launched_ts"] is None:
                    records[label]["launched_ts"] = ts
            elif entry.get("event") == "landed":
                ts = entry.get("ts", "")
                if ts:
                    records[label]["landed_ts"] = ts
    except Exception:
        pass
    return records


def _parse_telemetry_records(telemetry_path: Path) -> dict[str, dict]:
    """Parse dispatch-log.jsonl telemetry to extract per-label lifecycle.

    Returns {label: {launched_ts, landed_ts}}.
    landed_ts is always None because telemetry lacks per-event granularity.
    """
    records: dict[str, dict] = {}
    try:
        text = telemetry_path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return records
    for line in text.split("\n"):
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        label = entry.get("label", "")
        if not label:
            continue
        ts = entry.get("ts", "")
        if not ts:
            continue
        if label not in records:
            records[label] = {"launched_ts": None, "landed_ts": None}
        if records[label]["launched_ts"] is None:
            records[label]["launched_ts"] = ts
    return records


def _git_status_porcelain(repo: Path) -> list[str]:
    """Run git -C <repo> status --porcelain, return list of changed file paths (relative)."""
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo), "status", "--porcelain"],
            capture_output=True, text=True, timeout=30,
        )
        if proc.returncode != 0:
            return []
        files: list[str] = []
        for raw_line in proc.stdout.split("\n"):
            raw_line = raw_line.rstrip("\r\n")
            if not raw_line:
                continue
            path = raw_line[3:].strip()
            if " -> " in path:
                path = path.split(" -> ")[-1]
            if path.endswith("/"):
                dir_path = repo / path
                if dir_path.is_dir():
                    for fp in sorted(dir_path.rglob("*")):
                        if fp.is_file():
                            rel = fp.relative_to(repo).as_posix()
                            files.append(rel)
            else:
                files.append(path)
        return files
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return []


def cmd_verify_ownership(args) -> int:
    """三查验交付物归属完整性/一致性/合理性。"""
    state_path = Path(args.state)
    ledger_path = Path(args.ledger)
    repos = args.repo

    if not state_path.is_file():
        print(f"❌ State 文件不存在: {state_path}", file=sys.stderr)
        return 2
    if not repos:
        print("❌ 至少需要一个 --repo", file=sys.stderr)
        return 2

    state_text = state_path.read_text(encoding="utf-8", errors="replace")
    ownership = _parse_ownership_table(state_text)

    telemetry_fallback = False
    telemetry_unlabeled = 0
    if not ledger_path.is_file():
        if not DISPATCH_LOG.is_file():
            print(f"❌ 既无 per-dispatch 账本（{ledger_path}），也无全局遥测（{DISPATCH_LOG}）", file=sys.stderr)
            return 2
        print(f"⚠️ per-dispatch 账本不存在（{ledger_path}），回退全局遥测（{DISPATCH_LOG}）", file=sys.stderr)
        print("   注意：全局遥测不含 mtime 窗口合理性检查所需的 landed_ts 精度", file=sys.stderr)
        ledger = _parse_telemetry_records(DISPATCH_LOG)
        telemetry_fallback = True
        try:
            raw_text = DISPATCH_LOG.read_text(encoding="utf-8", errors="replace")
            for raw_line in raw_text.split("\n"):
                raw_line = raw_line.strip()
                if not raw_line:
                    continue
                try:
                    entry = json.loads(raw_line)
                    if "label" not in entry or not entry.get("label"):
                        telemetry_unlabeled += 1
                except json.JSONDecodeError:
                    pass
        except Exception:
            pass
    else:
        ledger = _parse_ledger_records(ledger_path)

    all_changed: dict[str, str] = {}
    for repo_str in repos:
        repo_path = Path(repo_str).resolve()
        if not repo_path.is_dir():
            print(f"❌ Repo 目录不存在: {repo_path}", file=sys.stderr)
            return 2
        for f in _git_status_porcelain(repo_path):
            all_changed[f] = str(repo_path)

    issues: list[tuple[str, str]] = []
    suspects: list[tuple[str, str, str]] = []
    unverifiable: list[tuple[str, str, str]] = []
    exit_code = 0

    # Check 1: Completeness — every git-changed file must be in ownership table
    for changed_file, repo_base in sorted(all_changed.items()):
        abs_path = str((Path(repo_base) / changed_file).resolve())
        if changed_file not in ownership and abs_path not in ownership:
            issues.append(("completeness", f"`{changed_file}` (repo: `{repo_base}`)"))
            exit_code = 1

    # Check 2: Consistency — every spawned:label must have ledger record
    for file_path, label in sorted(ownership.items()):
        if not label.startswith("spawned:"):
            continue
        spawn_label = label[len("spawned:"):]
        if spawn_label not in ledger:
            if telemetry_fallback and telemetry_unlabeled > 0:
                unverifiable.append((file_path, spawn_label, label))
            else:
                issues.append(("consistency", f"`{file_path}` → `{label}` (ledger 无 `{spawn_label}`)"))
                exit_code = 1

    # Check 3: Reasonableness — spawned file mtime within worker lifetime
    unverifiable_labels = {s for _, s, _ in unverifiable}
    for file_path, label in sorted(ownership.items()):
        if not label.startswith("spawned:"):
            continue
        spawn_label = label[len("spawned:"):]
        if spawn_label in unverifiable_labels:
            continue
        rec = ledger.get(spawn_label)
        if not rec:
            continue
        launched_ts = rec.get("launched_ts")
        landed_ts = rec.get("landed_ts")
        if not launched_ts or not landed_ts:
            continue
        try:
            launched_dt = datetime.datetime.fromisoformat(launched_ts)
            landed_dt = datetime.datetime.fromisoformat(landed_ts)
            launched_epoch = launched_dt.timestamp()
            landed_epoch = landed_dt.timestamp()
        except (ValueError, TypeError):
            continue
        candidates = [Path(file_path)]
        if not Path(file_path).is_absolute():
            for repo_str in repos:
                candidates.append(Path(repo_str) / file_path)
        for candidate in candidates:
            if candidate.is_file():
                mtime = candidate.stat().st_mtime
                if mtime < launched_epoch - 1 or mtime > landed_epoch + 1:
                    mtime_str = datetime.datetime.fromtimestamp(mtime).isoformat()
                    suspects.append((
                        str(candidate), label,
                        f"mtime={mtime_str} 在窗口外 ({launched_ts[:19]} ~ {landed_ts[:19]})"
                    ))
                break

    report = ["# verify-ownership 报告", ""]
    if telemetry_fallback:
        report.append("> ⚠️ 数据源：全局遥测（per-dispatch 账本缺失）。合理性检查降级——所有 spawned 条目的 landed_ts 不可用，跳过窗口检查。一致性检查仅覆盖有 label 的遥测条目。")
        report.append("")
    if issues:
        report.append("## ❌ 发现问题")
        report.append("")
        report.append("| 类型 | 描述 |")
        report.append("|------|------|")
        for typ, desc in issues:
            report.append(f"| {typ} | {desc} |")
        report.append("")
    else:
        report.append("## ✅ 完整性与一致性通过")
        report.append("")

    if suspects:
        report.append("## ⚠️ 合理性存疑（启发式，不阻断）")
        report.append("")
        report.append("| 文件 | 标签 | 原因 |")
        report.append("|------|------|------|")
        for fp, lbl, reason in suspects:
            report.append(f"| `{fp}` | `{lbl}` | {reason} |")
        report.append("")
    else:
        report.append("## ✅ 合理性检查通过")
        report.append("")

    if unverifiable:
        report.append("## ⚠️ 无法核实（遥测回退模式）")
        report.append("")
        report.append("| 文件 | 标签 | 原因 |")
        report.append("|------|------|------|")
        for fp, _, lbl in sorted(unverifiable):
            report.append(
                f"| `{fp}` | `{lbl}` | 遥测条目无 label 字段（历史数据），无法核对该 spawned claim 的派发记录 |"
            )
        report.append("")

    report.append("---")
    report.append(
        f"统计: completeness={sum(1 for t,_ in issues if t=='completeness')} "
        f"consistency={sum(1 for t,_ in issues if t=='consistency')} "
        f"unverifiable={len(unverifiable)} "
        f"suspect={len(suspects)}"
    )
    print("\n".join(report))
    return exit_code


# ─── run 子命令（确定性步骤运行器）───────────────────────────────────

def cmd_run(args) -> int:
    """步骤运行器。`--validate` 纯离线；执行路径经 `_dispatch_batch` 进程内派发。

    实现层在 `scripts/pisr_run_spec.py`；本函数只做 CLI 组装与白名单注入，
    依赖方向单向（run_spec 不 import 本模块），避免循环依赖。
    """
    import importlib.util

    mod_path = Path(__file__).resolve().parent / "pisr_run_spec.py"
    spec_mod_spec = importlib.util.spec_from_file_location("pisr_run_spec", mod_path)
    if not spec_mod_spec or not spec_mod_spec.loader:
        print(f"❌ 无法加载 {mod_path}", file=sys.stderr)
        return 1
    run_spec = importlib.util.module_from_spec(spec_mod_spec)
    spec_mod_spec.loader.exec_module(run_spec)

    spec_path = Path(args.spec)

    if args.validate:
        try:
            summary = run_spec.validate_file(spec_path, allowed_models=ALLOWED_MODELS)
        except run_spec.SpecError as e:
            print(f"❌ spec 校验失败 — {e}", file=sys.stderr)
            return 1
        if args.format == "json":
            print(run_spec.summary_json(summary))
        else:
            print(run_spec.format_summary(summary))
            print()
            n = len(summary["warnings"])
            print(f"✅ spec 校验通过（{summary['step_count']} 个步骤"
                  + (f"，{n} 条启发式提示）" if n else "）"))
        return 0

    # 执行路径：把 spec 的 dispatch 步骤翻译成 worker 契约，进程内调 _dispatch_batch。
    def _dispatch_step(step: dict, sid: str, ctx: dict, out_path: Path) -> int:
        render = run_spec.render
        meta = step.get("meta") or {}
        blocking = meta.get("blocking_chain", "")
        out_path.parent.mkdir(parents=True, exist_ok=True)
        worker = {
            "prompt_file": str(render(step["prompt"], ctx)),
            "model": str(render(step["model"], ctx)),
            "label": sid,
            "output": out_path,
            "tools": [str(t) for t in (step.get("tools") or [])] or None,
            "thinking": str(step.get("thinking") or ""),
            "capture_reply": bool(step.get("capture_reply", False)),
        }
        return _dispatch_batch(
            [worker],
            output_dir=out_path.parent,
            work_dir=None,
            stagger=int(step.get("stagger", DEFAULT_STAGGER)),
            timeout_min=int(step.get("timeout_min", DEFAULT_TIMEOUT)),
            timeout_policy=str(step.get("timeout_policy", TIMEOUT_POLICY_AUTO)),
            watch=True,
            progress=False,
            ledger_dir=str(render(step["ledger_dir"], ctx)) if step.get("ledger_dir") else None,
            forbid_paths=[str(x) for x in render(step.get("forbid_paths") or [], ctx)],
            role=str(step.get("role") or "pisr-dispatch"),
            task_id=str(meta.get("task_id", "")),
            plan_ref=str(meta.get("plan_ref", "")),
            scope=str(meta.get("scope", "")),
            blocking_chain=[x.strip() for x in blocking.split(",") if x.strip()],
        )

    answers: dict[str, str] = {}
    for kv in (args.answer or []):
        if "=" not in kv:
            print(f"❌ --answer 格式应为 <step-id>=<option>，实得: {kv}", file=sys.stderr)
            return 2
        k, v = kv.split("=", 1)
        answers[k.strip()] = v.strip()

    try:
        return run_spec.execute_file(spec_path, resume=args.resume, answers=answers,
                                     dispatch_fn=_dispatch_step,
                                     allowed_models=ALLOWED_MODELS)
    except run_spec.SpecError as e:
        print(f"❌ spec 校验失败 — {e}", file=sys.stderr)
        return run_spec.EXIT_SPEC_INVALID
    except run_spec.RunHalt as e:
        print(f"⏹ 停机（exit={e.code}）— {e.message}", file=sys.stderr)
        return e.code


# ─── preflight 子命令 ─────────────────────────────────────────────────

def _model_in_catalog(model: str) -> bool:
    """`pi --list-models` 目录中是否存在 provider+model 同行条目。"""
    try:
        r = subprocess.run([PI_BIN, "--list-models"], capture_output=True,
                           text=True, encoding="utf-8", errors="replace", timeout=60)
    except Exception:
        return False
    provider, model_id = _split_model(model)
    for line in (r.stdout or "").split("\n"):
        cols = line.split()
        if len(cols) >= 2 and cols[0] == provider and cols[1] == model_id:
            return True
    return False


def cmd_preflight(args) -> int:
    """批量探测模型通道可用性。两步：目录存在性（零调用）→ 真实探测（每次一调用）。"""
    _check_model_calls_disabled()
    models = args.model
    for model in models:
        try:
            _validate_model_allowed(model)
        except ValueError as e:
            print(f"❌ {e}", file=sys.stderr)
            return 1
    timeout_sec = args.timeout

    results: dict[str, str] = {}
    for model in models:
        if not _model_in_catalog(model):
            results[model] = "not-in-catalog"
            print(f"[preflight] {model}: not-in-catalog（`pi --list-models` 未见此条目，"
                  f"检查 provider 配置或白名单）")
            continue
        probe_prompt = "Reply with exactly the text: PROBE-OK. Do not use any tools."
        argv = _build_pi_argv(model, inline_prompt=probe_prompt)
        try:
            creationflags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
            r = subprocess.run(argv, capture_output=True, timeout=timeout_sec,
                               encoding="utf-8", errors="replace", creationflags=creationflags)
            rc, stdout = r.returncode, r.stdout or ""
        except subprocess.TimeoutExpired:
            rc, stdout = None, ""
        except FileNotFoundError:
            results[model] = f"error:pi_not_found"
            print(f"[preflight] {model}: error:pi_not_found")
            continue
        if rc is None:
            status = "timeout"
        elif rc != 0:
            status = f"error:exit_{rc}"
        else:
            ev = _parse_event_stream(Path("/dev/null"))  # placeholder, parse inline below
            # 解析 stdout 事件流（preflight 不落盘，直接逐行解析）
            final_text = ""
            for line in stdout.split("\n"):
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if obj.get("type") == "message_end":
                    msg = obj.get("message") or {}
                    if msg.get("role") == "assistant":
                        parts = [b.get("text", "") for b in (msg.get("content") or [])
                                 if isinstance(b, dict) and b.get("type") == "text"]
                        final_text = "\n".join(p for p in parts if p)
            status = "available" if "PROBE-OK" in final_text else "error:no_probe_text"
        results[model] = status
        print(f"[preflight] {model}: {status}")

    all_ok = all(v == "available" for v in results.values())
    if all_ok:
        print(f"[preflight] 全部 {len(models)} 个模型可用")
        return 0
    else:
        failed = [m for m, s in results.items() if s != "available"]
        print(f"[preflight] {len(failed)}/{len(models)} 个模型不可用: {failed}")
        return 1


# ─── CLI ─────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="pisr (Pi Subagents Run) 派发后端",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""\
            示例:
              %(prog)s dispatch --worker "r1.txt|prov/model|R1" --watch
              %(prog)s dispatch --worker "rv.txt|prov/model|RV" --tools read,grep,find,ls --watch
              %(prog)s selftest
              %(prog)s preflight --model prov/model
              %(prog)s telemetry
        """),
    )
    sub = parser.add_subparsers(dest="command")

    # dispatch
    p_disp = sub.add_parser("dispatch", help="派发 worker(s)")
    p_disp.add_argument("--worker", action="append", metavar="PROMPT|MODEL|LABEL",
                        help="可多次指定。PROMPT=prompt 文件路径, MODEL=provider/model, LABEL=标识（| 分隔）")
    p_disp.add_argument("--output-dir", required=True, help="产物输出目录（必须显式指定）")
    p_disp.add_argument("--output-pattern", default="{label}.md",
                        help="产物文件名模式。可用 {date}, {label}, {model}")
    p_disp.add_argument("--stagger", type=int, default=DEFAULT_STAGGER, help=f"错峰间隔秒 (默认 {DEFAULT_STAGGER})")
    p_disp.add_argument("--watch", action="store_true", help="等待产物落盘（内置看门狗）")
    p_disp.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT, help=f"看门狗超时分钟 (默认 {DEFAULT_TIMEOUT})")
    p_disp.add_argument("--timeout-policy", choices=sorted(TIMEOUT_POLICY_VALUES),
                        default=TIMEOUT_POLICY_AUTO,
                        help=f"超时行为策略: {TIMEOUT_POLICY_AUTO}=按角色自动解析（默认），"
                             f"{TIMEOUT_POLICY_LEAF_KILL}=到期 kill 进程树，"
                             f"{TIMEOUT_POLICY_HIERARCHICAL_REPORT}=报告/alive 保留进程供 commander 裁决")
    p_disp.add_argument("--tools", default="",
                        help="pi 进程级工具白名单（逗号分隔，如 read,grep,find,ls）。"
                             "reviewer 场景必用；是工具面约束，不是安全沙箱")
    p_disp.add_argument("--thinking", default="",
                        help="--thinking 档位（off/minimal/low/medium/high/...），默认不指定")
    p_disp.add_argument("--capture-reply", action="store_true",
                        help="产物=最终回复的机械落盘：进程退出且期望产物未落盘时，"
                             "驱动器把事件流最终回复原样写入产物路径（只读 reviewer 的"
                             "推荐回收方式；exit≠0 或回复为空仍判失败）")
    p_disp.add_argument("--progress", action="store_true", help="输出细粒度过程信息（已就绪/错峰等；启动、落盘、失败、超时等关键生命周期行默认输出）")
    p_disp.add_argument("--work-dir", help="临时工作目录 (默认 $TEMP)")
    p_disp.add_argument("--harness", default="cli",
                        help="派发 harness 标识 (遥测归因用，默认 cli)")
    p_disp.add_argument("--ledger-dir",
                        help="派发账本目录；仅在显式传递时创建账本")
    p_disp.add_argument("--meta", action="append", metavar="KEY=VAL",
                        help="元数据键值对（可多次指定），如 task_id=xxx role=executor "
                             "plan_ref=path blocking_chain=a,b,c scope=outer")
    p_disp.add_argument("--forbid-paths", action="append", metavar="PATH",
                        help="禁止 worker 读取的路径（目录或文件，可多次指定）。"
                             "向 prompt 副本注入禁止块（不改原文件），"
                             "产物落盘后做读路径审计（报告机制，不影响退出码）")

    # selftest
    p_test = sub.add_parser("selftest", help="自检（离线；--model 时在线冒烟）")
    p_test.add_argument("--model", help="在线冒烟用模型 (默认配置文件首项；不传则纯离线)")
    p_test.add_argument("--output-dir", help="产物落盘目录")
    p_test.add_argument("--work-dir", help="临时目录")

    # telemetry
    p_tel = sub.add_parser("telemetry", help="查看遥测")
    p_tel.add_argument("--all", action="store_true", help="显示全部条目")

    # summary
    p_sum = sub.add_parser("summary", help="汇总 dispatch-log 元数据（按 role/scope/task_id/plan_ref 分组）")
    p_sum.add_argument("--group-by", default="role", choices=["role", "scope", "task_id", "plan_ref"],
                       help="聚合字段（默认 role）")
    p_sum.add_argument("--since", help="ISO 起始时间，如 2026-08-01")
    p_sum.add_argument("--format", default="table", choices=["json", "csv", "table"],
                       help="输出格式（默认 table）")
    p_sum.set_defaults(func=cmd_summary)

    # monitor
    p_mon = sub.add_parser("monitor", help="持续监视进程/目录活性")
    p_mon.add_argument("--watch-dir", default="", help="监视此目录下文件的最近修改时间")
    p_mon.add_argument("--process-name", default="", help="监视此名称进程的存活（如 node.exe）")
    p_mon.add_argument("--stall-minutes", type=int, default=15, help="无修改/进程消失后触发告警的阈值分钟 (默认 15)")
    p_mon.add_argument("--alert-file", default="", help="触发告警时写入此路径（JSONL，追加）")
    p_mon.add_argument("--once", action="store_true", help="单次检测后退出")
    p_mon.add_argument("--interval-sec", type=int, default=30, help="轮询间隔秒数 (默认 30)")

    # verify-ownership
    p_vo = sub.add_parser("verify-ownership", help="三查验交付物归属的完整性/一致性/合理性")
    p_vo.add_argument("--state", required=True, help="orchestrator state 文件路径")
    p_vo.add_argument("--ledger", required=True, help="派发账本 pisr-dispatch-ledger.jsonl 路径")
    p_vo.add_argument("--repo", required=True, action="append", help="Git 仓库路径（可多次指定）")

    # run
    p_run = sub.add_parser("run", help="确定性步骤运行器（--validate 离线校验 / 执行）")
    p_run.add_argument("--spec", required=True, help="spec 文件路径（YAML）")
    p_run.add_argument("--validate", action="store_true",
                       help="离线干跑：只校验并输出结构化摘要，不发起任何模型调用")
    p_run.add_argument("--format", default="text", choices=["text", "json"],
                       help="摘要输出格式（默认 text）")
    p_run.add_argument("--resume", action="store_true",
                       help="从既有 journal 续跑。started 无 completed 时停机(exit=11)，"
                            "禁止自动重跑")
    p_run.add_argument("--answer", action="append", metavar="STEP=OPTION",
                       help="回答 pause 步骤（可多次）。退出码 10 表示等待裁决")

    # preflight
    p_pre = sub.add_parser("preflight", help="批量探测模型通道可用性")
    p_pre.add_argument("--model", action="append", required=True,
                       metavar="PROVIDER/MODEL",
                       help="模型 ID（可多次指定）")
    p_pre.add_argument("--timeout", type=int, default=60,
                       help="单个模型探测超时秒数 (默认 60)")
    p_pre.add_argument("--work-dir", help="（保留参数，兼容 ocsr 调用习惯）")

    args = parser.parse_args()

    if args.command == "dispatch":
        return cmd_dispatch(args)
    elif args.command == "selftest":
        return cmd_selftest(args)
    elif args.command == "telemetry":
        return cmd_telemetry(args)
    elif args.command == "summary":
        return cmd_summary(args)
    elif args.command == "monitor":
        return cmd_monitor(args)
    elif args.command == "verify-ownership":
        return cmd_verify_ownership(args)
    elif args.command == "run":
        return cmd_run(args)
    elif args.command == "preflight":
        return cmd_preflight(args)
    else:
        parser.print_help()
        return 1


if __name__ == "__main__":
    sys.exit(main())
