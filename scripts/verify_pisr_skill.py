"""Offline mechanical regression checks for PISR SKILL.md.

Resolves SKILL.md relative to this script. Runs no network calls,
no pi invocations, no model status checks. Exits nonzero on failure.
"""

import importlib.util
import json
import os
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SKILL_PATH = os.path.join(SCRIPT_DIR, "..", "SKILL.md")

# Import TELEMETRY_FIELDS from pisr_dispatch.py for deterministic field-set comparison
DISPATCH_SCRIPT = os.path.join(SCRIPT_DIR, "pisr_dispatch.py")
_SPEC = importlib.util.spec_from_file_location("pisr_dispatch_verify", DISPATCH_SCRIPT)
_DISPATCH_MOD = importlib.util.module_from_spec(_SPEC) if _SPEC and _SPEC.loader else None
if _DISPATCH_MOD:
    sys.modules[_SPEC.name] = _DISPATCH_MOD
    _SPEC.loader.exec_module(_DISPATCH_MOD)
    TELEMETRY_FIELDS = getattr(_DISPATCH_MOD, "TELEMETRY_FIELDS", {})
else:
    TELEMETRY_FIELDS = {}


def read_skill():
    if not os.path.isfile(SKILL_PATH):
        print(f"FAIL: SKILL.md not found at {SKILL_PATH}")
        sys.exit(1)
    with open(SKILL_PATH, "rb") as f:
        raw = f.read()
    with open(SKILL_PATH, "r", encoding="utf-8") as f:
        text = f.read()
    return raw, text


def check_encoding(raw):
    """Verify UTF-8 without BOM, LF-only."""
    ok = True
    if raw.startswith(b"\xef\xbb\xbf"):
        print("FAIL: SKILL.md starts with UTF-8 BOM")
        ok = False
    else:
        print("PASS: no UTF-8 BOM")
    if b"\r\n" in raw:
        print("FAIL: SKILL.md contains CRLF")
        ok = False
    else:
        print("PASS: LF-only line endings")
    if b"\r" in raw.replace(b"\r\n", b""):
        print("FAIL: SKILL.md contains stray CR")
        ok = False
    else:
        print("PASS: no stray CR")
    return ok


def check_frontmatter(text):
    """Verify frontmatter name and primary trigger description exist."""
    ok = True
    if not text.startswith("---"):
        print("FAIL: SKILL.md does not start with frontmatter")
        return False
    end = text.find("---", 3)
    if end == -1:
        print("FAIL: frontmatter not closed")
        return False
    fm = text[:end]
    if 'name: pisr' not in fm.lower():
        print("FAIL: frontmatter missing 'name: pisr'")
        ok = False
    else:
        print("PASS: frontmatter name present")
    triggers = ['pisr', 'pi', 'subagent']
    found_triggers = [t for t in triggers if t.lower() in fm.lower()]
    if found_triggers:
        print(f"PASS: trigger keywords present: {found_triggers}")
    else:
        print("FAIL: no trigger keywords in frontmatter description")
        ok = False
    return ok


def check_knowledge_cutoff(text):
    """Old pattern must be absent; new risk-anchor pattern must be present."""
    ok = True
    if "你的知识截止于" in text:
        print("FAIL: old knowledge-cutoff claim '你的知识截止于' still present")
        ok = False
    else:
        print("PASS: old knowledge-cutoff claim absent")
    if "知识截止可能早于今天" in text:
        print("PASS: new risk-anchor pattern present")
    else:
        print("FAIL: new risk-anchor pattern '知识截止可能早于今天' missing")
        ok = False
    return ok


def check_retry_limit(text):
    """Max total attempts (3) and budget expansion rules must be present."""
    ok = True
    if "3 次总尝试" in text or "3次总尝试" in text:
        print("PASS: max total attempts (3) present")
    else:
        print("FAIL: max total attempts (3) not found")
        ok = False
    if "未经新鲜授权" in text and "不突破" in text:
        print("PASS: budget expansion authorization rule present")
    else:
        print("FAIL: budget expansion authorization rule missing")
        ok = False
    return ok


def check_cost_zero_heuristic(text):
    """cost=0 / cost metadata absence must be marked as heuristic, not weak-model equivalence."""
    ok = True
    heuristic_signals = ["启发式", "元数据可能缺失", "不能单独证明", "价格元数据", "heuristic", "不含价格"]
    has_heuristic = any(s in text for s in heuristic_signals)
    old_bad = ["免费档", "免费/弱模型", "free/weak"]
    has_old_bad = any(s in text for s in old_bad)
    if has_heuristic:
        print("PASS: cost metadata absence described with heuristic/risk-signal language")
    else:
        print("FAIL: cost metadata absence not described as heuristic risk signal")
        ok = False
    if not has_old_bad:
        print("PASS: no legacy 'free/weak model' equivalence for cost=0")
    else:
        print("FAIL: legacy 'free/weak model' language still present for cost=0")
        ok = False
    return ok


