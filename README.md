# Multi-Agent GitHub PR Reviewer

Production-style PR review pipeline for GitHub: batch-index knowledge into Supabase, retrieve relevant context at review time, run parallel security and pattern agents, ensemble the findings, and post structured review comments.

**v0.5.2** — OCR-inspired precision harness on top of unified RAG (code + JIRA + Confluence):

| Layer | What changed |
|---|---|
| **Select + bundle** | Hard denylist (secrets/junk), `ignore_globs`, binary/oversized/no-hunk gates, directory bundles capped by `max_bundles` |
| **Context** | Unified RAG + optional `file_read` / `code_search` tools (LLM only); first bundle gets full context, later bundles get a slim summary |
| **Agents** | Language rule packs (python/ts/js/go/yaml) + heuristics; findings carry `evidence_snippet` for line re-location |
| **Ensemble** | Semantic dedupe → optional LLM verify → OCR-style fact-check (drop only what the diff disproves) → precision posting |
| **Safety** | Evidence redaction on all categories; `EMBEDDING_API_KEY` when chat is Anthropic; Action secrets passed as inputs |

```mermaid
flowchart LR
  GH["GitHub PR<br/>diff + metadata"]
  SEL["Select + Bundle<br/>deny · ignore · size · group"]
  CE["Context Engine<br/>RAG + tools"]
  RC["Relevant Context<br/>snippets + packs"]
  SA["Security Agent"]
  PA["Pattern Agent"]
  EV["Ensemble<br/>dedupe · fact-check · verdict"]
  OUT["GitHub Review<br/>precision-filtered comments"]

  GH --> SEL --> CE --> RC
  RC --> SA
  RC --> PA
  SA --> EV
  PA --> EV
  EV --> OUT
```

## How it works (summary)

```mermaid
flowchart LR
  T["1. Trigger"]
  IX["2. Index<br/>batch / cron"]
  IN["3. Ingest"]
  S["4. Select<br/>+ bundle"]
  R["5. Retrieve<br/>RAG + tools"]
  A["6. Analyze<br/>per bundle"]
  E["7. Ensemble<br/>fact-check"]
  P["8. Publish"]

  T --> IX --> IN --> S --> R --> A --> E --> P
```

1. **Trigger** — PR opened/updated or `codereview review-pr` from CLI
2. **Index** (batch/cron) — `codereview index-knowledge` embeds repo code + JIRA + Confluence into Supabase
3. **Ingest** — fetch changed files and unified diff from GitHub
4. **Select + bundle** — drop denied/ignored/binary/oversized files; group remaining by directory (`review.max_files_per_bundle`, `review.max_bundles`)
5. **Retrieve** — changed-file snippets + unified vector search + BM25 fallback; optionally enrich with repo tools when an LLM is available
6. **Analyze** — Security + Pattern agents run in parallel per bundle (LangGraph fan-out)
7. **Ensemble** — dedupe → LLM verify (optional) → fact-check against the diff → confidence/severity filter → verdict
8. **Publish** — structured PR review + inline comments (opt-in posting) + `review-report.json`

```mermaid
flowchart TD
  START([START]) --> SEL[select_and_bundle]
  SEL --> BC[build_context]
  BC --> SR[security_review]
  BC --> PR[pattern_review]
  SR --> ENS[ensemble]
  PR --> ENS
  ENS --> ENDNODE([END])
```

---

## Data flow (detailed)

