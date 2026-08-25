"""吃狗粮：用 runner 跑一条与真实收敛同构的流程，并做端到端比对
（plan 20260810-deterministic-run-spec 阶段 6）。

本文件的核心不是「正例能跑通」，而是**负向测试**：
故意把两条路由映射交换后重跑，验证端到端比对**会失败**——
证明机械验证确实能捕获语义错误，而不是只在正例上通过。
若负向测试也「通过」，说明比对本身没有鉴别力，那才是真正的问题。

全部离线：用 hook 步骤模拟 reviewer/executor 的产物落盘，不触发任何模型调用。
真实模型调用的端到端另行单独验证（见计划阶段 6 记录）。
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import sys
import tempfile
from pathlib import Path

import pytest

os.environ["PISR_DISABLE_MODEL_CALLS"] = "1"

ROOT = Path(__file__).resolve().parents[1]
IMPL = ROOT / "scripts" / "pisr_run_spec.py"

_S = importlib.util.spec_from_file_location("pisr_run_dogfood_test", IMPL)
rs = importlib.util.module_from_spec(_S)
assert _S and _S.loader
sys.modules[_S.name] = rs
_S.loader.exec_module(rs)

pytest.importorskip("yaml", reason="runner 依赖 PyYAML")

PY = sys.executable.replace("\\", "/")

# 与真实收敛同构：评审 → 按 verdict 路由 → （阻断则）修订 → 复评 → 收口。
# reviewer / executor 由 hook 步骤模拟：写出带 fenced YAML 的产物，
# 供 `yaml:verdict` 取值器解析——与真实 reviewer 报告的结构一致。
SPEC = """\
version: 1
run: {id: dogfood, workdir: __WD__}
vars:
  emit: __EMIT__
steps:
  - id: review1
    type: hook
    scope: review-round
    run: [__PY__, "{{vars.emit}}", "{{run.workdir}}/review-1.md", "__V1__"]
    extract: {verdict: "yaml:verdict"}
    route:
      "可执行": closeout
      "阻断需修复": fix
      "*": ask
  - id: fix
    type: hook
    run: [__PY__, "{{vars.emit}}", "{{run.workdir}}/fix-1.md", "已修订"]
    next: review2
  - id: review2
    type: hook
    scope: review-round
    run: [__PY__, "{{vars.emit}}", "{{run.workdir}}/review-2.md", "可执行"]
    extract: {verdict: "yaml:verdict"}
    route:
      "可执行": closeout
      "*": ask
  - id: ask
    type: pause
    question: verdict 非预期，请裁决
    options: [closeout, abort]
  - id: closeout
    type: assert
    assert:
      file_exists: "{{run.workdir}}/review-1.md"
      non_empty: true
