"""确定性步骤运行器执行引擎测试
（plan 20260810-deterministic-run-spec 阶段 3）。

覆盖：hook / assert 步骤执行、journal 两段式、pause 往返、resume 语义、
started-无-completed 的歧义停机、spec 变更检测、scope 编号派生、workdir 独占、
dispatch 步骤未接线时 fail-closed。

全部离线，不触发任何模型调用。
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

os.environ["PISR_DISABLE_MODEL_CALLS"] = "1"

ROOT = Path(__file__).resolve().parents[1]
IMPL = ROOT / "scripts" / "pisr_run_spec.py"
DISPATCH = ROOT / "scripts" / "pisr_dispatch.py"

_SPEC = importlib.util.spec_from_file_location("pisr_run_engine_test", IMPL)
rs = importlib.util.module_from_spec(_SPEC)
assert _SPEC and _SPEC.loader
sys.modules[_SPEC.name] = rs
_SPEC.loader.exec_module(rs)

pytest.importorskip("yaml", reason="spec 引擎依赖 PyYAML")

PY = sys.executable.replace("\\", "/")


class Scene:
    """一次性运行现场：spec 文件 + workdir 都在临时目录内。"""

    def __init__(self, td: Path, spec_text: str) -> None:
        self.td = td
        self.wd = td / "wd"
        self.spec = td / "spec.yaml"
        self.spec.write_text(spec_text.replace("__WD__", self.wd.as_posix())
                                      .replace("__PY__", PY), encoding="utf-8")

    def run(self, **kw) -> int:
        return rs.execute_file(self.spec, **kw)

    def journal(self) -> list[dict]:
        p = self.wd / rs.JOURNAL_NAME
        if not p.is_file():
            return []
        return [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]

    def events(self, kind: str) -> list[dict]:
        return [r for r in self.journal() if r["event"] == kind]


HOOK_OK = """\
version: 1
run: {id: t, workdir: __WD__}
steps:
  - {id: a, type: hook, run: [__PY__, -c, "print('ok')"]}
"""


# ─── hook / assert 执行 ──────────────────────────────────────────────
class TestStepExecution:
    def test_hook_success(self):
        with tempfile.TemporaryDirectory() as t:
            s = Scene(Path(t), HOOK_OK)
            assert s.run() == rs.EXIT_OK
            assert [r["step"] for r in s.events("step-completed")] == ["a"]

    def test_hook_nonzero_exit_fails_step(self):
        with tempfile.TemporaryDirectory() as t:
            s = Scene(Path(t), """\
version: 1
run: {id: t, workdir: __WD__}
steps:
  - {id: a, type: hook, run: [__PY__, -c, "raise SystemExit(3)"]}
""")
            assert s.run() == rs.EXIT_STEP_FAILED
            done = s.events("step-completed")[0]
            assert done["status"] == "failed"
            assert s.events("run-finished")[0]["status"] == "failed"

    def test_hook_expect_mismatch_fails(self):
        with tempfile.TemporaryDirectory() as t:
            s = Scene(Path(t), """\
version: 1
run: {id: t, workdir: __WD__}
steps:
  - {id: a, type: hook, run: [__PY__, -c, "print('nope')"], expect: '^YES$'}
""")
            assert s.run() == rs.EXIT_STEP_FAILED

    def test_hook_capture_flows_to_next_step(self):
        """命名组捕获进 run context，后续步骤可引用——记账参数由此确定性传递。"""
        with tempfile.TemporaryDirectory() as t:
            s = Scene(Path(t), """\
version: 1
run: {id: t, workdir: __WD__}
steps:
  - id: a
    type: hook
    run: [__PY__, -c, "print('PROCEED:zz9')"]
    pre:
      - run: [__PY__, -c, "print('RID:abc123')"]
        expect: '^RID:(?P<rid>\\w+)$'
    next: b
  - {id: b, type: hook, run: [__PY__, -c, "import sys; print(sys.argv[1])", "{{steps.a.pre[0].rid}}"]}
