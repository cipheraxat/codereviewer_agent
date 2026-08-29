from __future__ import annotations

import re


def first_changed_line(patch: str) -> int | None:
  """Return the first new line number introduced in a unified diff hunk."""
  for line in patch.splitlines():
    if line.startswith("@@"):
      match = re.search(r"\+(\d+)", line)
      if match:
        return int(match.group(1))
  return None


def line_for_pattern(patch: str, pattern: str) -> int | None:
  """Return the line number of the first added line matching pattern."""
  current_line: int | None = None
  compiled = re.compile(pattern)

  for line in patch.splitlines():
    if line.startswith("@@"):
      match = re.search(r"\+(\d+)", line)
      current_line = int(match.group(1)) - 1 if match else None
      continue
    if current_line is None:
      continue
    if line.startswith("+") and not line.startswith("+++"):
      current_line += 1
      if compiled.search(line[1:]):
        return current_line
    elif line.startswith(" "):
      current_line += 1

  return first_changed_line(patch)
