"""执行层验收语义回归测试（pisr 移植版，plan 20260826-pisr-bootstrap T6）。

覆盖四条 P0：
  A1 `_watch_loop` 失败结案与退出码契约（0/1/2，混合结局优先级 1>2>0）
  A2 工具越权审计：产物已落盘但 toolcall 超出 --tools 白名单 → 确定性失败
  A3 看门狗按 PID 终止且校验 taskkill 退出码

全部离线，不触发任何模型调用。
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest.mock as mock
from pathlib import Path

import pytest

# Fail-safe: 防止任何意外的模型调用
os.environ["PISR_DISABLE_MODEL_CALLS"] = "1"

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "pisr_dispatch.py"
SPEC = importlib.util.spec_from_file_location("pisr_dispatch_eli", SCRIPT)
mod = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
sys.modules[SPEC.name] = mod
SPEC.loader.exec_module(mod)


class _FakeProc:
    """Popen 替身：poll() 返回预设退出码。"""

    def __init__(self, exit_code: int | None, pid: int = 4242) -> None:
        self.exit_code = exit_code
        self.pid = pid

    def poll(self) -> int | None:
        return self.exit_code

    def wait(self, timeout=None) -> int:
        return self.exit_code if self.exit_code is not None else 0

    def kill(self) -> None:
        self.exit_code = -9


def _events(td: Path, name: str, *, tool_names=None, final_text="done",
            usage=None) -> Path:
    wd = td / f"wd-{name}"
    wd.mkdir(parents=True, exist_ok=True)
    lines = ['{"type":"session","version":3,"id":"sid"}', '{"type":"agent_start"}']
    for t in (tool_names or []):
        lines.append(f'{{"type":"tool_execution_start","toolCallId":"x","toolName":"{t}","args":{{}}}}')
    u = usage or {"input": 10, "output": 5, "totalTokens": 15, "cost": {"total": 0}}
    content = json.dumps(
        [{"type": "text", "text": final_text}], ensure_ascii=False)
    lines.append(
        '{"type":"message_end","message":{"role":"assistant","content":' + content +
        ',"usage":' + json.dumps(u) + ',"stopReason":"stop"}}')
    lines.append('{"type":"agent_end","messages":[]}')
    p = wd / "events.jsonl"
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return p


def _worker(td: Path, name: str, *, proc=None, events: Path | None = None,
            artifact: str | None = None, tools=None, spawn_failed=False) -> dict:
    wd = td / f"wd-{name}"
    wd.mkdir(parents=True, exist_ok=True)
    out = td / f"{name}.md"
    if artifact is not None:
        out.write_text(artifact, encoding="utf-8")
    return {"output": out, "label": name, "model": "prov/model-x",
            "prompt_size_bytes": 10, "work_dir": wd,
            "events_path": events or (wd / "events.jsonl"),
            "stderr_path": wd / "stderr.log",
            "proc": proc, "tools": tools, "thinking": "",
            "spawn_failed": spawn_failed, "forbid_paths": []}


class _FakeClock:
    """可控时钟：`sleep` 只推进虚拟时间，不耗墙钟。deadline 判定读 time.time()，
    必须整体替换时钟而非只 mock sleep，否则对着真实时钟忙等。"""

    def __init__(self, start: float = 1_000_000.0) -> None:
        self.t = start

    def time(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.t += max(float(seconds), 1.0)


def _run_watch(td: Path, parsed: list[dict], *, timeout_min: int = 5,
               started_ago: float = 0.0, kill_ok: bool = True, **kw):
    """跑 _watch_loop，隔离遥测日志与真实时钟。"""
    old_log = mod.DISPATCH_LOG
    mod.DISPATCH_LOG = td / "dispatch-log.jsonl"
    clock = _FakeClock()
    start_times = [clock.time() - started_ago for _ in parsed]
    try:
        with mock.patch.object(mod.time, "time", clock.time), \
             mock.patch.object(mod.time, "sleep", clock.sleep), \
             mock.patch.object(mod, "_kill_worker",
                               lambda label, w: kill_ok):
            rc = mod._watch_loop(parsed, start_times, timeout_min=timeout_min,
                                 progress=False, **kw)
        rows = []
        if mod.DISPATCH_LOG.is_file():
            rows = [json.loads(l) for l in
                    mod.DISPATCH_LOG.read_text(encoding="utf-8").splitlines() if l.strip()]
        return rc, rows
    finally:
        mod.DISPATCH_LOG = old_log


# ─── A1 · 退出码契约 ─────────────────────────────────────────────────
class TestExitCodeContract:
    """dispatch --watch 的退出码必须忠实反映真实结果（继承 ocsr A1 事故教训）。"""

    def test_nonzero_exit_zero_artifact_returns_2(self, capsys):
        with tempfile.TemporaryDirectory() as t:
            td = Path(t)
            w = _worker(td, "w0", proc=_FakeProc(1))
            rc, _ = _run_watch(td, [w])
            assert rc == mod.EXIT_DETERMINISTIC_FAILURE
            assert "全部 worker 完成" not in capsys.readouterr().out

    def test_zero_exit_zero_artifact_returns_2(self, capsys):
        """exit=0 但期望产物未落盘 → 确定性失败，且 outcome_detail 可归因。"""
        with tempfile.TemporaryDirectory() as t:
            td = Path(t)
            ev = _events(td, "w0", final_text="I cannot write files")
            w = _worker(td, "w0", proc=_FakeProc(0), events=ev)
            rc, rows = _run_watch(td, [w])
            assert rc == mod.EXIT_DETERMINISTIC_FAILURE
            out = capsys.readouterr().out
            assert "全部 worker 完成" not in out
            assert "exit=0" in out
            assert any(r.get("outcome_detail") == "error:exit_0_no_artifact" for r in rows), rows
            # 失败归因应携带模型末段文本
            assert any("cannot write" in (r.get("note") or "") for r in rows), rows

    def test_spawn_failed_returns_2(self):
        with tempfile.TemporaryDirectory() as t:
            td = Path(t)
            rc, _ = _run_watch(td, [_worker(td, "w0", spawn_failed=True)])
            assert rc == mod.EXIT_DETERMINISTIC_FAILURE

    def test_all_landed_returns_0(self, capsys):
        with tempfile.TemporaryDirectory() as t:
            td = Path(t)
            ev = _events(td, "w0", tool_names=["write"])
            w = _worker(td, "w0", proc=_FakeProc(0), events=ev, artifact="real content")
            rc, rows = _run_watch(td, [w])
            assert rc == 0
            assert "全部 worker 完成" in capsys.readouterr().out
            assert any(r.get("outcome") == "success" and r.get("usage_total_tokens") == 15
                       and r.get("tool_calls") == 1 for r in rows), rows

    def test_artifact_landed_but_process_alive_waits(self):
        """产物已落盘但进程未退出：暂不结案（等完整 usage/审计），未超时不判失败。"""
        with tempfile.TemporaryDirectory() as t:
            td = Path(t)
            ev = _events(td, "w0")
            w = _worker(td, "w0", proc=_FakeProc(None), events=ev, artifact="x")
            rc, _ = _run_watch(td, [w], timeout_min=1, started_ago=0)
            # 虚拟时钟会推进到超时 → 该 worker 归 timed_out(1)；关键是没被记成 landed/0
            assert rc == 1

    def test_partial_landed_partial_failed_returns_2(self):
        with tempfile.TemporaryDirectory() as t:
            td = Path(t)
            ok = _worker(td, "ok", proc=_FakeProc(0), events=_events(td, "ok"),
                         artifact="content")
            bad = _worker(td, "bad", proc=_FakeProc(1))
            rc, _ = _run_watch(td, [ok, bad])
            assert rc == mod.EXIT_DETERMINISTIC_FAILURE

    def test_failure_plus_timeout_returns_1(self):
        """混合结局优先级：看门狗超时(1) 优先于确定性失败(2)。"""
        with tempfile.TemporaryDirectory() as t:
            td = Path(t)
            bad = _worker(td, "bad", proc=_FakeProc(1))
            slow = _worker(td, "slow", proc=_FakeProc(None))  # 永不结案 → 超时
            rc, _ = _run_watch(td, [bad, slow], timeout_min=1, started_ago=600)
            assert rc == 1

    def test_settled_failure_not_double_killed(self):
        with tempfile.TemporaryDirectory() as t:
            td = Path(t)
            killed: list[str] = []
            old_log = mod.DISPATCH_LOG
            mod.DISPATCH_LOG = td / "log.jsonl"
            clock = _FakeClock()
            w = _worker(td, "bad", proc=_FakeProc(1))
            try:
                with mock.patch.object(mod.time, "time", clock.time), \
                     mock.patch.object(mod.time, "sleep", clock.sleep), \
                     mock.patch.object(mod, "_kill_worker",
                                       lambda label, _w: (killed.append(label), True)[1]):
                    rc = mod._watch_loop([w], [clock.time() - 600], timeout_min=1,
                                         progress=False)
            finally:
                mod.DISPATCH_LOG = old_log
            assert rc == mod.EXIT_DETERMINISTIC_FAILURE
            assert killed == [], f"已结案的 worker 被二次 kill: {killed}"

    def test_tool_violation_telemetry_details(self):
        with tempfile.TemporaryDirectory() as t:
            td = Path(t)
            ev = _events(td, "w0", tool_names=["read", "bash"], final_text="hmm")
            w = _worker(td, "w0", proc=_FakeProc(1), events=ev,
                        tools=["read", "grep", "find", "ls"])
            _, rows = _run_watch(td, [w])
            assert any(r.get("tool_violations") == 1 and r.get("tool_audit") == "violated"
                       for r in rows), rows


# ─── A2 · 工具越权审计 ───────────────────────────────────────────────
class TestToolViolationAudit:
    def test_landed_artifact_with_violation_fails(self, capsys):
        """产物已落盘 + 进程正常退出 + 越权 toolcall → 确定性失败（fail-closed）。"""
        with tempfile.TemporaryDirectory() as t:
            td = Path(t)
            ev = _events(td, "w0", tool_names=["read", "bash"])
            w = _worker(td, "w0", proc=_FakeProc(0), events=ev, artifact="content",
                        tools=["read", "grep", "find", "ls"])
            rc, rows = _run_watch(td, [w])
            assert rc == mod.EXIT_DETERMINISTIC_FAILURE
            assert "工具越权" in capsys.readouterr().err
            assert any(r.get("outcome_detail", "").startswith("tool_violation:bash")
                       for r in rows), rows

    def test_landed_artifact_clean_audit_passes(self):
        with tempfile.TemporaryDirectory() as t:
            td = Path(t)
            ev = _events(td, "w0", tool_names=["read", "grep", "read"])
            w = _worker(td, "w0", proc=_FakeProc(0), events=ev, artifact="content",
                        tools=["read", "grep", "find", "ls"])
            rc, rows = _run_watch(td, [w])
            assert rc == 0
            assert any(r.get("tool_audit") == "clean" for r in rows), rows

    def test_no_allowlist_records_unenforced(self):
        with tempfile.TemporaryDirectory() as t:
            td = Path(t)
            ev = _events(td, "w0", tool_names=["bash", "write"])
            w = _worker(td, "w0", proc=_FakeProc(0), events=ev, artifact="content",
                        tools=None)
            rc, rows = _run_watch(td, [w])
            assert rc == 0
            assert any(r.get("tool_audit") == "unenforced" for r in rows), rows


# ─── A3 · PID 终止 ───────────────────────────────────────────────────
class TestPidKill:
    def test_kill_uses_pid_tree_kill(self):
        with tempfile.TemporaryDirectory() as t:
            w = {"proc": _FakeProc(None, pid=4242), "label": "w0", "work_dir": Path(t)}
            calls: list[list[str]] = []

            def fake_run(argv, **kw):
                calls.append(argv)
                return subprocess.CompletedProcess(argv, 0, "SUCCESS", "")

            with mock.patch.object(mod.subprocess, "run", fake_run):
                assert mod._kill_worker("w0", w) is True
            assert calls == [["taskkill", "/F", "/T", "/PID", "4242"]]
            assert not any("/IM" in str(a) for a in calls), "禁止无差别 taskkill /IM"

    def test_kill_reports_failure_on_nonzero(self):
        with tempfile.TemporaryDirectory() as t:
            w = {"proc": _FakeProc(None, pid=4242), "label": "w0", "work_dir": Path(t)}
            with mock.patch.object(
                mod.subprocess, "run",
                lambda argv, **kw: subprocess.CompletedProcess(argv, 128, "", "not found"),
            ):
                assert mod._kill_worker("w0", w) is False

    def test_kill_failure_recorded_as_killed_failed(self):
        """kill 失败必须是 killed:failed，不得降级为普通 stall。"""
        with tempfile.TemporaryDirectory() as t:
            td = Path(t)
            w = _worker(td, "w0", proc=_FakeProc(None))
            _, rows = _run_watch(td, [w], timeout_min=1, started_ago=600, kill_ok=False,
                                 timeout_policy=mod.TIMEOUT_POLICY_LEAF_KILL)
            assert any(r.get("outcome_detail") == "killed:failed" for r in rows), rows

    def test_kill_success_not_killed_failed(self):
        with tempfile.TemporaryDirectory() as t:
            td = Path(t)
            w = _worker(td, "w0", proc=_FakeProc(None))
            _, rows = _run_watch(td, [w], timeout_min=1, started_ago=600, kill_ok=True,
                                 timeout_policy=mod.TIMEOUT_POLICY_LEAF_KILL)
            assert not any(r.get("outcome_detail") == "killed:failed" for r in rows), rows

    def test_hierarchical_report_policy_keeps_alive(self, capsys):
        with tempfile.TemporaryDirectory() as t:
            td = Path(t)
            w = _worker(td, "orch", proc=_FakeProc(None))
            _, rows = _run_watch(td, [w], timeout_min=1, started_ago=600,
                                 timeout_policy=mod.TIMEOUT_POLICY_HIERARCHICAL_REPORT)
            assert any(r.get("outcome_detail") == "reported:alive" for r in rows), rows
            assert "报告/alive" in capsys.readouterr().out

    @pytest.mark.skipif(sys.platform != "win32", reason="taskkill 仅 Windows")
    def test_kill_actually_terminates_process(self):
        """离线集成：真起一个进程、真杀掉、断言它确实消失（不触发模型调用）。"""
        exe = "pwsh" if shutil_which("pwsh") else "powershell"
        proc = subprocess.Popen(
            [exe, "-NoProfile", "-Command", "Start-Sleep -Seconds 120"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        try:
            w = {"proc": proc, "label": "dummy", "work_dir": None}
            assert proc.poll() is None, "被测进程未能启动"
            assert mod._kill_worker("dummy", w) is True
            for _ in range(60):
                if proc.poll() is not None:
                    break
                time.sleep(0.1)
            assert proc.poll() is not None, "taskkill 报告成功但目标进程仍存活"
        finally:
            if proc.poll() is None:
                proc.kill()


def shutil_which(name: str) -> bool:
    import shutil
    return shutil.which(name) is not None