""")
            # pre/post 是 dispatch 步骤的内联 hook；hook 步骤上声明 pre 仅用于校验期引用解析，
            # 故此处只断言 spec 合法且能跑到底。
            assert s.run() == rs.EXIT_OK

    def test_standalone_hook_capture_reaches_the_next_step_argv(self):
        """独立 hook 的捕获必须真的进入后续步骤的 argv，而不只是进 journal。

        `use` 只在收到 `abc123` 时退出 0，否则退出 7 —— run 成功即证明值确实传到了。
        """
        with tempfile.TemporaryDirectory() as t:
            s = Scene(Path(t), """\
version: 1
run: {id: t, workdir: __WD__}
steps:
  - id: emit
    type: hook
    run: [__PY__, -c, "print('V:abc123')"]
    expect: '^V:(?P<token>\\w+)$'
    next: use
  - {id: use, type: hook, run: [__PY__, -c, "import sys; sys.exit(0 if sys.argv[1]=='abc123' else 7)", "{{steps.emit.capture.token}}"]}
""")
            assert s.run() == rs.EXIT_OK

    def test_assert_file_exists_and_matches(self):
        with tempfile.TemporaryDirectory() as t:
            td = Path(t)
            (td / "wd").mkdir()
            (td / "wd" / "art.md").write_text("verdict: ok\n", encoding="utf-8")
            s = Scene(td, """\
version: 1
run: {id: t, workdir: __WD__}
steps:
  - id: a
    type: assert
    assert: {file_exists: "{{run.workdir}}/art.md", non_empty: true, matches: 'verdict'}
""")
            assert s.run() == rs.EXIT_OK

    def test_assert_missing_file_fails(self):
        with tempfile.TemporaryDirectory() as t:
            s = Scene(Path(t), """\
version: 1
run: {id: t, workdir: __WD__}
steps:
  - {id: a, type: assert, assert: {file_exists: "{{run.workdir}}/nope.md"}}
""")
            assert s.run() == rs.EXIT_STEP_FAILED

    def test_assert_empty_file_fails_non_empty(self):
        with tempfile.TemporaryDirectory() as t:
            td = Path(t)
            (td / "wd").mkdir()
            (td / "wd" / "art.md").write_text("", encoding="utf-8")
            s = Scene(td, """\
version: 1
run: {id: t, workdir: __WD__}
steps:
  - {id: a, type: assert, assert: {file_exists: "{{run.workdir}}/art.md", non_empty: true}}
""")
            assert s.run() == rs.EXIT_STEP_FAILED


# ─── 路由：键来自产物的确定性解析 ────────────────────────────────────
class TestRouting:
    ROUTE_SPEC = """\
version: 1
run: {id: t, workdir: __WD__}
steps:
  - id: gate
    type: hook
    run: [__PY__, -c, "print('```yaml'); print('verdict: %s'); print('```')"]
    extract: {verdict: "yaml:verdict"}
    route:
      ok: fin
      "*": ask
  - {id: ask, type: pause, question: q, options: [fin, abort]}
  - {id: fin, type: hook, run: [__PY__, -c, "print('fin')"]}
"""

    def test_matched_route_taken(self):
        with tempfile.TemporaryDirectory() as t:
            s = Scene(Path(t), self.ROUTE_SPEC % "ok")
            assert s.run() == rs.EXIT_OK
            gate = [r for r in s.events("step-completed") if r["step"] == "gate"][0]
            assert gate["route_key"] == "ok"
            assert gate["next"] == "fin"

    def test_unmatched_route_goes_to_pause(self):
        """未预期的取值是判断分歧 → fail-open 交回 agent，而不是让 runner 猜。"""
        with tempfile.TemporaryDirectory() as t:
            s = Scene(Path(t), self.ROUTE_SPEC % "surprise")
            assert s.run() == rs.EXIT_PAUSED
            gate = [r for r in s.events("step-completed") if r["step"] == "gate"][0]
            assert gate["route_key"] == "surprise"
            assert gate["route_matched"] == "*"
            assert gate["next"] == "ask"

    def test_yaml_missing_route_key_fails_closed(self):
        with tempfile.TemporaryDirectory() as t:
            s = Scene(Path(t), """\