```mermaid
flowchart TD
  subgraph indexPhase [Indexing phase - batch or cron]
    codeWalk[Walk_repo_code]
    jiraBulk[JIRA_project_search]
    confBulk[Confluence_space_pages]
    chunk[Chunk_documents]
    embed[Embed_via_LLM]
  end

  subgraph store [Supabase pgvector]
    db[(code_embeddings)]
  end

  subgraph reviewPhase [Review phase - per PR]
    prEvent[GitHub_PR_or_CLI]
    ingest[Fetch_PR_diff]
    select[Select_and_bundle]
    changed[Selected_files]
    query[Embed_PR_query]
    search[Vector_similarity_search]
    tools[Optional_repo_tools]
    agents[Security_and_Pattern_per_bundle]
    ensemble[Ensemble_fact_check]
    output[PR_comments_and_report]
  end

  codeWalk --> chunk
  jiraBulk --> chunk
  confBulk --> chunk
  chunk --> embed --> db

  prEvent --> ingest --> select --> changed
  ingest --> query --> search
  db --> search
  changed --> agents
  search --> tools --> agents
  agents --> ensemble --> output
```

### Index knowledge (run before reviews)

```bash
# One-time or scheduled (see .github/workflows/knowledge-index.yml)
codereview index-knowledge --repo owner/repo --repo-root . --config reviewer.yaml
```

Requires `vector.enabled: true`, Supabase credentials, and `LLM_API_KEY`. When `external_context.enabled: true`, JIRA projects and Confluence spaces are indexed alongside repo code.

### End-to-end pipeline (review time)

```mermaid
flowchart TD
  subgraph trigger [Trigger]
    prEvent[GitHub_PR_event]
    cli[CLI_review-pr_or_review-diff]
  end

  subgraph ingest [Ingest]
    ghApi[GitHub_API_fetch_diff]
    parseDiff[Parse_local_diff]
    prCtx[PullRequestContext]
  end

  subgraph select [Select and bundle]
    deny[Hard_denylist]
    ignore[ignore_globs]
    gates[Binary_oversized_no_hunk]
    bundles[Directory_bundles]
  end

  subgraph context [Context Engine]
    changed[Changed_files]
    query[Embed_PR_query]
    vectorSearch[Supabase_vector_search]
    bm25Fallback[BM25_if_sparse]
    tools[file_read_code_search]
    mergeCtx[Merged_context_block]
  end

  subgraph agents [LangGraph Agents]
    sec[Security_Agent]
    pat[Pattern_Agent]
    ens[Ensemble_dedupe_fact_check]
  end

  subgraph output [Output]
    report[ReviewReport_JSON]
    ghReview[GitHub_PR_comments]
    artifact[Actions_artifact]
  end

  prEvent --> ghApi
  cli --> ghApi
  cli --> parseDiff
  ghApi --> prCtx
  parseDiff --> prCtx

  prCtx --> deny --> ignore --> gates --> bundles
  bundles --> changed
  bundles --> query --> vectorSearch
  changed --> mergeCtx
  vectorSearch --> mergeCtx
  vectorSearch --> bm25Fallback --> mergeCtx
  mergeCtx --> tools --> mergeCtx

  mergeCtx --> sec
  mergeCtx --> pat
  sec --> ens
  pat --> ens
  ens --> report
  report --> ghReview
  report --> artifact
```

### Step 1 — Trigger

| Path | When |
|---|---|
| **GitHub Action** | `pull_request` opened, synchronized, or reopened |
| **CLI** | `codereview review-pr owner/repo#123` or `codereview review-diff file.patch` |

### Step 2 — Ingest → `PullRequestContext`

The pipeline normalizes all inputs into one object:

| Field | Purpose |
|---|---|
| `changed_files[]` | Files touched in the PR |
| `patches{}` | Unified diff hunks per file |
| `title`, `body` | PR metadata for query terms and JIRA key extraction |
| `head_ref`, `base_ref` | Branch names (ticket keys often appear here) |
| `head_sha` | Commit identity for idempotent GitHub review posting |

### Step 3 — Select + bundle (OCR harness)

Before any LLM or RAG work, `select_and_bundle` narrows the PR to reviewable files:

| Gate | Behavior |
|---|---|
| **Hard denylist** | Always drop secrets/junk (`.env` variants, `*.pem`, `.ssh/`, `node_modules/`, …). Templates like `.env.example` are allowed; names like `.environment.py` are not treated as secrets. |
| **`ignore_globs`** | Team ignore patterns from `reviewer.yaml` (robust `**/dir/**` matching) |
| **Binary / no hunks** | Skip media/binaries and rename-only patches without `@@` hunks |
| **Oversized** | Skip patches larger than `review.max_diff_chars_per_file` (default 40k) |
| **Bundles** | Group remaining files by parent directory, up to `max_files_per_bundle` each |
| **Bundle cap** | At most `max_bundles` (default 4). Remainder is capped at `2 × max_files_per_bundle`; overflow is skipped with reason `bundle_cap` |

Agents then review **per bundle**. The first bundle receives the full shared `context_block`; later bundles get a slim summary so RAG/tool text is not re-sent in full.

### Step 4 — Hybrid context engine

When `vector.unified_rag: true` (default), review-time retrieval is:

1. **Changed files** — selected patches included directly
2. **Unified vector search** — semantic search over pre-indexed code, JIRA, and Confluence chunks in Supabase
3. **BM25 fallback** — if vector search returns fewer than `context.min_vector_snippets_before_fallback` hits, neighbor files are added via keyword scoring
4. **Repo tools** (optional) — when `review.use_tools: true` **and** an LLM is available, `file_read` / `code_search` pull nearby source around hunks (truncated at block boundaries)

Live JIRA/Confluence API calls are **not** made during review. Run `codereview index-knowledge` (or the `knowledge-index.yml` workflow) to refresh the index.

Set `vector.unified_rag: false` to restore the legacy path: BM25 neighbors always on + optional `index_on_review` embedding + live JIRA/Confluence fetch.

`ContextEngine.build_context()` merges sources, ranks by score, and returns the top N snippets (`context.max_snippets`, default 12).

```mermaid
flowchart LR
  pr[PR_metadata_and_diff] --> engine[ContextEngine]
  repo[Local_repo_checkout] --> engine
  yaml[reviewer.yaml] --> engine
  supa[(Supabase_pgvector_pre-indexed)] --> engine
  engine --> snippets[Ranked_CodeSnippets]
  snippets --> block[context_block_markdown]
```

#### A. Changed files (always on for selected paths)

Every **selected** file in the PR diff is included with a high fixed score. This guarantees the agents always see what actually changed (after deny/ignore/size gates).

#### B. Unified vector search (recommended)

When `vector.enabled: true`, `vector.unified_rag: true`, and Supabase credentials are set:

1. The PR title, body, and diff are embedded into a query vector
2. `match_code_embeddings()` searches pre-indexed chunks (code, JIRA, Confluence) keyed by `owner/repo`
3. Top-K similar chunks are merged into context with reason `vector_match:<source>`

Indexing happens separately via `codereview index-knowledge` or the scheduled `knowledge-index.yml` workflow — not on every PR review.

**Fail-open:** if Supabase or embeddings fail, the review continues with changed files + BM25 fallback.

**Anthropic chat:** Anthropic has no embeddings API. Set `EMBEDDING_API_KEY` (OpenAI-compatible) when `LLM_PROVIDER=anthropic`, or use OpenRouter/OpenAI for both.

#### C. BM25-lite neighbors (fallback / legacy)

- Used when unified vector search returns too few snippets, or when `unified_rag: false`
- Loads sibling files in the same directory as changed files
- Scores neighbors by keyword overlap with PR title, body, and diff tokens
- Zero external infrastructure; works offline

#### D. Repo tools (`file_read` / `code_search`)

When `review.use_tools: true` and an LLM client is available:

- Read a window of source around each hunk start
- Light in-repo symbol search from added-line identifiers
- Appended as `### tool:…` blocks; truncated at the last complete block so fences stay intact
- Skipped entirely in heuristic-only / no-key runs (no wasted I/O)

#### E. Legacy: index-on-review + live Atlassian (opt-in)

Set `vector.unified_rag: false` to enable the v0.3 path:

- **`index_on_review: true`** — embed and upsert changed/neighbor files during each review
- **Live JIRA/Confluence** — when `external_context.enabled: true`, fetches ticket/page content at review time

