"""确定性步骤运行器 spec 离线校验测试
（plan 20260810-deterministic-run-spec 阶段 2）。

覆盖：合法 spec 通过、坏 spec 逐类 fail-closed、取值器封闭枚举、模板文法边界、
pause 选项成边、启发式警告非阻断、G2 不变量（实现文件不含分组键字面值）、CLI 退出码。

全部离线，不触发任何模型调用、不启动任何进程。
"""
from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path

import pytest

os.environ["PISR_DISABLE_MODEL_CALLS"] = "1"

ROOT = Path(__file__).resolve().parents[1]
IMPL = ROOT / "scripts" / "pisr_run_spec.py"
DISPATCH = ROOT / "scripts" / "pisr_dispatch.py"

_SPEC = importlib.util.spec_from_file_location("pisr_run_spec_test", IMPL)
rs = importlib.util.module_from_spec(_SPEC)
assert _SPEC and _SPEC.loader
sys.modules[_SPEC.name] = rs
_SPEC.loader.exec_module(rs)

yaml = pytest.importorskip("yaml", reason="spec 校验依赖 PyYAML")

ALLOWED = {"prov-a/model-x", "prov-a/model-pro"}


def _write(td: Path, text: str, name: str = "spec.yaml") -> Path:
    p = td / name
    p.write_text(textwrap.dedent(text), encoding="utf-8")
    return p


def _validate(text: str, *, files: dict[str, str] | None = None):
    """在临时目录里校验一段 spec 文本。files 用于铺设 prompt 等引用文件。"""
    with tempfile.TemporaryDirectory() as t:
        td = Path(t)
        for rel, content in (files or {}).items():
            fp = td / rel
            fp.parent.mkdir(parents=True, exist_ok=True)
            fp.write_text(content, encoding="utf-8")
        return rs.validate_file(_write(td, text), allowed_models=ALLOWED)


HEAD = """\
    version: 1
    run: {id: t, workdir: wd}
    steps:
"""


# ─── 合法 spec ───────────────────────────────────────────────────────
class TestValidSpec:
    def test_minimal_spec_passes(self):
        s = _validate(HEAD + "      - {id: a, type: hook, run: [echo, hi]}\n")
        assert s["step_count"] == 1
        assert s["entry"] == "a"
        assert s["warnings"] == []

    def test_dispatch_with_route_and_hooks(self):
        text = HEAD + """\
      - id: r1
        type: dispatch
        scope: some-group
        model: prov-a/model-pro
        prompt: p.txt
        output: "{{run.workdir}}/out.md"
        pre:
          - run: [echo, reserve]
            expect: '^PROCEED:(?P<rid>\\w+)$'
        post:
          - run: [echo, settle, "{{steps.r1.pre[0].rid}}"]
        extract:
          verdict: "yaml:verdict"
        route:
          ok: fin
          "*": ask
      - {id: ask, type: pause, question: q, options: [fin, abort]}
      - {id: fin, type: assert, assert: {file_exists: "{{run.workdir}}/out.md"}}
"""
        s = _validate(text, files={"p.txt": "prompt"})
        assert s["step_count"] == 3
        assert s["scope_keys"] == ["some-group"]
        assert "{{steps.r1.pre[0].rid}}" in s["var_refs"] or \
               "steps.r1.pre[0].rid" in s["var_refs"]

    def test_standalone_hook_capture_is_inside_the_grammar(self):
        """独立 `hook` 步骤自身的 expect 捕获必须可被后续步骤引用。

        修复前：捕获确实存进了 run context（`ctx["captures"][sid]["self"]`），
        但模板文法里没有任何形式能取出来，而 `refs/run-spec.md` 写着
        「`expect` 的命名组捕获进 run context，供后续步骤引用」——
        文档承诺了实现不提供的能力。converge 的终局链路正需要这条通道
        （`record-user-message` 的 event_id 要成为终局决策的 `--source-ref`）。
        """
        s = _validate(HEAD + """\
      - {id: a, type: hook, run: [echo], expect: '^V:(?P<token>\\w+)$', next: b}
      - {id: b, type: hook, run: [echo, "{{steps.a.capture.token}}"]}
""")
        assert any("steps.a.capture.token" in r for r in s["var_refs"])

    def test_capture_form_is_unambiguous_against_dotted_step_ids(self):
        """封闭文法新增形式不得与既有形式产生二义性（2026-08-10 独立评审 B2 的要求 a）。

        步骤 id 允许含 `.`，所以 `steps.a.capture.token` 原则上可能被读成
        「步骤 `a.capture` 的某个东西」。这里同时存在 `a` 与 `a.capture` 两个步骤，
        断言两条引用各自解析到确定且不同的目标。
        """
        s = _validate(HEAD + """\
      - {id: a, type: hook, run: [echo], expect: '^A:(?P<token>\\w+)$', next: a.capture}
      - {id: a.capture, type: hook, run: [echo], expect: '^B:(?P<token>\\w+)$', next: b}
      - {id: b, type: hook, run: [echo, "{{steps.a.capture.token}}", "{{steps.a.capture.capture.token}}"]}
""")
        refs = " ".join(s["var_refs"])
        assert "steps.a.capture.token" in refs
        assert "steps.a.capture.capture.token" in refs
        # 运行期两条引用取到各自步骤的捕获，互不串味。
        ctx = {"captures": {"a": {rs.SELF_CAPTURE_KEY: {"token": "from-a"}},
                            "a.capture": {rs.SELF_CAPTURE_KEY: {"token": "from-a-capture"}}}}
        assert rs._resolve_value("steps.a.capture.token", ctx) == "from-a"
        assert rs._resolve_value("steps.a.capture.capture.token", ctx) == "from-a-capture"

    def test_summary_lists_route_graph_and_refs(self):
        s = _validate("""\
    version: 1
    run: {id: t, workdir: wd}
    vars: {x: "1"}
    steps:
      - {id: a, type: hook, run: [echo, "{{vars.x}}"], next: b}
      - {id: b, type: hook, run: [echo]}
""")
        assert s["route_graph"]["a"] == ["b"]
        assert s["route_graph"]["b"] == []
        assert "vars.x" in s["var_refs"]
        assert s["var_refs"]["vars.x"] == ["a"]
        assert rs.format_summary(s)


