from __future__ import annotations

import re

from codereview.models import Finding, SEVERITY_ORDER


def normalize_title(title: str) -> str:
  return re.sub(r"[^a-z0-9]+", " ", title.lower()).strip()


def titles_similar(left: str, right: str) -> bool:
  a = normalize_title(left)
  b = normalize_title(right)
  if not a or not b:
    return False
  if a == b:
    return True
  if a in b or b in a:
    return True

  tokens_a = set(a.split())
  tokens_b = set(b.split())
  if not tokens_a or not tokens_b:
    return False

  overlap = len(tokens_a & tokens_b)
  union = len(tokens_a | tokens_b)
  if union and overlap / union >= 0.5:
    return True

  topic_groups = [
    {"sql", "injection", "query", "interpolat"},
    {"secret", "api", "key", "hardcoded", "credential", "token"},
    {"debug", "print", "logging", "log"},
  ]
  for group in topic_groups:
    if any(token in a for token in group) and any(token in b for token in group):
      return True
  return False


def findings_overlap(left: Finding, right: Finding) -> bool:
  if left.category != right.category:
    return False
  if left.file and right.file and left.file == right.file:
    if titles_similar(left.title, right.title):
      return True
    if left.line is not None and right.line is not None and left.line == right.line:
      return True
  return titles_similar(left.title, right.title) and left.file == right.file


def dedupe_findings(findings: list[Finding]) -> list[Finding]:
  """Merge overlapping findings, keeping the highest-confidence item."""
  ranked = sorted(
    findings,
    key=lambda finding: (SEVERITY_ORDER[finding.severity], finding.confidence),
    reverse=True,
  )
  kept: list[Finding] = []
  for candidate in ranked:
    if any(findings_overlap(candidate, existing) for existing in kept):
      continue
    kept.append(candidate)
  return kept