With the default unified RAG config, step E is skipped entirely.

### Step 5 — LangGraph agent orchestration

```text
START → select_and_bundle → build_context
           ├→ security_review  ─┐   (each agent loops bundles internally)
           └→ pattern_review   ─┴→ ensemble → ReviewReport → END
```

Both specialist agents receive:

- Selected PR diffs for the current bundle (`pr_from_bundle`)
- Shared `context_block` (full on first bundle, slim thereafter)
- `reviewer.yaml` (team rules, language packs, severity, precision)
- Optional LLM client (OpenRouter / OpenAI / Anthropic)

| Agent | Focus | Layers |
|---|---|---|
| **Security** | Secrets, injection, authz, unsafe defaults | Heuristics + language packs; LLM if `LLM_API_KEY` set |
| **Pattern** | Conventions, TODOs, tests, docs, smells | Heuristics + language packs; LLM if `LLM_API_KEY` set |
| **Ensemble** | Semantic dedupe, LLM verify, fact-check, filter, verdict | Rules-based dedupe; `ensemble.llm_verify` + `ensemble.fact_check` |

**Language rule packs** (`rule_packs.py`): built-in checklists for `python`, `typescript`, `javascript`, `go`, and `yaml`, enabled from `languages:` (yaml always on). Custom `reviewer.yaml` rules merge on top. Pack rules only fire on **added** lines.

**Line anchoring:** findings carry an `evidence_snippet`; the harness re-locates the line in the diff (OCR-style) when evidence is present. Findings without usable evidence fall back to the first changed line in the hunk. Evidence is **redacted** (API keys / tokens) for all finding categories before posting.

**Fact-check** (`ensemble.fact_check`): an LLM pass that may **drop** findings only when the provided diffs prove them wrong. Cited file hunks are prioritized when the fact-check diff budget is capped. `review.effort` (`low` / `medium` / `high`) controls how many fact-check rounds run.

**Precision posting:** with `review.precision_mode: true` (default), GitHub comments only include findings at/above `severity_threshold` with `posting.min_confidence` (default `0.70`). CLI posting is opt-in (`--post`); the Action uses `dry_run` / post flags explicitly.

**LLM fail-open:** if any LLM call fails, heuristic findings are still returned. The report includes `llm_degraded: true` when ensemble verification falls back.

### Step 6 — Structured output

Each finding is a typed object (not free-form prose):

| Field | Example |
|---|---|
| `category` | `security` |
| `severity` | `high` |
| `title` | `Possible hardcoded secret` |
| `file` / `line` | `src/app/auth.ts:42` |
| `rationale` | Why this matters |
| `suggestion` | What to do instead |
| `confidence` | `0.85` |
| `agent` | `security` |
| `evidence_snippet` | Exact added lines used to anchor `line` |

**Ensemble verdict:**

| Verdict | When |
|---|---|
| `request_changes` | Any high or critical finding survives filtering |
| `comment` | Medium/low findings only |
| `approve` | No findings above threshold |

**Outputs:**

- GitHub PR review (summary + inline comments on changed lines)
- `review-report.json` (audit artifact with latency, token usage, estimated cost)
- GitHub Actions artifact (`review-report`)

### GitHub Action sequence

```mermaid
sequenceDiagram
  participant Dev as Developer
  participant GH as GitHub
  participant WF as PR_Review_Workflow
  participant CR as codereview_CLI
  participant OR as OpenRouter
  participant SB as Supabase
  participant Bot as github-actions_bot

  Dev->>GH: Open or update PR
  GH->>WF: pull_request event
  WF->>CR: review-pr with action inputs
  CR->>GH: Fetch PR diff and metadata
  CR->>CR: Select + bundle files
  CR->>SB: Vector similarity search over pre-indexed knowledge
  SB-->>CR: Relevant semantic chunks
  CR->>CR: BM25 fallback if vector results sparse
  CR->>CR: Optional file_read / code_search tools
  CR->>OR: Security and Pattern LLM calls per bundle
  OR-->>CR: Structured findings JSON
  CR->>CR: Ensemble dedupe, fact-check, precision filter
  CR->>GH: Post PR review and inline comments
  Bot-->>Dev: Findings visible on PR diff
  CR->>WF: Upload review-report.json artifact
```