# ─── 坏 spec 逐类 fail-closed ────────────────────────────────────────
class TestFailClosed:
    """每条都必须以**特定 code** 拒绝，而不是笼统失败。"""

    def _expect(self, text: str, code: str, files=None):
        with pytest.raises(rs.SpecError) as e:
            _validate(text, files=files)
        assert e.value.code == code, f"期望 {code}，实得 {e.value.code}: {e.value}"

    def test_route_without_fallback(self):
        self._expect(HEAD + """\
      - {id: a, type: hook, run: [echo], extract: {v: "yaml:verdict"}, route: {ok: b}}
      - {id: b, type: pause, question: q, options: [abort]}
""", "route-no-fallback")

    def test_fallback_not_pause(self):
        self._expect(HEAD + """\
      - {id: a, type: hook, run: [echo], extract: {v: "yaml:verdict"}, route: {"*": b}}
      - {id: b, type: hook, run: [echo]}
""", "route-fallback-not-pause")

    def test_route_target_missing(self):
        self._expect(HEAD + """\
      - {id: a, type: hook, run: [echo], extract: {v: "yaml:verdict"}, route: {ok: zzz, "*": b}}
      - {id: b, type: pause, question: q, options: [abort]}
""", "route-target-missing")

    def test_route_without_extract(self):
        self._expect(HEAD + """\
      - {id: a, type: hook, run: [echo], route: {"*": b}}
      - {id: b, type: pause, question: q, options: [abort]}
""", "route-without-extract")

    def test_route_ambiguous_multi_extract(self):
        self._expect(HEAD + """\
      - id: a
        type: hook
        run: [echo]
        extract: {v: "yaml:verdict", w: "exitcode"}
        route: {"*": b}
      - {id: b, type: pause, question: q, options: [abort]}
""", "route-ambiguous")

    def test_graph_cycle(self):
        self._expect(HEAD + """\
      - {id: a, type: hook, run: [echo], next: b}
      - {id: b, type: hook, run: [echo], next: a}
""", "graph-cycle")

    def test_unreachable_step(self):
        self._expect(HEAD + """\
      - {id: a, type: hook, run: [echo]}
      - {id: b, type: hook, run: [echo]}
""", "graph-unreachable")

    def test_unknown_extractor(self):
        """取值器是封闭枚举——不能塞入让脚本'判断'的入口。"""
        self._expect(HEAD + """\
      - {id: a, type: hook, run: [echo], extract: {v: "llm:judge"}, route: {"*": b}}
      - {id: b, type: pause, question: q, options: [abort]}
""", "extract-unknown")

    def test_unresolvable_var(self):
        self._expect(HEAD + '      - {id: a, type: hook, run: [echo, "{{vars.nope}}"]}\n',
                     "template-unresolvable")

    def test_template_outside_grammar(self):
        self._expect(HEAD + '      - {id: a, type: hook, run: [echo, "{{os.environ.HOME}}"]}\n',
                     "template-unresolvable")

    def test_hook_capture_group_missing(self):
        self._expect(HEAD + """\
      - {id: a, type: hook, run: [echo], pre: [{run: [x], expect: '^(?P<rid>\\w+)$'}], next: b}
      - {id: b, type: hook, run: [echo, "{{steps.a.pre[0].missing}}"]}
""", "template-unresolvable")

    def test_hook_index_out_of_range(self):
        self._expect(HEAD + """\
      - {id: a, type: hook, run: [echo], pre: [{run: [x], expect: '^(?P<rid>\\w+)$'}], next: b}
      - {id: b, type: hook, run: [echo, "{{steps.a.pre[3].rid}}"]}
""", "template-unresolvable")

    def test_standalone_hook_capture_group_missing(self):
        self._expect(HEAD + """\
      - {id: a, type: hook, run: [echo], expect: '^V:(?P<token>\\w+)$', next: b}
      - {id: b, type: hook, run: [echo, "{{steps.a.capture.nope}}"]}
""", "template-unresolvable")

    def test_standalone_hook_capture_without_expect(self):
        self._expect(HEAD + """\
      - {id: a, type: hook, run: [echo], next: b}
      - {id: b, type: hook, run: [echo, "{{steps.a.capture.token}}"]}
""", "template-unresolvable")

    def test_standalone_hook_capture_on_non_hook_step(self):
        """`capture.<name>` 只对 hook 步骤成立——assert / pause 没有 expect 可捕获。"""
        self._expect(HEAD + """\
      - {id: a, type: assert, assert: {file_exists: x}, next: b}
      - {id: b, type: hook, run: [echo, "{{steps.a.capture.token}}"]}
""", "template-unresolvable")

    def test_unknown_step_type_rejected(self):
        """步骤类型集合封闭：任意代码执行类步骤不得被接受（设计 G1）。"""
        self._expect(HEAD + "      - {id: a, type: userfunc, run: [echo]}\n", "step-type")

    def test_pause_option_unknown(self):
        self._expect(HEAD + "      - {id: a, type: pause, question: q, options: [nosuch]}\n",
                     "pause-option-unknown")

    def test_model_not_in_allowlist(self):
        self._expect(HEAD + """\
      - {id: a, type: dispatch, model: gpt-4o, prompt: p.txt, output: o}
""", "dispatch-model", files={"p.txt": "x"})

    def test_prompt_file_missing(self):
        self._expect(HEAD + """\
      - {id: a, type: dispatch, model: prov-a/model-pro, prompt: nope.txt, output: o}
""", "dispatch-prompt-missing")

    def test_duplicate_step_id(self):
        self._expect(HEAD + """\
      - {id: a, type: hook, run: [echo]}
      - {id: a, type: hook, run: [echo]}
""", "step-id-duplicate")

    def test_bad_version(self):
        self._expect("version: 99\nrun: {id: t, workdir: wd}\nsteps:\n"
                     "  - {id: a, type: hook, run: [echo]}\n", "spec-version")

    def test_empty_steps(self):
        self._expect("version: 1\nrun: {id: t, workdir: wd}\nsteps: []\n", "steps-empty")

    def test_bad_expect_regex(self):
        self._expect(HEAD + "      - {id: a, type: hook, run: [echo], expect: '(['}\n",
                     "hook-expect-regex")

    def test_assert_unknown_condition(self):
        self._expect(HEAD + "      - {id: a, type: assert, assert: {sha256: deadbeef}}\n",
                     "assert-unknown")