def check_security_boundary(text):
    """--tools allowlist and prompt directives must be stated as non-sandbox."""
    ok = True
    if "不是安全沙箱" in text or "不构成安全" in text or "非安全沙箱" in text:
        print("PASS: non-sandbox boundary language present")
    else:
        print("FAIL: non-sandbox boundary language missing")
        ok = False
    if "不能阻止" in text or "不能构成" in text or "不构成安全" in text:
        print("PASS: security limitation (what it cannot prevent) stated")
    else:
        print("FAIL: security limitation not stated")
        ok = False
    if "--tools" in text:
        print("PASS: --tools tool-face mechanism documented")
    else:
        print("FAIL: --tools tool-face mechanism missing")
        ok = False
    return ok


def check_write_fallback(text):
    """Write tool fallback language must exist (not hard Write requirement)."""
    ok = True
    if "优先使用 write" in text or "若无 write 工具" in text or "回退到" in text:
        print("PASS: write fallback language present")
    else:
        print("FAIL: write fallback language missing")
        ok = False
    if "未实际写入文件" in text or "未实际调用" in text:
        print("PASS: real file evidence still required")
    else:
        print("FAIL: real file evidence requirement missing")
        ok = False
    return ok


def check_ps_version_table(text):
    """PowerShell 5.1/7 difference table must exist (manual invocation path)."""
    ok = True
    has_51 = "PowerShell 5.1" in text or "PS5.1" in text
    has_7 = "PowerShell 7" in text or "PS7" in text
    has_utf16le = "UTF-16LE" in text
    if has_51 and has_7 and has_utf16le:
        print("PASS: PS5.1/7 encoding difference table present")
    else:
        print("FAIL: PS5.1/7 encoding difference table missing or incomplete")
        ok = False
    return ok


def check_model_id_rule(text):
    """pi --list-models rule must be present; memory-spliced IDs must be forbidden."""
    ok = True
    if "pi --list-models" in text:
        print("PASS: pi --list-models rule present")
    else:
        print("FAIL: pi --list-models rule missing")
        ok = False
    if "禁止凭" in text and ("记忆" in text or "id" in text or "裸名" in text):
        print("PASS: memory/bare-name splicing forbidden")
    else:
        print("FAIL: memory/bare-name splice prohibition not found")
        ok = False
    return ok


def check_tool_face_declaration(text):
    """PISR-specific: prompt seven elements incl. tool-face declaration."""
    ok = True
    if "工具面" in text and ("第七" in text or "七要素" in text or "7 项" in text or "七项" in text):
        print("PASS: tool-face declaration (7th prompt element) present")
    else:
        print("FAIL: tool-face declaration (7th prompt element) missing")
        ok = False
    if "read,grep,find,ls" in text:
        print("PASS: read-only reviewer tool set documented")
    else:
        print("FAIL: read-only reviewer tool set missing")
        ok = False
    return ok


def check_converge_exclusion(text):
    """PISR must explicitly disclaim converge Spawn-backend usage (not integrated)."""
    ok = True
    if "converge" in text.lower() and ("不得" in text or "未接入" in text or "禁止" in text):
        print("PASS: converge non-integration disclaimer present")
    else:
        print("FAIL: converge non-integration disclaimer missing")
        ok = False
    return ok


def check_dispatch_hardening(text):
    """Dispatch-link hardening anchors must be present."""
    ok = True
    # C2 silent-stall failure mode + watchdog threshold
    if "静默停滞" in text and "1.5" in text and "15 分钟" in text:
        print("PASS: C2 silent-stall failure mode + watchdog threshold (15min)")
    else:
        print("FAIL: C2 watchdog / silent-stall not found")
        ok = False
    # idempotency no-auto-retry clause
    if "禁止自动重派" in text and "幂等性" in text:
        print("PASS: idempotency no-auto-retry clause preserved")
    else:
        print("FAIL: idempotency no-auto-retry clause missing")
        ok = False
    # §7 pitfall-table rows
    if "harness 前台超时 < 单轮耗时" in text and "模型端静默停滞" in text:
        print("PASS: pitfall-table rows present")
    else:
        print("FAIL: pitfall-table rows missing")
        ok = False
    # C3 failover ladder
    if "失败切换阶梯" in text and "切换 family" in text:
        print("PASS: C3 failover ladder")
    else:
        print("FAIL: C3 failover ladder missing")
        ok = False
    # C4 dispatch telemetry + default-flip threshold
    if "dispatch-log" in text and "≥5 次" in text:
        print("PASS: C4 dispatch telemetry + default-flip threshold")
    else:
        print("FAIL: C4 dispatch telemetry missing")
        ok = False
    # C5 honest value-premise
    if "价值前提" in text and "派发链路已在本机验证" in text:
        print("PASS: C5 honest value-premise")
    else:
        print("FAIL: C5 value premise missing")
        ok = False
    return ok


