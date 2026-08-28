"""stub-pi 端到端集成测试：PI_BIN 指向测试桩，走**真实** subprocess 链路
（Popen spawn → stdout/stderr 重定向 → poll → taskkill → 事件流解析 →
capture-reply 落盘 → 遥测），零模型调用。

与 test_execution_layer_integrity.py 的差异：那边 mock 掉 Popen 只测结案语义；
这边不 mock Popen，专抓 argv 构造、重定向、进程生命周期等真实 OS 层回归。
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path

import pytest

# Fail-safe 兜底（本文件内部会用 contextmanager 临时摘除以放行 _dispatch_batch）
os.environ.setdefault("PISR_DISABLE_MODEL_CALLS", "1")

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "pisr_dispatch.py"
_SPEC = importlib.util.spec_from_file_location("pisr_dispatch_e2e", SCRIPT)
mod = importlib.util.module_from_spec(_SPEC)
assert _SPEC and _SPEC.loader
sys.modules[_SPEC.name] = mod
_SPEC.loader.exec_module(mod)

STUB_SRC = Path(__file__).resolve().parent / "stub_pi.py"


def _make_stub_bin(td: Path) -> str:
    """生成平台包装器（CreateProcess 无法直接执行 .py，需经 cmd/sh 中转）。"""
    if sys.platform == "win32":
        wrapper = td / "stub_pi.cmd"
        wrapper.write_text(f'@echo off\r\n"{sys.executable}" "{STUB_SRC}" %*\r\n',
                           encoding="ascii")
        return str(wrapper)
    wrapper = td / "stub_pi.sh"
    wrapper.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{STUB_SRC}" "$@"\n',
                       encoding="utf-8")
    wrapper.chmod(0o755)
    return str(wrapper)


@contextmanager
def _stub_env(td: Path, **env):
    """隔离 e2e 环境：PI_BIN→桩、CHECK_INTERVAL→0.5s、白名单→stub/model、
    遥测→临时文件、摘除 PISR_DISABLE_MODEL_CALLS（_dispatch_batch 有真熔断）。"""
    stub = _make_stub_bin(td)
    saved = {
        "PI_BIN": mod.PI_BIN,
        "CHECK_INTERVAL": mod.CHECK_INTERVAL,
        "ALLOWED_MODELS": mod.ALLOWED_MODELS,
        "DISPATCH_LOG": mod.DISPATCH_LOG,
        "tripwire": os.environ.get("PISR_DISABLE_MODEL_CALLS"),
    }
    mod.PI_BIN = stub
    mod.CHECK_INTERVAL = 0.5
    mod.ALLOWED_MODELS = frozenset({"stub/model"})
    mod.DISPATCH_LOG = td / "dispatch-log.jsonl"
    os.environ.pop("PISR_DISABLE_MODEL_CALLS", None)
    for k, v in env.items():
        os.environ[k] = v
    try:
        yield stub
    finally:
        mod.PI_BIN = saved["PI_BIN"]
        mod.CHECK_INTERVAL = saved["CHECK_INTERVAL"]
        mod.ALLOWED_MODELS = saved["ALLOWED_MODELS"]
        mod.DISPATCH_LOG = saved["DISPATCH_LOG"]
        if saved["tripwire"] is not None:
            os.environ["PISR_DISABLE_MODEL_CALLS"] = saved["tripwire"]
        for k in env:
            os.environ.pop(k, None)


def _worker_dict(td: Path, label: str, **kw) -> dict:
    prompt = td / f"prompt-{label}.txt"
    prompt.write_text(f"stub prompt for {label} → {{outputs}}", encoding="utf-8")
    out = td / f"{label}.md"
    w = {"prompt_file": str(prompt), "model": "stub/model", "label": label,
         "output": out, "tools": None, "thinking": "", "capture_reply": False,
         "prompt_size_bytes": prompt.stat().st_size, "forbid_paths": []}
    w.update(kw)
    return w


def _rows(td: Path) -> list[dict]:
    log = td / "dispatch-log.jsonl"
    if not log.is_file():
        return []
    return [json.loads(l) for l in log.read_text(encoding="utf-8").splitlines() if l.strip()]


class TestStubE2E:
    def test_capture_reply_full_chain(self):
        """真实链路：spawn → 事件流重定向 → poll → capture-reply 机械落盘 → 遥测。"""
        with tempfile.TemporaryDirectory() as t:
            td = Path(t)
            with _stub_env(td, STUB_MODE="events", STUB_TEXT="E2E_CAPTURE_OK",
                           STUB_TOOLS="read,grep"):
                w = _worker_dict(td, "rv", tools=["read", "grep", "find", "ls"],
                                 capture_reply=True)
                rc = mod._dispatch_batch([w], output_dir=td, work_dir=td,
                                         stagger=0, timeout_min=5, watch=True,
                                         progress=False, role="e2e")
            assert rc == 0, "e2e capture 应成功"
            art = Path(w["output"])
            assert art.is_file() and art.stat().st_size > 0
            assert "E2E_CAPTURE_OK" in art.read_text(encoding="utf-8")
            # 真实事件流确实经重定向落盘且被解析（usage 来自桩的 18 tokens）
            rows = _rows(td)
            ok = [r for r in rows if r.get("outcome") == "success"]
            assert ok and ok[-1]["usage_total_tokens"] == 18
            assert ok[-1]["tool_calls"] == 2
            assert ok[-1]["tool_audit"] == "clean"

    def test_nonzero_exit_marks_failure(self):
        """真实 poll() 非零退出 → 确定性失败，stderr 进入归因。"""
        with tempfile.TemporaryDirectory() as t:
            td = Path(t)
            with _stub_env(td, STUB_MODE="exit3"):
                w = _worker_dict(td, "boom")
                rc = mod._dispatch_batch([w], output_dir=td, work_dir=td,
                                         stagger=0, timeout_min=5, watch=True,
                                         progress=False, role="e2e")
            assert rc == mod.EXIT_DETERMINISTIC_FAILURE
            rows = _rows(td)
            assert any(r.get("outcome_detail") == "error:exit_code_3" for r in rows), rows
            stderr_log = Path(w["work_dir"]) / "stderr.log"
            assert "stub boom" in stderr_log.read_text(encoding="utf-8")

    def test_garbage_stream_flags_schema_drift(self):
        """e2e 漂移：桩输出垃圾事件流、exit=0、零产物 → schema_drift_suspect。"""
        with tempfile.TemporaryDirectory() as t:
            td = Path(t)
            with _stub_env(td, STUB_MODE="garbage"):
                w = _worker_dict(td, "drift")
                rc = mod._dispatch_batch([w], output_dir=td, work_dir=td,
                                         stagger=0, timeout_min=5, watch=True,
                                         progress=False, role="e2e")
            assert rc == mod.EXIT_DETERMINISTIC_FAILURE
            rows = _rows(td)
            assert any(r.get("outcome_detail") == "error:schema_drift_suspect"
                       for r in rows), rows

    def test_kill_terminates_real_tree(self):
        """真实进程树终止：静默挂起的桩被 _kill_worker 按 PID 杀死。"""
        with tempfile.TemporaryDirectory() as t:
            td = Path(t)
            with _stub_env(td, STUB_MODE="sleep", STUB_SLEEP="300"):
                w = _worker_dict(td, "sleeper")
                rc = mod._dispatch_batch([w], output_dir=td, work_dir=td,
                                         stagger=0, timeout_min=5, watch=False,
                                         progress=False, role="e2e")
                assert rc == 0  # 未启用 --watch：仅启动即返回
                proc = w.get("proc")
                assert proc is not None and proc.poll() is None, "桩进程应存活"
                assert mod._kill_worker(w["label"], w) is True
                deadline = time.time() + 15
                while time.time() < deadline and proc.poll() is None:
                    time.sleep(0.2)
                assert proc.poll() is not None, "taskkill 报告成功但桩进程仍存活"