# ─── pause 选项成边（吃狗粮发现的缺陷）────────────────────────────────
class TestPauseOptionsAreEdges:
    """某些步骤可能只经由 pause 的裁决到达；options 不计入边会误判其不可达。"""

    def test_step_reachable_only_via_pause_option(self):
        text = HEAD + """\
      - {id: a, type: hook, run: [echo], extract: {v: "exitcode"}, route: {"*": ask}}
      - {id: ask, type: pause, question: q, options: [rescue, abort]}
      - {id: rescue, type: hook, run: [echo]}
"""
        s = _validate(text)
        assert "rescue" in s["route_graph"]["ask"], "pause 选项未计入出边"
        assert s["step_count"] == 3

    def test_reserved_options_are_not_edges(self):
        text = HEAD + """\
      - {id: a, type: hook, run: [echo], extract: {v: "exitcode"}, route: {"*": ask}}
      - {id: ask, type: pause, question: q, options: [abort, retry]}
"""
        s = _validate(text)
        assert s["route_graph"]["ask"] == [], "保留字不应构成图的边"

    def test_retry_is_reserved(self):
        assert "retry" in rs.PAUSE_RESERVED_OPTIONS
        assert "abort" in rs.PAUSE_RESERVED_OPTIONS


# ─── 启发式警告：提示而非阻断 ────────────────────────────────────────
class TestHeuristicWarning:
    def test_complex_regex_warns_but_passes(self):
        text = HEAD + """\
      - id: a
        type: hook
        run: [echo]
        extract:
          v: 'regex:(?P<a>x)(?P<b>y)(?P<c>z)(?P<d>w)'
        route: {"*": b}
      - {id: b, type: pause, question: q, options: [abort]}
"""
        s = _validate(text)
        assert s["warnings"], "复杂 regex 应产生启发式提示"
        assert "启发式" in s["warnings"][0]

    def test_simple_regex_no_warning(self):
        text = HEAD + """\
      - {id: a, type: hook, run: [echo], extract: {v: 'regex:(?P<v>ok)'}, route: {"*": b}}
      - {id: b, type: pause, question: q, options: [abort]}
"""
        assert _validate(text)["warnings"] == []