def check_telemetry_fields():
    """Verify dispatch-patterns.md telemetry template contains all implementation fields.

    Uses TELEMETRY_FIELDS from pisr_dispatch.py as single source of truth.
    Required fields must appear in the template; optional fields should appear.
    """
    if not TELEMETRY_FIELDS:
        print("FAIL: could not load TELEMETRY_FIELDS from pisr_dispatch.py")
        return False
    dp_path = os.path.join(SCRIPT_DIR, "..", "refs", "dispatch-patterns.md")
    if not os.path.isfile(dp_path):
        print(f"FAIL: dispatch-patterns.md not found at {dp_path}")
        return False
    with open(dp_path, "r", encoding="utf-8") as f:
        dp_text = f.read()

    section_marker = "## 派发遥测记录片段"
    section_pos = dp_text.find(section_marker)
    if section_pos < 0:
        print("FAIL: telemetry section header not found in dispatch-patterns.md")
        return False

    tail = dp_text[section_pos:]
    in_block = False
    code_block_lines = []
    for line in tail.split("\n"):
        if not in_block and "```" in line:
            in_block = True
            continue
        if in_block:
            if line.strip().startswith("```"):
                break
            code_block_lines.append(line)
    template_text = "\n".join(code_block_lines)

    missing_required = []
    missing_optional = []
    for field, kind in sorted(TELEMETRY_FIELDS.items()):
        if field not in template_text:
            if kind == "required":
                missing_required.append(field)
            else:
                missing_optional.append(field)

    ok = True
    if missing_required:
        print(f"FAIL: telemetry template missing required fields: {', '.join(missing_required)}")
        ok = False
    if missing_optional:
        print(f"INFO: telemetry template missing optional fields: {', '.join(missing_optional)}")
    if ok:
        print(f"PASS: telemetry template matches implementation schema ({len(TELEMETRY_FIELDS)} fields)")
    return ok


def check_allowlist():
    """Verify the user-editable model allowlist loads as a non-empty frozenset."""
    ok = True
    allowed = getattr(_DISPATCH_MOD, "ALLOWED_MODELS", None) if _DISPATCH_MOD else None
    if allowed is None:
        print("FAIL: ALLOWED_MODELS not found in pisr_dispatch.py")
        return False
    if not isinstance(allowed, frozenset):
        print("FAIL: ALLOWED_MODELS is not a frozenset")
        ok = False
    if not allowed:
        print("FAIL: ALLOWED_MODELS is empty")
        ok = False
    config_path = getattr(_DISPATCH_MOD, "ALLOWED_MODELS_PATH", None) if _DISPATCH_MOD else None
    if not config_path or not config_path.is_file():
        print("FAIL: user-editable allowed-models.json not found")
        ok = False
    if ok:
        print(f"PASS: user-editable ALLOWED_MODELS loaded: {', '.join(sorted(allowed))}")
    return ok


def main():
    raw, text = read_skill()
    results = []
    results.append(("Encoding/BOM/CRLF", check_encoding(raw)))
    results.append(("Frontmatter", check_frontmatter(text)))
    results.append(("Knowledge cutoff", check_knowledge_cutoff(text)))
    results.append(("Retry limit + budget", check_retry_limit(text)))
    results.append(("cost heuristic", check_cost_zero_heuristic(text)))
    results.append(("Security boundary (--tools)", check_security_boundary(text)))
    results.append(("Write fallback", check_write_fallback(text)))
    results.append(("PS5.1/7 table", check_ps_version_table(text)))
    results.append(("Model ID rule", check_model_id_rule(text)))
    results.append(("Tool-face declaration", check_tool_face_declaration(text)))
    results.append(("Converge exclusion", check_converge_exclusion(text)))
    results.append(("Telemetry field sync", check_telemetry_fields()))
    results.append(("Dispatch hardening (C2-C5)", check_dispatch_hardening(text)))
    results.append(("Model allowlist (ALLOWED_MODELS)", check_allowlist()))

    failed = [name for name, ok in results if not ok]
    print(f"\n{'='*50}")
    if failed:
        print(f"FAILED ({len(failed)}/{len(results)}): {', '.join(failed)}")
        sys.exit(1)
    else:
        print(f"ALL {len(results)} CHECKS PASSED")
        sys.exit(0)


if __name__ == "__main__":
    main()