### What is always on vs optional

| Component | Default | If unavailable |
|---|---|---|
| Select + bundle + hard denylist | Always | N/A |
| Changed files (selected) | Always | N/A |
| Language rule packs | On from `languages:` (+ yaml) | Custom rules only |
| Unified vector search | On when `vector.enabled: true` | Falls back to BM25 |
| BM25 neighbors | Fallback when vectors sparse, or legacy mode | N/A |
| Repo tools | On when `use_tools` + LLM key | Skipped |
| LLM agents | On when `LLM_API_KEY` set | Heuristics + packs only |
| Ensemble fact-check | On (`ensemble.fact_check`) when LLM available | Heuristic dedupe/filter only |
| JIRA / Confluence (indexing) | Off | Skipped at index time |
| JIRA / Confluence (live fetch) | Off (`unified_rag: true`) | Skipped at review time |
| GitHub posting | Opt-in CLI (`--post`); Action via `dry_run` | Report still generated locally |

### Core object flow

```text
PullRequestContext
  → select_and_bundle → SelectionResult (selected · bundles · skipped)
  → ContextEngine + optional tools → context_block (markdown)
  → per bundle:
        SecurityAgent  → list[Finding]  (evidence + packs)
        PatternAgent   → list[Finding]
  → EnsembleAgent  → ReviewReport  (dedupe · verify · fact-check · verdict)
  → GitHubClient   → PR review + inline comments (precision-filtered)
```

---

## Example output on GitHub

```text
github-actions[bot] requested changes · reviewed just now

Automated multi-agent review found 8 issue(s): 2 critical, 2 high, 4 medium
Overall confidence: 0.87 · Latency: 6076 ms

1. [CRITICAL / security] Hardcoded API Key — src/app/auth.ts:42
2. [HIGH / security] SQL Injection Risk — src/db/query.py:18

Inline on diff:
  + const api_key = "sk-live-secret";
  > [critical] Hardcoded API Key
  > Use environment variables or a secret manager.
```

See [`docs/images/github-review-example.svg`](docs/images/github-review-example.svg) for a visual mock.

## Features

- **OCR-inspired harness** — select/bundle, hard denylist, evidence line anchoring, language rule packs, repo tools, fact-check
- **Precision defaults** — `posting.min_confidence: 0.70`, `review.effort` (`low|medium|high`), `review.max_bundles`, `ensemble.fact_check`, opt-in CLI posting
- **Unified RAG** — batch-index code + JIRA + Confluence into Supabase; query vectors at review time; stale path cleanup on reindex
- Structured findings: category, severity, file, line, evidence (redacted), rationale, suggestion, confidence
- Hybrid context retrieval: selected files + vector search + BM25 fallback + optional tools
- **Offline demo** — `codereview demo` with mock JIRA/Confluence and in-memory vectors (no APIs)
- Semantic finding dedupe (merges similar titles without collapsing distinct adjacent issues)
- Accurate line numbers from diff hunks + evidence re-location
- Optional **JIRA / Confluence** indexing (fail-open, disabled by default)
- Benchmark eval suite with dual CI gate (5 golden cases; heuristic + posting-threshold summaries)
- Parallel specialist agents with LangGraph fan-out/fan-in and per-bundle review
- Ensemble verifier with optional LLM cross-check and OCR-style fact-check filter
- Works offline with `review-diff` (no GitHub API)
- GitHub Action for company repos (secrets wired as composite-action **inputs**)
- Team rules via `reviewer.yaml` (procedural memory + language packs)
- OpenRouter, OpenAI, or Anthropic for LLM-backed analysis (`EMBEDDING_API_KEY` when needed)

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
export EMBEDDING_API_KEY=sk-...       # optional; required when LLM_PROVIDER=anthropic
export EMBEDDING_MODEL=text-embedding-3-small  # optional override
export GITHUB_TOKEN=ghp_...           # only needed for review-pr
```

Copy [`.env.example`](.env.example) to `.env` for local development.

**OpenRouter (recommended):** use your OpenRouter key as `LLM_API_KEY` with `LLM_PROVIDER=openrouter`. Pick any model from [openrouter.ai/models](https://openrouter.ai/models), e.g. `openai/gpt-4o-mini`, `anthropic/claude-3.5-sonnet`.

### 3. Run the offline demo (no API keys)

```bash
codereview demo
```

This indexes mock JIRA/Confluence fixtures + a golden-case repo into an in-memory vector store, runs the full review pipeline, and writes `demo-report.json`.

### 4. Review a local diff (heuristics only, no API keys)

```bash
codereview review-diff tests/fixtures/sample_diff.patch --repo-root .
```

### 5. Index knowledge into Supabase (unified RAG)

```bash
export SUPABASE_URL=https://your-project-ref.supabase.co
export SUPABASE_SERVICE_ROLE_KEY=eyJ...
export LLM_API_KEY=sk-or-...

