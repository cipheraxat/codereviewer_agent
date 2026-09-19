import shutil
from pathlib import Path

from codereview.agents.pattern import PatternAgent
from codereview.agents.security import SecurityAgent
from codereview.chunking import chunk_text
from codereview.config import ReviewerConfig
from codereview.diff_utils import (
    evidence_from_match,
    line_for_evidence,
    line_for_pattern,
    redact_evidence,
)
from codereview.file_selection import select_files
from codereview.finding_dedupe import dedupe_findings
from codereview.github_client import synthetic_pr_from_diff
from codereview.models import Finding, FindingCategory, Severity
from codereview.path_utils import is_denied_path, should_ignore_path
from codereview.review_tools import RepoTools
from codereview.rule_packs import pack_for_path, rules_for_path


def test_line_for_pattern_finds_added_line() -> None:
    patch = """@@ -1,3 +1,5 @@
 def login(username):
     if not username:
         return False
+    api_key = "secret"
+    print("debug", username)
     return True
"""
    assert line_for_pattern(patch, r"api_key") == 4
    assert line_for_pattern(patch, r"print\(") == 5


def test_line_for_evidence_anchors_snippet() -> None:
    patch = """@@ -1,3 +1,5 @@
 def login(username):
     if not username:
         return False
+    api_key = "secret"
+    print("debug", username)
     return True
"""
    evidence = 'api_key = "secret"\nprint("debug", username)'
    assert line_for_evidence(patch, evidence) == 4
    assert evidence_from_match(patch, r"api_key") is not None


def test_ignore_globs_match_venv_paths() -> None:
    assert should_ignore_path(".venv311/lib/python3.11/site-packages/x.py", ["**/.venv311/**"])
    assert should_ignore_path("src/app.py", ["**/.venv311/**"]) is False


def test_select_and_bundle_filters_ignored_and_groups() -> None:
    diff = """diff --git a/src/a.py b/src/a.py
--- a/src/a.py
+++ b/src/a.py
@@ -1 +1,2 @@
+print(1)
diff --git a/src/b.py b/src/b.py
--- a/src/b.py
+++ b/src/b.py
@@ -1 +1,2 @@
+print(2)
diff --git a/.venv311/x.py b/.venv311/x.py
--- a/.venv311/x.py
+++ b/.venv311/x.py
@@ -1 +1,2 @@
+secret
"""
    pr = synthetic_pr_from_diff(diff)
    config = ReviewerConfig(ignore_globs=["**/.venv311/**"], review={"max_files_per_bundle": 8})
    result = select_files(pr, config)
    assert all(item.path.startswith("src/") for item in result.selected)
    assert not any(".venv311" in item.path for item in result.selected)
    assert any(item.reason == "ignored" for item in result.skipped)
    assert len(result.bundles) >= 1


def test_rule_packs_for_python_path() -> None:
    config = ReviewerConfig(languages=["python"])
    assert pack_for_path("src/app.py") == "python"
    rules = rules_for_path("src/app.py", config)
    assert any(rule.id == "py-no-eval" for rule in rules)
    assert not any("hardcoded-secret" in rule.id for rule in rules)


def test_context_line_print_does_not_fire() -> None:
    diff = """diff --git a/src/app.py b/src/app.py
--- a/src/app.py
+++ b/src/app.py
@@ -1,4 +1,5 @@
 def main():
     print("existing debug")
     x = 1
+    y = 2
     return x
"""
    pr = synthetic_pr_from_diff(diff)
    findings = PatternAgent()._heuristic_scan(pr, ReviewerConfig())
    assert not any("print" in f.title.lower() for f in findings)


def test_secret_finding_is_single_and_redacted() -> None:
    diff = """diff --git a/src/config.py b/src/config.py
--- a/src/config.py
+++ b/src/config.py
@@ -1,2 +1,3 @@
 import os
+API_KEY = "sk-live-abcdef123456"
 x = 1
"""
    pr = synthetic_pr_from_diff(diff)
    config = ReviewerConfig()
    sec = SecurityAgent()._heuristic_scan(pr, config)
    pat = PatternAgent()._heuristic_scan(pr, config)
    merged = dedupe_findings(sec + pat)
    assert len(merged) == 1
    finding = merged[0]
    assert finding.category == FindingCategory.SECURITY
    assert finding.evidence_snippet is not None
    assert "sk-live" not in finding.evidence_snippet
    assert "***" in finding.evidence_snippet


