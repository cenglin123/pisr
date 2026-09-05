"""pisr_dispatch.py 单元测试：遥测字段、argv 构造、事件流解析、工具审计、
forbid-paths 注入与 reads: 审计、summary 聚合、白名单 fail-closed、快照碰撞。

全部离线，不触发任何模型调用（PISR_DISABLE_MODEL_CALLS=1 兜底）。
事件流 fixture 取自 2026-08-26 本机 pi 0.84.3 真实输出（脱敏）。
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
from pathlib import Path
from unittest import mock

import pytest

# Fail-safe: prevent any accidental model call in tests
os.environ["PISR_DISABLE_MODEL_CALLS"] = "1"

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "pisr_dispatch.py"
SPEC = importlib.util.spec_from_file_location("pisr_dispatch", SCRIPT)
mod = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
sys.modules[SPEC.name] = mod
SPEC.loader.exec_module(mod)


# ─── 遥测字段 ────────────────────────────────────────────────────────
class TestFieldDefaults:
    def test_minimal_call_has_all_required_fields(self):
        with tempfile.TemporaryDirectory() as td:
            mod.DISPATCH_LOG = Path(td) / "dispatch-log.jsonl"
            mod._append_telemetry(
                model="prov/model", role="pisr-dispatch", channel="detached",
                outcome="success", wall_min=1.5, artifact_bytes=100,
            )
            rows = [json.loads(l) for l in
                    mod.DISPATCH_LOG.read_text(encoding="utf-8").splitlines() if l.strip()]
            assert len(rows) == 1
            r = rows[0]
            for field, kind in mod.TELEMETRY_FIELDS.items():
                if kind == "required":
                    assert field in r, f"required field {field} missing"
            assert r["task_id"] and r["task_id"].startswith("dispatch_")
            assert r["blocking_chain"] == []
            assert r["failure_retry_index"] == 0
            assert r["cost_estimate"] == 0.0
            assert r["usage_input"] == 0
            assert r["usage_total_tokens"] == 0
            assert r["tool_calls"] == 0
            assert r["tool_violations"] == 0

    def test_usage_and_tool_passthrough(self):
        with tempfile.TemporaryDirectory() as td:
            mod.DISPATCH_LOG = Path(td) / "dispatch-log.jsonl"
            mod._append_telemetry(
                model="p/m", role="reviewer", channel="detached",
                outcome="success", wall_min=2.0, artifact_bytes=50,
                usage_input=2241, usage_output=18, usage_total_tokens=5331,
                usage_cost=0.0, tool_calls=4, tool_violations=0,
                tool_audit="clean",
            )
            r = json.loads(mod.DISPATCH_LOG.read_text(encoding="utf-8").splitlines()[0])
            assert r["usage_input"] == 2241
            assert r["usage_output"] == 18
            assert r["usage_total_tokens"] == 5331
            assert r["tool_calls"] == 4
            assert r["tool_violations"] == 0
            assert r["tool_audit"] == "clean"

    def test_role_passthrough_and_normalize_helper(self):
        with tempfile.TemporaryDirectory() as td:
            mod.DISPATCH_LOG = Path(td) / "dispatch-log.jsonl"
            # _append_telemetry 原样落盘（归一化在调用方 _normalize_role）
            mod._append_telemetry("t/m", "legacy-role", "fg", "success", 1.0, 10)
            lines = mod.DISPATCH_LOG.read_text(encoding="utf-8").splitlines()
            assert json.loads(lines[0])["role"] == "legacy-role"
            assert mod._normalize_role("legacy-role") == mod.ROLE_LEGACY
            assert mod._normalize_role("executor") == "executor"


# ─── argv 构造 ───────────────────────────────────────────────────────
class TestBuildArgv:
    def test_baseline_flags(self):
        argv = mod._build_pi_argv("prov/model-x")
        assert argv[1:10] == ["--mode", "json", "--no-session", "-nc", "-na",
                              "--provider", "prov", "--model", "model-x"]

    def test_tools_and_thinking_and_atfile(self):
        argv = mod._build_pi_argv("prov/model-x", tools=["read", "grep", "find", "ls"],
                                  thinking="low", prompt_at="C:/t/p.txt")
        assert "--thinking" in argv and argv[argv.index("--thinking") + 1] == "low"
        assert "--tools" in argv and argv[argv.index("--tools") + 1] == "read,grep,find,ls"
        assert argv[-1] == "@C:/t/p.txt"

    def test_inline_prompt(self):
        argv = mod._build_pi_argv("prov/model-x", inline_prompt="hi")
        assert argv[-1] == "hi"


# ─── 事件流解析（真实 fixture）───────────────────────────────────────
REAL_EVENTS = """\
{"type":"session","version":3,"id":"01a03a1a-981a-7e1f-ace8-a5e0fff35eb1","timestamp":"2026-08-25T18:06:56.538Z","cwd":"D:\\\\tmp"}
{"type":"agent_start"}
{"type":"turn_start"}
{"type":"tool_execution_start","toolCallId":"tc1","toolName":"read","args":{"path":"C:/x.md"}}
{"type":"tool_execution_end","toolCallId":"tc1","toolName":"read","result":"ok","isError":false}
{"type":"tool_execution_start","toolCallId":"tc2","toolName":"grep","args":{"pattern":"verdict"}}
{"type":"tool_execution_end","toolCallId":"tc2","toolName":"grep","result":"2 matches","isError":false}
{"type":"message_end","message":{"role":"assistant","content":[{"type":"thinking","thinking":"..."},{"type":"text","text":"MODEJSON_OK"}],"api":"openai-completions","provider":"xiaomi","model":"mimo-v2.5","usage":{"input":2241,"output":18,"cacheRead":3072,"cacheWrite":0,"reasoning":13,"totalTokens":5331,"cost":{"input":0,"output":0,"cacheRead":0,"cacheWrite":0,"total":0}},"stopReason":"stop"}}
{"type":"agent_end","messages":[]}
{"type":"agent_settled"}
"""


class TestParseEventStream:
    def _fixture(self, td: Path) -> Path:
        p = Path(td) / "events.jsonl"
        p.write_text(REAL_EVENTS, encoding="utf-8")
        return p

    def test_extracts_final_text_usage_tools(self):
        with tempfile.TemporaryDirectory() as td:
            ev = mod._parse_event_stream(self._fixture(td))
            assert ev["final_text"] == "MODEJSON_OK"
            assert ev["session_id"].startswith("01a03a1a")
            assert ev["stop_reason"] == "stop"
            assert ev["tool_names"] == ["read", "grep"]
            u_in, u_out, u_tot, u_cost = mod._usage_fields(ev["usage"])
            assert (u_in, u_out, u_tot, u_cost) == (2241, 18, 5331, 0.0)

    def test_tolerates_garbage_and_missing(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "e.jsonl"
            p.write_text("not json\n\n{bad}\n", encoding="utf-8")
            ev = mod._parse_event_stream(p)
            assert ev["final_text"] == ""
            assert ev["tool_names"] == []
        ev = mod._parse_event_stream(Path(td) / "missing.jsonl")
        assert ev["final_text"] == ""

    def test_later_assistant_message_wins(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "e.jsonl"
            two = (REAL_EVENTS +
                   '{"type":"message_end","message":{"role":"assistant","content":[{"type":"text","text":"FINAL2"}],"usage":{"input":5,"output":6,"totalTokens":11,"cost":{"total":0.5}},"stopReason":"stop"}}\n')
            p.write_text(two, encoding="utf-8")
            ev = mod._parse_event_stream(p)
            assert ev["final_text"] == "FINAL2"
            assert mod._usage_fields(ev["usage"]) == (5, 6, 11, 0.5)


# ─── 工具越权审计 ────────────────────────────────────────────────────
class TestToolAudit:
    def test_clean(self):
        status, v = mod._audit_tool_calls(["read", "grep", "read"], ["read", "grep", "find", "ls"])
        assert status == "clean" and v == []

    def test_violated(self):
        status, v = mod._audit_tool_calls(["read", "bash"], ["read", "grep", "find", "ls"])
        assert status == "violated" and v == ["bash"]

    def test_unenforced_when_no_allowlist(self):
        status, v = mod._audit_tool_calls(["write", "bash"], None)
        assert status == "unenforced" and v == []


# ─── forbid-paths / reads: 审计 ──────────────────────────────────────
class TestForbidAndReads:
    def test_forbid_block_lists_paths_and_reads_requirement(self):
        block = mod._build_forbid_block(["C:/a/b", "C:/x/y.md"])
        assert "C:/a/b" in block and "C:/x/y.md" in block
        assert "reads:" in block

    def test_parse_reads_list_yaml_style(self):
        text = "前言\nreads:\n  - C:/a.md\n  - C:/b.py\n\n后文"
        assert mod._parse_reads_list(text) == ["C:/a.md", "C:/b.py"]

    def test_parse_reads_list_inline_style(self):
        assert mod._parse_reads_list("reads: [C:/a.md, C:/b.py]") == ["C:/a.md", "C:/b.py"]

    def test_parse_reads_list_missing_returns_none(self):
        assert mod._parse_reads_list("no reads here") is None

    def test_audit_output_reads_violation_subpath_casefold(self):
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "report.md"
            out.write_text("reads:\n  - C:/Secret/inner.md\n", encoding="utf-8")
            status, violated = mod._audit_output_reads(out, ["c:/secret"])
            assert status == "violated" and "inner.md" in violated

    def test_audit_output_reads_clean(self):
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "report.md"
            out.write_text("reads:\n  - C:/allowed/x.md\n", encoding="utf-8")
            status, _ = mod._audit_output_reads(out, ["C:/secret"])
            assert status == "clean"


# ─── 白名单 fail-closed ──────────────────────────────────────────────
class TestAllowlistFailClosed:
    def test_rejects_not_list(self):
        with tempfile.TemporaryDirectory() as td:
            bad = Path(td) / "bad.json"
            bad.write_text('{"a": 1}', encoding="utf-8")
            with pytest.raises(RuntimeError):
                mod._load_allowed_models(bad)

    def test_rejects_empty_or_malformed_entries(self):
        with tempfile.TemporaryDirectory() as td:
            bad = Path(td) / "bad.json"
            for content in ("[]", '["nodash"]', '["a/b", "a/b"]', '[" a/b"]', '["a/b/c"]'):
                bad.write_text(content, encoding="utf-8")
                with pytest.raises(RuntimeError):
                    mod._load_allowed_models(bad)

    def test_repo_config_loads(self):
        assert mod.ALLOWED_MODELS
        assert all(len(m.split("/")) == 2 for m in mod.ALLOWED_MODELS)

    def test_validate_model_allowed_rejects_unknown(self):
        with pytest.raises(ValueError, match="PISR allowlist"):
            mod._validate_model_allowed("unknown/model")


# ─── 快照 / 碰撞 / 改名检测 ──────────────────────────────────────────
class TestSnapshotAndCollision:
    def test_snapshot_dir_captures_files(self):
        with tempfile.TemporaryDirectory() as td:
            (Path(td) / "a.md").write_text("x", encoding="utf-8")
            snap = mod._snapshot_dir(Path(td))
            assert "a.md" in snap

    def test_detect_name_mismatch_finds_new_same_suffix(self):
        with tempfile.TemporaryDirectory() as td:
            before = {}
            (Path(td) / "final-review-renamed.md").write_text("y", encoding="utf-8")
            mismatch = mod._detect_name_mismatch(Path(td) / "final.md", before, "final")
            assert "final-review-renamed.md" in mismatch

    def test_collision_report_flags_overwrite(self, capsys):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            victim = d / "victim.md"
            victim.write_text("old", encoding="utf-8")
            before = mod._snapshot_dir(d)
            victim.write_text("OVERWRITTEN-LONGER", encoding="utf-8")
            with tempfile.TemporaryDirectory() as td2:
                mod.DISPATCH_LOG = Path(td2) / "log.jsonl"
                assert mod._collision_report(d, before, {"expected.md"}, None) is True
            assert "非预期覆盖" in capsys.readouterr().err


# ─── summary 聚合 ────────────────────────────────────────────────────
class TestSummary:
    def _rows(self, td):
        rows = [
            {"ts": "2026-08-26T10:00:00+08:00", "role": "executor", "outcome": "success",
             "wall_min": 2.0, "usage_total_tokens": 1000, "cost_estimate": 0.0},
            {"ts": "2026-08-26T11:00:00+08:00", "role": "executor", "outcome": "error",
             "wall_min": 1.0, "usage_total_tokens": 200, "cost_estimate": 0.0},
            {"ts": "2026-08-26T12:00:00+08:00", "role": "unknown-old", "outcome": "success",
             "wall_min": 0.5, "usage_total_tokens": 50, "cost_estimate": 0.0},
        ]
        log = Path(td) / "dispatch-log.jsonl"
        log.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows), encoding="utf-8")
        return log

    def test_group_by_role_with_legacy_bucket(self, capsys):
        with tempfile.TemporaryDirectory() as td:
            old = mod.DISPATCH_LOG
            mod.DISPATCH_LOG = self._rows(td)
            try:
                import argparse
                rc = mod.cmd_summary(argparse.Namespace(group_by="role", since="",
                                                        format="table"))
                assert rc == 0
                out = capsys.readouterr().out
                assert "executor" in out
                assert mod.ROLE_LEGACY in out
            finally:
                mod.DISPATCH_LOG = old

    def test_since_filter(self, capsys):
        with tempfile.TemporaryDirectory() as td:
            old = mod.DISPATCH_LOG
            mod.DISPATCH_LOG = self._rows(td)
            try:
                import argparse
                mod.cmd_summary(argparse.Namespace(group_by="role", since="2026-08-26T11:30:00",
                                                   format="json"))
                out = capsys.readouterr().out
                data = json.loads(out)
                assert "executor" not in data  # 两条 executor 均早于 cutoff
            finally:
                mod.DISPATCH_LOG = old


# ─── worker 启动边界（prompt 副本注入与上限）─────────────────────────
class TestSpawnWorkerPromptInjection:
    def test_output_placeholders_substituted(self):
        with tempfile.TemporaryDirectory() as td:
            src = Path(td) / "p.txt"
            src.write_text("写到 {{OUTPUT_PATH}}（名 {{OUTPUT_NAME}}，目录 {{OUTPUT_DIR}}）",
                           encoding="utf-8")
            w = {"prompt_file": str(src), "model": "prov/model-x", "label": "L",
                 "output": Path(td) / "out" / "L.md", "work_dir": Path(td) / "wd",
                 "tools": None, "thinking": "", "forbid_paths": []}
            with mock.patch.object(mod.subprocess, "Popen") as fake_popen:
                fake_popen.return_value.pid = 4242
                mod._spawn_worker(w)
            injected = (Path(td) / "wd" / "prompt.txt").read_text(encoding="utf-8")
            assert "{{OUTPUT_PATH}}" not in injected
            assert "L.md" in injected
            argv = fake_popen.call_args[0][0]
            assert argv[-1].endswith("/prompt.txt") and argv[-1].startswith("@")

    def test_forbid_block_appended_to_copy_not_source(self):
        with tempfile.TemporaryDirectory() as td:
            src = Path(td) / "p.txt"
            src.write_text("正文", encoding="utf-8")
            w = {"prompt_file": str(src), "model": "prov/model-x", "label": "L",
                 "output": Path(td) / "L.md", "work_dir": Path(td) / "wd",
                 "tools": None, "thinking": "", "forbid_paths": ["C:/secret"]}
            with mock.patch.object(mod.subprocess, "Popen") as fake_popen:
                fake_popen.return_value.pid = 1
                mod._spawn_worker(w)
            assert "禁止读取" in (Path(td) / "wd" / "prompt.txt").read_text(encoding="utf-8")
            assert "禁止读取" not in src.read_text(encoding="utf-8")

    def test_prompt_size_cap_fail_closed(self):
        with tempfile.TemporaryDirectory() as td:
            src = Path(td) / "p.txt"
            src.write_text("x" * (mod.PROMPT_ARG_MAX_BYTES + 1), encoding="utf-8")
            w = {"prompt_file": str(src), "model": "prov/model-x", "label": "L",
                 "output": Path(td) / "L.md", "work_dir": Path(td) / "wd",
                 "tools": None, "thinking": "", "forbid_paths": []}
            with mock.patch.object(mod.subprocess, "Popen") as fake_popen:
                with pytest.raises(ValueError, match="上限"):
                    mod._spawn_worker(w)
            fake_popen.assert_not_called()