"""

# 取值来源的语义（重要）：
#   hook 步骤     → extract 作用于 **stdout+stderr**
#   dispatch 步骤 → extract 作用于 **产物文件内容**
# 故模拟 reviewer 的 hook 既写产物文件、也把同一份内容打到 stdout —— 这与真实
# CLI 型 reviewer 的行为一致（落盘 + 回显），不是为了迁就实现而造的特例。
EMIT_SRC = '''\
import pathlib, sys
body = "```yaml\\nverdict: " + sys.argv[2] + "\\n```\\n"
p = pathlib.Path(sys.argv[1])
p.parent.mkdir(parents=True, exist_ok=True)
p.write_text(body, encoding="utf-8")
sys.stdout.reconfigure(encoding="utf-8")
print(body)
'''


class Scene:
    def __init__(self, td: Path, first_verdict: str, *, swap_routes: bool = False) -> None:
        self.td = td
        self.wd = td / "wd"
        emit = td / "emit.py"
        emit.write_text(EMIT_SRC, encoding="utf-8")
        text = (SPEC.replace("__WD__", self.wd.as_posix())
                    .replace("__PY__", PY)
                    .replace("__EMIT__", emit.as_posix())
                    .replace("__V1__", first_verdict))
        if swap_routes:
            # 语义错误注入：把 review1 的两条路由映射对调。
            # spec 依旧**完全合法**——`--validate` 查不出来，只有端到端比对能抓。
            text = text.replace(
                '      "可执行": closeout\n      "阻断需修复": fix\n',
                '      "可执行": fix\n      "阻断需修复": closeout\n', 1)
        self.spec = td / "spec.yaml"
        self.spec.write_text(text, encoding="utf-8")

    def run(self, **kw) -> int:
        return rs.execute_file(self.spec, **kw)

    def journal(self) -> list[dict]:
        p = self.wd / rs.JOURNAL_NAME
        return ([json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]
                if p.is_file() else [])

    def step_sequence(self) -> list[str]:
        return [r["step"] for r in self.journal() if r["event"] == "step-completed"]

    def artifacts(self) -> dict[str, str]:
        """产物清单 + 各自 SHA256 —— 端到端比对的客观判据之一。"""
        out = {}
        for f in sorted(self.wd.glob("*.md")):
            out[f.name] = hashlib.sha256(f.read_bytes()).hexdigest()
        return out


# 预期基线：首轮阻断 → 修订 → 复评可执行 → 收口
EXPECTED_SEQUENCE = ["review1", "fix", "review2", "closeout"]
EXPECTED_ARTIFACTS = {"review-1.md", "fix-1.md", "review-2.md"}


class TestDogfoodHappyPath:
    def test_full_convergence_shape_runs(self):
        with tempfile.TemporaryDirectory() as t:
            s = Scene(Path(t), "阻断需修复")
            assert s.run() == rs.EXIT_OK
            assert s.step_sequence() == EXPECTED_SEQUENCE
            assert set(s.artifacts()) == EXPECTED_ARTIFACTS

    def test_first_round_pass_skips_fix(self):
        with tempfile.TemporaryDirectory() as t:
            s = Scene(Path(t), "可执行")
            assert s.run() == rs.EXIT_OK
            assert s.step_sequence() == ["review1", "closeout"]
            assert "fix-1.md" not in s.artifacts()

    def test_scope_index_counts_review_rounds(self):
        """两轮评审属同一分组键 → 编号 1、2 由 runner 派生，调用方不写数字。"""
        with tempfile.TemporaryDirectory() as t:
            s = Scene(Path(t), "阻断需修复")
            s.run()
            started = {r["step"]: r for r in s.journal() if r["event"] == "step-started"}
            assert started["review1"]["scope_index"] == 1
            assert started["review2"]["scope_index"] == 2

    def test_artifacts_sha256_stable_across_reruns(self):
        """SHA256 比对仅对**预期确定性**的产物生效（本例产物无时间戳/会话 ID）。"""
        digests = []
        for _ in range(2):
            with tempfile.TemporaryDirectory() as t:
                s = Scene(Path(t), "阻断需修复")
                s.run()
                digests.append(s.artifacts())
        assert digests[0] == digests[1]


class TestDogfoodNegative:
    """负向测试：故意注入语义错误，验证机械比对**会失败**。

    这是阶段 6 的核心。若这些断言「通过」（即比对没发现差异），
    说明端到端比对没有鉴别力——那时正例全绿也毫无意义。
    """

    def test_swapped_routes_still_pass_static_validation(self):
        """先确认这类错误**逃得过** `--validate`：它是语义错误，不是结构错误。"""
        with tempfile.TemporaryDirectory() as t:
            s = Scene(Path(t), "阻断需修复", swap_routes=True)
            summary = rs.validate_file(s.spec)   # 不抛异常即通过静态校验
            assert summary["step_count"] == 5

    def test_swapped_routes_change_step_sequence(self):
        """路由映射对调 → 执行序列偏离基线 → 端到端比对失败。"""
        with tempfile.TemporaryDirectory() as t:
            s = Scene(Path(t), "阻断需修复", swap_routes=True)
            s.run()
            actual = s.step_sequence()
            assert actual != EXPECTED_SEQUENCE, (
                "路由被对调后执行序列竟与基线一致 —— 端到端比对没有鉴别力，"
                "阶段 6 的验收判据无效")
            # 对调后 "阻断需修复" 被路由到 closeout，跳过了修订与复评
            assert actual == ["review1", "closeout"]

    def test_swapped_routes_change_artifact_set(self):
        """产物清单同样偏离基线 —— 第二条独立的比对判据。"""
        with tempfile.TemporaryDirectory() as t:
            s = Scene(Path(t), "阻断需修复", swap_routes=True)
            s.run()
            produced = set(s.artifacts())
            assert produced != EXPECTED_ARTIFACTS, "产物清单比对没有鉴别力"
            assert "fix-1.md" not in produced and "review-2.md" not in produced

    def test_swapped_routes_recorded_in_journal(self):
        """journal 如实记下走了哪条边 —— 事后可归因，不必靠猜。"""
        with tempfile.TemporaryDirectory() as t:
            s = Scene(Path(t), "阻断需修复", swap_routes=True)
            s.run()
            r1 = [r for r in s.journal()
                  if r["event"] == "step-completed" and r["step"] == "review1"][0]
            assert r1["route_key"] == "阻断需修复"
            assert r1["route_matched"] == "阻断需修复"
            assert r1["next"] == "closeout", "journal 应如实记录被对调后的实际去向"

    def test_tampered_artifact_breaks_sha256_comparison(self):
        """产物被改动 → SHA256 比对失败。证明该判据对内容变化敏感。"""
        with tempfile.TemporaryDirectory() as t:
            s = Scene(Path(t), "阻断需修复")
            s.run()
            baseline = s.artifacts()
            target = s.wd / "review-1.md"
            target.write_text(target.read_text(encoding="utf-8") + "\n<!-- 篡改 -->\n",
                              encoding="utf-8")
            assert s.artifacts() != baseline
