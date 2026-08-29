# Multi-Agent GitHub PR Reviewer

Production-style PR review pipeline for GitHub: retrieve relevant repo context, run parallel security and pattern agents, ensemble the findings, and post structured review comments.

<p align="center">
  <img src="docs/images/architecture.svg" alt="Multi-agent PR review architecture diagram" width="900"/>
</p>

## How it works

<p align="center">
  <img src="docs/images/data-flow.svg" alt="End-to-end PR review data flow" width="900"/>
</p>

1. **Trigger** — PR opened/updated or `codereview review-pr` from CLI
2. **Ingest** — fetch changed files and unified diff from GitHub
3. **Retrieve** — RAG-lite context from repo checkout + `reviewer.yaml` team rules
4. **Analyze** — Security + Pattern agents run in parallel (LangGraph)
5. **Ensemble** — dedupe, filter by confidence/severity, assign verdict
6. **Publish** — structured PR review + inline comments + `review-report.json`

<p align="center">
  <img src="docs/images/langgraph-pipeline.svg" alt="LangGraph fan-out fan-in orchestration" width="720"/>
</p>

## Example output on GitHub

<p align="center">
  <img src="docs/images/github-review-example.svg" alt="Example GitHub PR review with inline comments" width="900"/>
</p>

Every finding is structured — not just prose:

| Field | Example |
|---|---|
| category | `security` |
| severity | `high` |
| file / line | `src/app/auth.ts:42` |
| confidence | `0.85` |
| suggestion | Use env vars instead of hardcoded secrets |

## Features

- Structured findings: category, severity, file, line, rationale, suggestion, confidence
- RAG-lite context retrieval from changed files and neighbors (no vector DB required in v1)
- Parallel specialist agents with LangGraph fan-out/fan-in
- Ensemble verifier dedupes, filters, and assigns verdict
- Works offline with `review-diff` (no GitHub API)
- GitHub Action for company repos
- Team rules via `reviewer.yaml` (procedural memory)
- OpenRouter, OpenAI, or Anthropic for LLM-backed analysis

## Quick start

### 1. Install

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

### 2. Configure

Copy the example config into your target repo:

```bash
cp reviewer.example.yaml reviewer.yaml
```

Set environment variables:

```bash
export LLM_API_KEY=sk-or-...          # OpenRouter API key
export LLM_PROVIDER=openrouter        # openrouter | openai | anthropic
export LLM_MODEL=openai/gpt-4o-mini   # optional; any OpenRouter model slug
export GITHUB_TOKEN=ghp_...           # only needed for review-pr
```

**OpenRouter (recommended):** use your OpenRouter key as `LLM_API_KEY` with `LLM_PROVIDER=openrouter`. Pick any model from [openrouter.ai/models](https://openrouter.ai/models), e.g. `openai/gpt-4o-mini`, `anthropic/claude-3.5-sonnet`.

### 3. Review a local diff (no API keys required for heuristics)

```bash
codereview review-diff tests/fixtures/sample_diff.patch --repo-root .
```

### 4. Review a GitHub PR

```bash
codereview review-pr owner/repo#123 --repo-root . --output review-report.json
```

Use `--dry-run` to generate the report without posting comments.

## GitHub Action (company install)

Add to your company repo:

```yaml
# .github/workflows/pr-review.yml
name: PR Review
on:
  pull_request:
    types: [opened, synchronize, reopened]

permissions:
  contents: read
  pull-requests: write

jobs:
  review:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0

      - uses: cipheraxat/codereviewer_agent/action@main
        with:
          llm_api_key: ${{ secrets.LLM_API_KEY }}
          llm_provider: openrouter
          llm_model: openai/gpt-4o-mini
          config_path: reviewer.yaml
          dry_run: "false"
```

Reviews post as **github-actions[bot]** using the default `GITHUB_TOKEN`.

### Company setup checklist

1. Add `reviewer.yaml` with your team's ignore paths and custom rules
2. Create repo secret `LLM_API_KEY` (your OpenRouter API key)
3. Enable the workflow on `pull_request`
4. Start with `dry_run: true` for one sprint, then switch to live posting
5. Tune `severity_threshold` and `posting.min_confidence` to reduce noise

## CLI

```bash
codereview review-pr owner/repo#123 [--no-post] [--dry-run] [--config reviewer.yaml]
codereview review-diff path/to/changes.patch [--title "My change"]
codereview version
```

## Output

- Terminal summary table
- `review-report.json` audit artifact with metrics (latency, token usage, estimated cost)

## Project layout

```text
src/codereview/
  cli.py
  github_client.py
  context_engine.py
  graph.py
  agents/
    security.py
    pattern.py
    ensemble.py
action/
  action.yml
docs/images/
  architecture.svg
  data-flow.svg
  langgraph-pipeline.svg
  github-review-example.svg
```

## Resume bullet

Built a multi-agent GitHub PR reviewer (LangGraph) with RAG-style context retrieval, parallel security/pattern agents, and ensemble verification that posts structured inline comments via GitHub Actions.

## Roadmap (v2)

- JIRA / Confluence external context (ticket-aware reviews)
- GitHub App webhook ingress
- Postgres/pgvector semantic memory
- Redis job queue
- TimescaleDB observability for cost/latency trends

## License

MIT
