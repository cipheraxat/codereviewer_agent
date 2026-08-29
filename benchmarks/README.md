# Benchmark evaluation

Measures **precision** and **recall** of the review agent against labeled golden PR diffs.

## Run

```bash
codereview eval --benchmark-dir benchmarks/golden --output benchmarks/results.json
```

## Why this is useful

- Tune `reviewer.yaml` thresholds without spamming developers
- Compare OpenRouter models on the same labeled set
- Catch regressions when changing prompts or agents

## Add a golden case

```
benchmarks/golden/case_xxx/
  diff.patch          # unified diff
  labels.json         # expected findings
  repo/               # optional local fixture files for context engine
```

### labels.json

```json
{
  "expected": [
    {
      "title_contains": "hardcoded secret",
      "severity": "high",
      "file": "src/app/auth.py",
      "category": "security"
    }
  ],
  "should_not_find": ["unrelated issue title fragment"]
}
```

Expand to 20+ cases by adding real PR diffs from your repos (sanitized).
