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