version: 1
run: {id: t, workdir: __WD__}
steps:
  - id: gate
    type: hook
    run: [__PY__, -c, "print('```yaml'); print('other: value'); print('```')"]
    extract: {verdict: "yaml:verdict"}
    route: {ok: fin, "*": ask}
  - {id: ask, type: pause, question: q, options: [fin, abort]}
  - {id: fin, type: hook, run: [__PY__, -c, "print('fin')"]}
""")
            assert s.run() == rs.EXIT_STEP_FAILED
            gate = s.events("step-completed")[0]
            assert gate["status"] == "failed"
            assert "抽取失败" in gate["detail"]
            assert not (s.wd / rs.PAUSE_REQUEST_NAME).exists()

    def test_yaml_empty_route_value_fails_closed(self):
        with tempfile.TemporaryDirectory() as t:
            s = Scene(Path(t), """\
version: 1
run: {id: t, workdir: __WD__}
steps:
  - id: gate
    type: hook
    run: [__PY__, -c, "print('```yaml'); print('verdict:'); print('```')"]
    extract: {verdict: "yaml:verdict"}
    route: {ok: fin, "*": ask}
  - {id: ask, type: pause, question: q, options: [fin, abort]}
  - {id: fin, type: hook, run: [__PY__, -c, "print('fin')"]}
""")
            assert s.run() == rs.EXIT_STEP_FAILED
            assert s.events("step-completed")[0]["status"] == "failed"
            assert not (s.wd / rs.PAUSE_REQUEST_NAME).exists()

    def test_only_first_yaml_block_is_eligible_for_extraction(self):
        with tempfile.TemporaryDirectory() as t:
            s = Scene(Path(t), """\
version: 1
run: {id: t, workdir: __WD__}
steps:
  - id: gate
    type: hook
    run: [__PY__, -c, "print('```yaml'); print('other: value'); print('```'); print('```yaml'); print('verdict: ok'); print('```')"]
    extract: {verdict: "yaml:verdict"}
    route: {ok: fin, "*": ask}
  - {id: ask, type: pause, question: q, options: [fin, abort]}
  - {id: fin, type: hook, run: [__PY__, -c, "print('fin')"]}
""")
            assert s.run() == rs.EXIT_STEP_FAILED
            assert s.events("step-completed")[0]["status"] == "failed"
            assert not (s.wd / rs.PAUSE_REQUEST_NAME).exists()

    def test_regex_no_match_fails_closed(self):
        with tempfile.TemporaryDirectory() as t:
            s = Scene(Path(t), """\
version: 1
run: {id: t, workdir: __WD__}
steps:
  - id: gate
    type: hook
    run: [__PY__, -c, "print('no verdict here')"]
    extract: {verdict: 'regex:verdict=(\\w+)'}
    route: {ok: fin, "*": ask}
  - {id: ask, type: pause, question: q, options: [fin, abort]}
  - {id: fin, type: hook, run: [__PY__, -c, "print('fin')"]}
""")
            assert s.run() == rs.EXIT_STEP_FAILED
            gate = s.events("step-completed")[0]
            assert gate["status"] == "failed"
            assert "抽取失败" in gate["detail"]
            assert not (s.wd / rs.PAUSE_REQUEST_NAME).exists()

    def test_regex_empty_capture_fails_closed(self):
        with tempfile.TemporaryDirectory() as t:
            s = Scene(Path(t), """\
version: 1
run: {id: t, workdir: __WD__}
steps:
  - id: gate
    type: hook
    run: [__PY__, -c, "import sys; sys.stdout.write('verdict=')"]
    extract: {verdict: 'regex:verdict=(.*)'}
    route: {ok: fin, "*": ask}
  - {id: ask, type: pause, question: q, options: [fin, abort]}
  - {id: fin, type: hook, run: [__PY__, -c, "print('fin')"]}
""")
            assert s.run() == rs.EXIT_STEP_FAILED
            assert s.events("step-completed")[0]["status"] == "failed"
            assert not (s.wd / rs.PAUSE_REQUEST_NAME).exists()

    def test_exitcode_extractor(self):
        with tempfile.TemporaryDirectory() as t:
            s = Scene(Path(t), """\