codereview index-knowledge --repo owner/repo --repo-root . --config reviewer.yaml
```

Enable vectors in `reviewer.yaml` (`vector.enabled: true`). See [Unified RAG setup](#unified-rag-setup) below.

### 6. Review a GitHub PR

```bash
codereview review-pr owner/repo#123 --repo-root . --output review-report.json
```

By default the CLI does **not** post comments. Pass `--post` to publish (requires `GITHUB_TOKEN`). Use `--dry-run` to force no posting.

## Unified RAG setup

The recommended path: **batch-index all knowledge sources**, then **query vectors at review time**.

1. Create a [Supabase](https://supabase.com) project (or use the CLI: `supabase projects create`)
2. Link and push migrations:

```bash
supabase link --project-ref <your-project-ref>
supabase db push
```

Or run both migrations manually in the SQL editor:

- [`supabase/migrations/001_code_embeddings.sql`](supabase/migrations/001_code_embeddings.sql) — table + RPC
- [`supabase/migrations/002_unified_knowledge_source.sql`](supabase/migrations/002_unified_knowledge_source.sql) — `source` column (code / jira / confluence)

3. Set secrets / env vars:

```bash
export SUPABASE_URL=https://your-project-ref.supabase.co
export SUPABASE_SERVICE_ROLE_KEY=eyJ...
export LLM_API_KEY=sk-or-...
```

4. Enable in `reviewer.yaml`:

```yaml
vector:
  enabled: true
  unified_rag: true
  embedding_model: openai/text-embedding-3-small
  indexing:
    sources: [code, jira, confluence]
  supabase:
    enabled: true
    index_on_review: false   # batch index via index-knowledge
    match_threshold: 0.55
    vector_top_k: 12
```

5. Index knowledge (one-time or on a schedule):

```bash
codereview index-knowledge --repo owner/repo --repo-root .
```

The [`knowledge-index.yml`](.github/workflows/knowledge-index.yml) workflow runs this on push to `main` and daily at 06:00 UTC when repo secrets are configured.

If Supabase is unavailable, the agent **falls back to changed files + BM25** automatically.

### Review harness (`review:` in `reviewer.yaml`)

```yaml
ensemble:
  llm_verify: true
  fact_check: true

posting:
  min_confidence: 0.70
  max_inline_comments: 25

review:
  effort: medium              # low | medium | high — fact-check rounds
  use_tools: true             # file_read / code_search when LLM is available
  precision_mode: true        # post only above severity + min_confidence
  max_files_per_bundle: 8
  max_bundles: 4              # remainder capped; overflow skipped
  max_diff_chars_per_file: 40000
  # rule_packs: [python, typescript, javascript, yaml]  # defaults to languages + yaml