def test_redact_evidence_masks_literal() -> None:
    raw = 'API_KEY = "sk-live-abcdef123456"'
    redacted = redact_evidence(raw)
    assert redacted is not None
    assert "sk-live" not in redacted
    assert "***" in redacted


def test_search_code_denies_dotenv() -> None:
    root = Path("tmp_denylist_test")
    if root.exists():
        shutil.rmtree(root)
    (root / "src").mkdir(parents=True)
    (root / ".env").write_text('OPENAI_KEY="sk-secret-value-123"\n')
    (root / "src" / "app.py").write_text('OPENAI_KEY = os.environ["OPENAI_KEY"]\n')
    try:
        assert is_denied_path(".env")
        tools = RepoTools(root, ignore_globs=[])
        hits = tools.search_code("OPENAI_KEY")
        assert not any(line.startswith(".env:") for line in hits.splitlines())
        assert "src/app.py" in hits
    finally:
        shutil.rmtree(root)


def test_same_line_findings_dedupe() -> None:
    findings = [
        Finding(
            category=FindingCategory.SECURITY,
            severity=Severity.HIGH,
            title="Possible hardcoded secret",
            file="a.py",
            line=2,
            rationale="r1",
            suggestion="s1",
            confidence=0.85,
            agent="security",
        ),
        Finding(
            category=FindingCategory.SECURITY,
            severity=Severity.HIGH,
            title="Possible hardcoded secret in Python source",
            file="a.py",
            line=2,
            rationale="r2",
            suggestion="s2",
            confidence=0.8,
            agent="security",
        ),
    ]
    assert len(dedupe_findings(findings)) == 1


def test_chunk_text_rejects_bad_overlap() -> None:
    try:
        chunk_text("x" * 100, max_chars=10, overlap=10)
        raise AssertionError("expected ValueError")
    except ValueError as exc:
        assert "must be <" in str(exc)


def test_chunk_text_splits_with_overlap() -> None:
    chunks = chunk_text("abcdefghij", max_chars=4, overlap=1)
    assert chunks[0] == "abcd"
    assert len(chunks) > 1


def test_no_hunks_patch_is_skipped() -> None:
    diff = """diff --git a/old.py b/new.py
similarity index 90%
rename from old.py
rename to new.py
"""
    pr = synthetic_pr_from_diff(diff)
    result = select_files(pr, ReviewerConfig())
    assert result.selected == []
    assert any(item.reason == "no_hunks" for item in result.skipped)


def test_env_denylist_is_precise() -> None:
    assert is_denied_path(".env")
    assert is_denied_path(".env.local")
    assert is_denied_path(".env.production")
    assert not is_denied_path(".environment.py")
    assert not is_denied_path(".env.example")
    assert not is_denied_path("src/env_utils.py")


def test_remainder_bundle_is_capped() -> None:
    patches = {f"dir{i}/f.py": "@@ -1 +1,2 @@\n+x=1\n" for i in range(20)}
    pr = synthetic_pr_from_diff("").model_copy(update={"changed_files": list(patches), "patches": patches})
    result = select_files(pr, ReviewerConfig(review={"max_files_per_bundle": 1, "max_bundles": 4}))
    sizes = [len(b.files) for b in result.bundles]
    assert len(result.bundles) <= 4
    assert max(sizes) <= 2  # remainder_cap = max_files_per_bundle * 2
    assert any(item.reason == "bundle_cap" for item in result.skipped)


def test_quality_evidence_is_redacted() -> None:
    from codereview.agents.pattern import anchor_findings

    diff = """diff --git a/a.py b/a.py
--- a/a.py
+++ b/a.py
@@ -1 +1,2 @@
+API_KEY = "sk-live-abcdef123456"
"""
    pr = synthetic_pr_from_diff(diff)
    findings = [
        Finding(
            category=FindingCategory.QUALITY,
            severity=Severity.LOW,
            title="Style",
            file="a.py",
            line=1,
            rationale="r",
            suggestion="s",
            confidence=0.8,
            agent="pattern",
            evidence_snippet='API_KEY = "sk-live-abcdef123456"',
        )
    ]
    anchored = anchor_findings(findings, pr)
    assert "sk-live" not in (anchored[0].evidence_snippet or "")