version: 1
run: {id: t, workdir: __WD__}
steps:
  - id: a
    type: hook
    run: [__PY__, -c, "print('x')"]
    extract: {rc: "exitcode"}
    route: {"0": fin, "*": ask}
  - {id: ask, type: pause, question: q, options: [fin, abort]}
  - {id: fin, type: hook, run: [__PY__, -c, "print('fin')"]}
""")
            assert s.run() == rs.EXIT_OK


# ─── journal 两段式 ──────────────────────────────────────────────────
class TestJournal:
    def test_started_precedes_completed_for_every_step(self):
        with tempfile.TemporaryDirectory() as t:
            s = Scene(Path(t), """\
version: 1
run: {id: t, workdir: __WD__}
steps:
  - {id: a, type: hook, run: [__PY__, -c, "print(1)"], next: b}
  - {id: b, type: hook, run: [__PY__, -c, "print(2)"]}
""")
            assert s.run() == rs.EXIT_OK
            seq = [(r["event"], r.get("step")) for r in s.journal()]
            assert seq == [
                ("run-started", None),
                ("step-started", "a"), ("step-completed", "a"),
                ("step-started", "b"), ("step-completed", "b"),
                ("run-finished", None),
            ]

    def test_journal_records_spec_sha(self):
        with tempfile.TemporaryDirectory() as t:
            s = Scene(Path(t), HOOK_OK)
            s.run()
            assert len(s.events("run-started")[0]["spec_sha256"]) == 64


# ─── pause / resume ─────────────────────────────────────────────────
PAUSE_SPEC = """\
version: 1
run: {id: t, workdir: __WD__}
steps:
  - id: gate
    type: hook
    run: [__PY__, -c, "print('```yaml'); print('verdict: surprise'); print('```')"]
    extract: {verdict: "yaml:verdict"}
    route: {"ok": fin, "*": ask}
  - {id: ask, type: pause, question: 请裁决, options: [fin, abort]}
  - {id: fin, type: hook, run: [__PY__, -c, "print('fin')"]}