```

See [`reviewer.example.yaml`](reviewer.example.yaml) for a full template.

### Supabase schema

| Object | Role |
|---|---|
| `code_embeddings` table | Chunked content + 1536-dim vectors per `owner/repo`, tagged by `source` |
| `match_code_embeddings()` | RPC for cosine-similarity search filtered by repo |

## Legacy: BM25 + index-on-review

By default, context retrieval uses **BM25-lite** over changed files and neighbors when vectors are disabled (zero infra).

To use the v0.3 incremental indexing path instead of unified RAG:

```yaml
vector:
  enabled: true
  unified_rag: false
  supabase:
    index_on_review: true
```

On each review, changed files are embedded and stored; similar chunks are retrieved for the PR query. Over time this builds a **per-repo semantic memory** in Supabase.

## JIRA / Confluence indexing (opt-in, fail-open)

With unified RAG, JIRA and Confluence are **indexed in batch** (not fetched live at review time).

```yaml
external_context:
  enabled: true
  jira:
    enabled: true
    base_url: yourcompany.atlassian.net
    projects: [CP]
  confluence:
    enabled: true
```

PR convention:

```markdown
## JIRA
[CP-123] Add session export

## Confluence
https://yourcompany.atlassian.net/wiki/spaces/ENG/pages/123456/Design
```

Secrets (GitHub Actions or local):

```bash
export ATLASSIAN_EMAIL=you@company.com
export ATLASSIAN_API_TOKEN=...
export ATLASSIAN_DOMAIN=yourcompany.atlassian.net
```

If credentials are missing or Atlassian is down, indexing **skips those sources** and continues with repo code.

For live fetch at review time (legacy), set `vector.unified_rag: false` and `external_context.enabled: true`.

## Benchmark evaluation

Measure precision/recall on labeled golden diffs:

```bash
codereview eval --benchmark-dir benchmarks/golden --output benchmarks/results.json
```

Use this to tune `severity_threshold`, compare OpenRouter models, and catch regressions when changing prompts or agents. CI enforces on **5** golden cases:

- Heuristic gate: recall ≥ 0.9, precision ≥ 0.65
- Posting-threshold gate: production `severity_threshold` / `min_confidence` / `precision_mode` with LLM verify/fact-check off (deterministic)

See [`benchmarks/README.md`](benchmarks/README.md) to add more cases (target 20+ real PRs over time).

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
          supabase_url: ${{ secrets.SUPABASE_URL }}
          supabase_service_role_key: ${{ secrets.SUPABASE_SERVICE_ROLE_KEY }}
          atlassian_email: ${{ secrets.ATLASSIAN_EMAIL }}
          atlassian_api_token: ${{ secrets.ATLASSIAN_API_TOKEN }}
          atlassian_domain: ${{ secrets.ATLASSIAN_DOMAIN }}
```

Pass secrets as **action inputs** (composite actions cannot read `secrets.*` directly):

| Input / Secret | Purpose |
|---|---|
| `llm_api_key` / `LLM_API_KEY` | OpenRouter/OpenAI/Anthropic (review + embeddings) |
| `supabase_url` / `SUPABASE_URL` | Supabase project URL for vector context |
| `supabase_service_role_key` / `SUPABASE_SERVICE_ROLE_KEY` | Supabase service role for embedding upsert/search |
| `atlassian_email` / `ATLASSIAN_EMAIL` | JIRA/Confluence API user (optional) |
| `atlassian_api_token` / `ATLASSIAN_API_TOKEN` | Atlassian API token (optional) |
| `atlassian_domain` / `ATLASSIAN_DOMAIN` | e.g. `yourcompany.atlassian.net` (optional) |
| `EMBEDDING_API_KEY` (env) | Dedicated OpenAI-compatible embeddings key when using Anthropic for chat |

Reviews post as **github-actions[bot]** using the default `GITHUB_TOKEN`.