# ─── G2 不变量：实现文件不得出现分组键字面值 ─────────────────────────
class TestScopeOpacityInvariant:
    """runner 只按分组键计数，不理解其含义。

    含义归 spec 作者。若实现里出现具体取值（特殊大小写处理、与角色隐式关联等），
    就是语义从文档向代码渗漏的征兆——把边界从「靠自觉」变成「靠测试」。
    """

    #  这些字面值来自 converge 的记账语义，runner 不应认识它们。
    #  （本测试文件出现它们是必要的——被检查的是实现文件，不是本文件。）
    FORBIDDEN = ("outer", "blind", "ultraverge")

    def test_impl_contains_no_scope_literals(self):
        src = IMPL.read_text(encoding="utf-8")
        hits = [w for w in self.FORBIDDEN if w in src]
        assert not hits, (
            f"{IMPL.name} 出现了分组键字面值 {hits}——runner 必须把 scope 当作不透明字符串。"
        )

    def test_scope_key_is_opaque_any_value_accepted(self):
        for key in ("anything", "x-y-z", "任意中文键"):
            text = HEAD + f"""\
      - {{id: a, type: hook, run: [echo, "{{{{scope.{key}.next_index}}}}"], scope: {key}}}
"""
            s = _validate(text)
            assert s["scope_keys"] == [key]


# ─── CLI ─────────────────────────────────────────────────────────────
class TestCli:
    def _run(self, args: list[str]):
        env = dict(os.environ, PISR_DISABLE_MODEL_CALLS="1", PYTHONIOENCODING="utf-8")
        return subprocess.run([sys.executable, str(DISPATCH), *args],
                              capture_output=True, text=True, encoding="utf-8", env=env)

    def test_validate_ok_exit_zero(self):
        with tempfile.TemporaryDirectory() as t:
            p = _write(Path(t), HEAD + "      - {id: a, type: hook, run: [echo, hi]}\n")
            r = self._run(["run", "--spec", str(p), "--validate"])
        assert r.returncode == 0, r.stderr
        assert "校验通过" in r.stdout

    def test_validate_bad_exit_one(self):
        with tempfile.TemporaryDirectory() as t:
            p = _write(Path(t), HEAD + "      - {id: a, type: userfunc, run: [echo]}\n")
            r = self._run(["run", "--spec", str(p), "--validate"])
        assert r.returncode == 1
        assert "step-type" in r.stderr

    def test_validate_json_format(self):
        import json
        with tempfile.TemporaryDirectory() as t:
            p = _write(Path(t), HEAD + "      - {id: a, type: hook, run: [echo, hi]}\n")
            r = self._run(["run", "--spec", str(p), "--validate", "--format", "json"])
        assert r.returncode == 0, r.stderr
        assert json.loads(r.stdout)["step_count"] == 1

    def test_dispatch_step_is_blocked_by_tripwire(self):
        """阶段 5 接线后，`run` 的 dispatch 步骤走真实派发路径 ——
        因此必须同样受 `PISR_DISABLE_MODEL_CALLS` tripwire 管辖。

        本测试证明：runner 没有绕开模型调用防护另开一条口子。
        """
        with tempfile.TemporaryDirectory() as t:
            td = Path(t)
            (td / "p.txt").write_text("x", encoding="utf-8")
            p = _write(td, """\
    version: 1
    run: {id: t, workdir: %s}
    steps:
      - {id: a, type: dispatch, model: xiaomi/mimo-v2.5, prompt: p.txt, output: "{{run.workdir}}/o.md"}
""" % (td / "wd").as_posix())
            r = self._run(["run", "--spec", str(p)])
        assert r.returncode != 0, r.stdout
        assert "PISR_DISABLE_MODEL_CALLS" in (r.stdout + r.stderr)

    def test_validate_makes_no_model_call(self):
        """--validate 是纯离线的：即便 tripwire 开着也必须正常工作。"""
        with tempfile.TemporaryDirectory() as t:
            p = _write(Path(t), """\
    version: 1
    run: {id: t, workdir: wd}
    steps:
      - {id: a, type: dispatch, model: xiaomi/mimo-v2.5, prompt: p.txt, output: o}
""")
            (Path(t) / "p.txt").write_text("x", encoding="utf-8")
            r = self._run(["run", "--spec", str(p), "--validate"])
        assert r.returncode == 0, r.stderr
