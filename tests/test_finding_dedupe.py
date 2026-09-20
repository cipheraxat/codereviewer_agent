from codereview.finding_dedupe import dedupe_findings, titles_similar
from codereview.models import Finding, FindingCategory, Severity


def test_titles_similar_for_sql_variants() -> None:
    assert titles_similar("SQL Injection Vulnerability", "SQL Injection Risk")
    assert titles_similar("Possible hardcoded secret", "Hardcoded API Key")


def test_dedupe_merges_similar_security_findings() -> None:
    findings = [
        Finding(
            category=FindingCategory.SECURITY,
            severity=Severity.CRITICAL,
            title="SQL Injection Vulnerability",
            file="auth.py",
            line=9,
            rationale="a",
            suggestion="b",
            confidence=0.9,
            agent="security",
        ),
        Finding(
            category=FindingCategory.SECURITY,
            severity=Severity.CRITICAL,
            title="SQL Injection Risk",
            file="auth.py",
            line=9,
            rationale="c",
            suggestion="d",
            confidence=1.0,
            agent="pattern",
        ),
    ]
    deduped = dedupe_findings(findings)
    assert len(deduped) == 1
    assert deduped[0].confidence == 1.0


def test_dedupe_merges_same_line_across_categories() -> None:
    """Security + pattern often double-report the same secret on the same line."""
    findings = [
        Finding(
            category=FindingCategory.SECURITY,
            severity=Severity.CRITICAL,
            title="Hardcoded Secret",
            file="probe.ts",
            line=7,
            rationale="a",
            suggestion="b",
            confidence=1.0,
            agent="security",
        ),
        Finding(
            category=FindingCategory.QUALITY,
            severity=Severity.CRITICAL,
            title="Hardcoded Secret",
            file="probe.ts",
            line=7,
            rationale="c",
            suggestion="d",
            confidence=1.0,
            agent="pattern",
        ),
        Finding(
            category=FindingCategory.SECURITY,
            severity=Severity.HIGH,
            title="Use of eval()",
            file="probe.ts",
            line=16,
            rationale="e",
            suggestion="f",
            confidence=1.0,
            agent="security",
        ),
    ]
    deduped = dedupe_findings(findings)
    assert len(deduped) == 2
    lines = {finding.line for finding in deduped}
    assert lines == {7, 16}
