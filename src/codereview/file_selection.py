from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass, field

from codereview.config import ReviewerConfig
from codereview.models import PullRequestContext
from codereview.path_utils import should_ignore_path

logger = logging.getLogger(__name__)

BINARY_EXTENSIONS = {
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".webp",
    ".ico",
    ".pdf",
    ".zip",
    ".gz",
    ".tar",
    ".woff",
    ".woff2",
    ".ttf",
    ".eot",
    ".mp4",
    ".mp3",
    ".exe",
    ".dll",
    ".so",
    ".dylib",
    ".bin",
}


@dataclass
class SelectedFile:
    path: str
    patch: str
    reason: str = "selected"


@dataclass
class FileBundle:
    label: str
    files: list[SelectedFile] = field(default_factory=list)

    @property
    def paths(self) -> list[str]:
        return [item.path for item in self.files]


@dataclass
class SelectionResult:
    selected: list[SelectedFile]
    bundles: list[FileBundle]
    skipped: list[SelectedFile]


def select_files(pr: PullRequestContext, config: ReviewerConfig) -> SelectionResult:
    """Deterministic file selection before LLM review (OCR-style gates)."""
    selected: list[SelectedFile] = []
    skipped: list[SelectedFile] = []
    max_chars = config.review.max_diff_chars_per_file

    for path, patch in pr.patches.items():
        if should_ignore_path(path, config.ignore_globs):
            skipped.append(SelectedFile(path=path, patch=patch, reason="ignored"))
            continue
        lower = path.lower()
        if any(lower.endswith(ext) for ext in BINARY_EXTENSIONS):
            skipped.append(SelectedFile(path=path, patch=patch, reason="binary"))
            continue
        if "@@" not in patch:
            skipped.append(SelectedFile(path=path, patch=patch, reason="no_hunks"))
            continue
        if len(patch) > max_chars:
            logger.info("Skipping oversized patch %s (%s chars > %s)", path, len(patch), max_chars)
            skipped.append(SelectedFile(path=path, patch=patch, reason="oversized"))
            continue
        selected.append(SelectedFile(path=path, patch=patch, reason="selected"))
    bundles = bundle_files(selected, config)
    # Cap bundle count to bound LLM fan-out. Remainder is size-capped; overflow is skipped.
    max_bundles = max(1, config.review.max_bundles)
    max_per = max(1, config.review.max_files_per_bundle)
    remainder_cap = max_per * 2
    if len(bundles) > max_bundles:
        head = bundles[: max_bundles - 1]
        tail_files: list[SelectedFile] = []
        for extra in bundles[max_bundles - 1 :]:
            tail_files.extend(extra.files)
        kept = tail_files[:remainder_cap]
        overflow = tail_files[remainder_cap:]
        if kept:
            head.append(FileBundle(label="remainder", files=kept))
        for item in overflow:
            skipped.append(SelectedFile(path=item.path, patch=item.patch, reason="bundle_cap"))
            logger.info("Skipping %s due to max_bundles remainder cap", item.path)
        # Keep selected list in sync with what will actually be reviewed.
        overflow_paths = {item.path for item in overflow}
        selected = [item for item in selected if item.path not in overflow_paths]
        bundles = head
    return SelectionResult(selected=selected, bundles=bundles, skipped=skipped)


def bundle_files(files: list[SelectedFile], config: ReviewerConfig) -> list[FileBundle]:
    """Group selected files by parent directory, capped per bundle."""
    if not files:
        return []

    max_per = max(1, config.review.max_files_per_bundle)
    by_dir: dict[str, list[SelectedFile]] = defaultdict(list)
    for item in files:
        parent = item.path.rsplit("/", 1)[0] if "/" in item.path else "."
        by_dir[parent].append(item)

    bundles: list[FileBundle] = []
    for parent, group in sorted(by_dir.items()):
        for start in range(0, len(group), max_per):
            chunk = group[start : start + max_per]
            label = parent if start == 0 else f"{parent}#{start // max_per + 1}"
            bundles.append(FileBundle(label=label, files=chunk))
    return bundles


def filter_pr_context(pr: PullRequestContext, config: ReviewerConfig) -> tuple[PullRequestContext, SelectionResult]:
    """Return a PR context limited to selected files plus the selection plan."""
    result = select_files(pr, config)
    filtered = pr.model_copy(
        update={
            "changed_files": [item.path for item in result.selected],
            "patches": {item.path: item.patch for item in result.selected},
        }
    )
    return filtered, result


def pr_from_bundle(pr: PullRequestContext, bundle: FileBundle) -> PullRequestContext:
    """Narrow a PR context to a single review bundle."""
    return pr.model_copy(
        update={
            "changed_files": bundle.paths,
            "patches": {item.path: item.patch for item in bundle.files},
        }
    )


def format_bundle_summary(result: SelectionResult) -> str:
    lines = ["Review selection:"]
    if result.bundles:
        lines.append("Bundles:")
        for bundle in result.bundles:
            lines.append(f"- {bundle.label}: {', '.join(bundle.paths)}")
    else:
        lines.append("No reviewable files after selection filters.")
    if result.skipped:
        lines.append("Skipped:")
        for item in result.skipped[:20]:
            lines.append(f"- {item.path} ({item.reason})")
        if len(result.skipped) > 20:
            lines.append(f"- … and {len(result.skipped) - 20} more")
    return "\n".join(lines)
