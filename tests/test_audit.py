import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


AUDIT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "audit.py"
SPEC = importlib.util.spec_from_file_location("pisr_audit", AUDIT_PATH)
audit = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(audit)


class StructureLinksTest(unittest.TestCase):
    def test_structure_links_are_relative_to_structure_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()  # resolve: 与 audit._resolve 的 .resolve() 对齐（8.3 短路径环境）
            docs = root / "docs"
            (docs / "problems" / "bugfix").mkdir(parents=True)
            (docs / "CURRENT.md").write_text("# Current\n", encoding="utf-8")
            (docs / "problems" / "bugfix" / "fixed.md").write_text(
                "# Fixed\n", encoding="utf-8"
            )
            (docs / "STRUCTURE.md").write_text(
                "# Index\n\n"
                "| Topic | Document |\n"
                "|---|---|\n"
                "| Current | [CURRENT](CURRENT.md) |\n"
                "| Bugs | [bugs](problems/bugfix/) |\n",
                encoding="utf-8",
            )

            with patch.object(audit, "ROOT", root):
                results = audit._check_structure()

            failures = [r for r in results if r["status"] != "ok"]
            self.assertEqual([], failures)


class DriftWordBoundaryTest(unittest.TestCase):
    def test_substring_words_do_not_count_as_mentions(self):
        # "windows"/"shows" contain "ws" as a substring but are not
        # WebSocket mentions; bare keyword occurrences still match.
        patterns = audit.DRIFT_KEYWORD_RES["WebSocket"]
        self.assertFalse(audit._mentions(patterns, "windows powershell shows hooks"))
        self.assertTrue(audit._mentions(patterns, "a plain ws connection"))
        self.assertTrue(audit._mentions(patterns, "socket.io gateway"))

    def test_drift_check_ignores_windows_substring(self):
        # Regression: docs saying "Windows PowerShell" must not report
        # WebSocket drift merely because "windows" contains "ws".
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()  # resolve: 与 audit._resolve 的 .resolve() 对齐（8.3 短路径环境）
            docs = root / "docs"
            docs.mkdir()
            (docs / "overview.md").write_text(
                "# Overview\n\nRuns on Windows PowerShell.\n", encoding="utf-8"
            )
            (root / "package.json").write_text(
                '{"name": "x", "dependencies": {}}', encoding="utf-8"
            )

            with patch.object(audit, "ROOT", root):
                results = audit._drift_check()

            drifts = [r for r in results if r["status"] == "drift"]
            self.assertEqual([], drifts)


if __name__ == "__main__":
    unittest.main()