"""


class TestPauseResume:
    def test_pause_writes_request_and_exits_10(self):
        with tempfile.TemporaryDirectory() as t:
            s = Scene(Path(t), PAUSE_SPEC)
            assert s.run() == rs.EXIT_PAUSED
            req = json.loads((s.wd / rs.PAUSE_REQUEST_NAME).read_text(encoding="utf-8"))
            assert req["step"] == "ask"
            assert req["options"] == ["fin", "abort"]
            assert sorted(req["reserved_options"]) == ["abort", "retry"]
            assert "--answer ask=" in req["resume_hint"]
            assert s.events("step-paused")[0]["step"] == "ask"

    def test_resume_without_answer_stays_paused(self):
        with tempfile.TemporaryDirectory() as t:
            s = Scene(Path(t), PAUSE_SPEC)
            assert s.run() == rs.EXIT_PAUSED
            with pytest.raises(rs.RunHalt) as e:
                s.run(resume=True)
            assert e.value.code == rs.EXIT_PAUSED

    def test_resume_with_answer_completes(self):
        with tempfile.TemporaryDirectory() as t:
            s = Scene(Path(t), PAUSE_SPEC)
            assert s.run() == rs.EXIT_PAUSED
            assert s.run(resume=True, answers={"ask": "fin"}) == rs.EXIT_OK
            assert not (s.wd / rs.PAUSE_REQUEST_NAME).exists(), "pause 请求应在结案后清除"
            assert s.events("run-finished")[-1]["status"] == "ok"

    def test_answer_abort_terminates(self):
        with tempfile.TemporaryDirectory() as t:
            s = Scene(Path(t), PAUSE_SPEC)
            s.run()
            assert s.run(resume=True, answers={"ask": "abort"}) == rs.EXIT_STEP_FAILED
            assert s.events("run-finished")[-1]["status"] == "aborted"

    def test_invalid_answer_rejected(self):
        with tempfile.TemporaryDirectory() as t:
            s = Scene(Path(t), PAUSE_SPEC)
            s.run()
            with pytest.raises(rs.RunHalt) as e:
                s.run(resume=True, answers={"ask": "not-an-option"})
            assert e.value.code == rs.EXIT_SPEC_INVALID


# ─── resume 的 fail-closed 语义（D5）────────────────────────────────
class TestResumeSafety:
    def test_rerun_without_resume_refuses_to_clobber(self):
        with tempfile.TemporaryDirectory() as t:
            s = Scene(Path(t), HOOK_OK)
            assert s.run() == rs.EXIT_OK
            with pytest.raises(rs.RunHalt) as e:
                s.run()
            assert e.value.code == rs.EXIT_RESUME_AMBIGUOUS
            assert "--resume" in e.value.message

    def test_started_without_completed_halts_no_autorerun(self):
        """该步可能已消耗真实模型调用与预算 reservation，自动重跑会双花。"""
        with tempfile.TemporaryDirectory() as t:
            s = Scene(Path(t), HOOK_OK)
            jp = s.wd
            jp.mkdir(parents=True, exist_ok=True)
            (jp / rs.JOURNAL_NAME).write_text(
                json.dumps({"event": "run-started", "seq": 1, "ts": "x",
                            "spec_sha256": rs._sha256_bytes(s.spec.read_bytes())},
                           ensure_ascii=False) + "\n"
                + json.dumps({"event": "step-started", "seq": 2, "ts": "x", "step": "a"},
                             ensure_ascii=False) + "\n",
                encoding="utf-8")
            with pytest.raises(rs.RunHalt) as e:
                s.run(resume=True)
            assert e.value.code == rs.EXIT_RESUME_AMBIGUOUS
            assert "禁止自动重跑" in e.value.message

    def test_spec_change_between_runs_halts(self):
        with tempfile.TemporaryDirectory() as t:
            s = Scene(Path(t), PAUSE_SPEC)
            assert s.run() == rs.EXIT_PAUSED
            s.spec.write_text(s.spec.read_text(encoding="utf-8") + "\n# 改了一行\n",
                              encoding="utf-8")
            with pytest.raises(rs.RunHalt) as e:
                s.run(resume=True, answers={"ask": "fin"})
            assert e.value.code == rs.EXIT_RESUME_AMBIGUOUS
            assert "sha256" in e.value.message

    def test_corrupt_journal_halts(self):
        with tempfile.TemporaryDirectory() as t:
            s = Scene(Path(t), HOOK_OK)
            s.wd.mkdir(parents=True, exist_ok=True)
            (s.wd / rs.JOURNAL_NAME).write_text("{not json}\n", encoding="utf-8")
            with pytest.raises(rs.RunHalt) as e:
                s.run(resume=True)
            assert e.value.code == rs.EXIT_RESUME_AMBIGUOUS


# ─── scope 编号由 runner 派生（D3）──────────────────────────────────
class TestScopeIndexDerivation:
    def test_index_increments_per_group_key(self):
        """调用方声明"这是某组的第几个"，数字由 runner 算——消灭手填轮次号那类错误。"""
        with tempfile.TemporaryDirectory() as t:
            s = Scene(Path(t), """\
version: 1
run: {id: t, workdir: __WD__}
steps:
  - {id: a, type: hook, scope: g1, run: [__PY__, -c, "import sys; print(sys.argv[1])", "{{scope.g1.next_index}}"], next: b}
  - {id: b, type: hook, scope: g1, run: [__PY__, -c, "import sys; print(sys.argv[1])", "{{scope.g1.next_index}}"], next: c}
  - {id: c, type: hook, scope: g2, run: [__PY__, -c, "import sys; print(sys.argv[1])", "{{scope.g2.next_index}}"]}
""")
            assert s.run() == rs.EXIT_OK
            started = {r["step"]: r for r in s.events("step-started")}
            assert started["a"]["scope_index"] == 1
            assert started["b"]["scope_index"] == 2, "同组应单调递增"
            assert started["c"]["scope_index"] == 1, "不同组各自计数"
            assert started["c"]["scope_key"] == "g2"


# ─── workdir 独占与 dispatch 接线点（D7 / 阶段 5）────────────────────
class TestDispatchWiring:
    DISPATCH_SPEC = """\