### Company setup checklist

1. Add `reviewer.yaml` with your team's ignore paths and custom rules
2. Create repo secrets: `LLM_API_KEY`, `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY`
3. Push Supabase migrations (`001` + `002`) and run `codereview index-knowledge` once
4. Enable the PR review workflow on `pull_request`
5. Enable `knowledge-index.yml` for scheduled re-indexing (optional)
6. Start with `dry_run: true` for one sprint, then switch to live posting
7. Tune `severity_threshold`, `posting.min_confidence`, `review.effort`, `review.max_bundles`, and `ensemble.fact_check`
8. Run `codereview eval` periodically to track precision/recall

### Live example

This project powers PR reviews on [CopilotPulse](https://github.com/cipheraxat/CopilotPulse) with Supabase vectors enabled.

## CLI

```bash
codereview review-pr owner/repo#123 [--post] [--no-post] [--dry-run] [--config reviewer.yaml]
codereview review-diff path/to/changes.patch [--title "My change"]
codereview index-knowledge --repo owner/repo [--sources code,jira,confluence]
codereview demo [--diff path/to/diff.patch] [--fixtures-dir tests/fixtures/knowledge]
codereview eval [--benchmark-dir benchmarks/golden] [--output benchmarks/results.json]
codereview version
```

## Output

- Terminal summary table
- `review-report.json` audit artifact with metrics (latency, token usage, estimated cost)

## Project layout

```text
src/codereview/
  cli.py                 # review-pr, review-diff, index-knowledge, demo, eval
  graph.py               # LangGraph: select → context → agents → ensemble
  file_selection.py      # deny/ignore/size gates + directory bundles + remainder cap
  path_utils.py          # hard denylist + robust ignore_glob matching
  rule_packs.py          # language packs (python / ts / js / go / yaml)
  review_tools.py        # file_read / code_search context enrichment
  context_engine.py      # unified RAG + BM25 fallback
  knowledge_indexer.py   # batch index code + JIRA + Confluence (+ stale delete)
  finding_dedupe.py      # semantic dedupe across agents
  diff_utils.py          # line numbers + evidence anchoring + redaction
  chunking.py            # overlap-safe text chunking for embeddings
  demo_pipeline.py       # offline demo (mock knowledge + in-memory vectors)
  mock_knowledge.py      # mock JIRA/Confluence fixtures loader
  in_memory_vector_store.py
  local_embeddings.py
  github_client.py       # PR fetch + review posting
  external_context.py    # JIRA / Confluence fetchers (fail-open)
  embeddings.py          # OpenAI-compatible embeddings (+ EMBEDDING_API_KEY)
  vector_store.py        # Supabase pgvector upsert + search + retries
  eval.py                # precision/recall + posting-threshold gate
  agents/
    security.py
    pattern.py
    ensemble.py          # dedupe + LLM verify + fact-check + effort rounds
supabase/migrations/
  001_code_embeddings.sql
  002_unified_knowledge_source.sql
tests/fixtures/knowledge/  # mock JIRA/Confluence JSON for offline demo
benchmarks/golden/         # 5 labeled PR diffs for eval + CI gate
.github/workflows/
  pr-review.yml
  knowledge-index.yml
  self-test.yml          # pytest + dual benchmark gate
action/
  action.yml             # secrets → inputs for composite action
docs/images/
  architecture.svg
  data-flow.svg
  langgraph-pipeline.svg
  github-review-example.svg
```

## Resume bullet

Built a multi-agent GitHub PR reviewer (LangGraph) with unified RAG and an OCR-inspired precision harness — select/bundle + hard denylist, language rule packs, evidence anchoring/redaction, repo tools, fact-check, and dual eval gates — posting structured inline comments via GitHub Actions.

## Roadmap (v2)

- Webhook ingress service (FastAPI + Redis queue)
- Episodic review memory across PRs (review history in Supabase)
- TimescaleDB observability for cost/latency trends

## License

MIT