version: 1
run: {id: t, workdir: __WD__}
steps:
  - id: d
    type: dispatch
    model: prov-a/model-x
    prompt: p.txt
    output: "%s"
"""

    def _scene(self, td: Path, output: str) -> Scene:
        (td / "p.txt").write_text("prompt", encoding="utf-8")
        return Scene(td, self.DISPATCH_SPEC % output)

    def test_dispatch_without_wiring_fails_closed(self):
        """阶段 5 未接线时必须明确失败，不得假装成功。"""
        with tempfile.TemporaryDirectory() as t:
            s = self._scene(Path(t), "{{run.workdir}}/out.md")
            assert s.run(allowed_models={"prov-a/model-x"}) == rs.EXIT_STEP_FAILED
            assert "阶段 5" in s.events("step-completed")[0]["detail"]

    def test_output_outside_workdir_rejected(self):
        """runner 独占 workdir：产物必须落在其内（D7）。"""
        with tempfile.TemporaryDirectory() as t:
            td = Path(t)
            s = self._scene(td, (td / "elsewhere.md").as_posix())
            called = []
            rc = s.run(allowed_models={"prov-a/model-x"},
                       dispatch_fn=lambda *a: called.append(a) or 0)
            assert rc == rs.EXIT_STEP_FAILED
            assert not called, "越界产物路径必须在派发前就被拒绝"
            assert "workdir" in s.events("step-completed")[0]["detail"]

    def test_injected_dispatch_fn_is_called(self):
        with tempfile.TemporaryDirectory() as t:
            s = self._scene(Path(t), "{{run.workdir}}/out.md")
            seen = {}

            def fake(step, sid, ctx, out_path):
                seen["sid"] = sid
                Path(out_path).parent.mkdir(parents=True, exist_ok=True)
                Path(out_path).write_text("done", encoding="utf-8")
                return 0

            assert s.run(allowed_models={"prov-a/model-x"},
                         dispatch_fn=fake) == rs.EXIT_OK
            assert seen["sid"] == "d"

    def test_dispatch_nonzero_rc_fails_step(self):
        with tempfile.TemporaryDirectory() as t:
            s = self._scene(Path(t), "{{run.workdir}}/out.md")
            rc = s.run(allowed_models={"prov-a/model-x"},
                       dispatch_fn=lambda *a: 2)
            assert rc == rs.EXIT_STEP_FAILED
            assert "rc=2" in s.events("step-completed")[0]["detail"]


# ─── CLI 端到端 ──────────────────────────────────────────────────────
class TestCliRun:
    def _run(self, args: list[str]):
        env = dict(os.environ, PISR_DISABLE_MODEL_CALLS="1", PYTHONIOENCODING="utf-8")
        return subprocess.run([sys.executable, str(DISPATCH), *args],
                              capture_output=True, text=True, encoding="utf-8", env=env)

    def test_cli_run_completes(self):
        with tempfile.TemporaryDirectory() as t:
            s = Scene(Path(t), HOOK_OK)
            r = self._run(["run", "--spec", str(s.spec)])
        assert r.returncode == rs.EXIT_OK, r.stderr
        assert "run 完成" in r.stdout

    def test_cli_pause_exit_10_then_resume(self):
        with tempfile.TemporaryDirectory() as t:
            s = Scene(Path(t), PAUSE_SPEC)
            r1 = self._run(["run", "--spec", str(s.spec)])
            assert r1.returncode == rs.EXIT_PAUSED, r1.stderr
            r2 = self._run(["run", "--spec", str(s.spec), "--resume", "--answer", "ask=fin"])
            assert r2.returncode == rs.EXIT_OK, r2.stderr

    def test_cli_bad_answer_format(self):
        with tempfile.TemporaryDirectory() as t:
            s = Scene(Path(t), PAUSE_SPEC)
            self._run(["run", "--spec", str(s.spec)])
            r = self._run(["run", "--spec", str(s.spec), "--resume", "--answer", "oops"])
        assert r.returncode == 2
