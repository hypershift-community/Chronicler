# PR Analysis Evaluations

This directory contains agent-eval-harness test cases for validating the quality of PR analysis performed by `weekly_pr_report.py`'s `analyze_prs_with_llm()` method.

## Structure

```
evals/
├── eval-pr-analysis.yaml     # Eval configuration and judges
└── cases/
    └── pr-analysis/
        ├── breaking-api-change/   # API + behavioral change (Azure scale-from-zero)
        ├── routine-bugfix/        # Documentation-only PR
        ├── api-addition/          # Additive API change (OVN subnets)
        └── light-mode-feature/    # Metadata-only analysis (no diff)
```

## Test Cases

### 1. breaking-api-change (PR 8337)
- **Description**: Azure scale-from-zero support with new InstanceTypeProvider interface
- **Expected**: API changes + breaking changes + high impact
- **Source**: Real PR from 2026-07-22 weekly report

### 2. routine-bugfix (PR 8371)
- **Description**: Documentation PR adding cluster capabilities guide
- **Expected**: No API/breaking changes + low impact
- **Source**: Real PR from 2026-07-22 weekly report

### 3. api-addition (PR 8249)
- **Description**: Adds optional V4/V6InternalSubnet fields to OVN config
- **Expected**: API changes (additive only) + medium impact
- **Source**: Real PR from 2026-07-22 weekly report

### 4. light-mode-feature (PR 8337, no diff)
- **Description**: Same PR as case 1, but with diff section removed
- **Expected**: Same outcomes as case 1, but based on metadata/description only
- **Purpose**: Tests whether analysis can detect breaking changes from PR description alone

## Judges

### Python Checks
1. **valid_output_json**: All 13 fields present with correct types
2. **breaking_changes_detection**: `breaking_changes` non-empty iff expected
3. **api_changes_detection**: `api_changes` matches expected
4. **impact_level_accuracy**: `impact_level` in acceptable range

### LLM Judge
5. **analysis_quality**: 1-5 scale assessing summary depth, actual_changes specificity, impact_statement relevance

## Running the Eval

```bash
# From repo root
agent-eval-harness run contrib/repo_metrics/evals/eval-pr-analysis.yaml
```

## Expected Analysis Schema

The analysis output must be a JSON file with these fields:

```json
{
  "repo": "owner/repo",
  "number": 1234,
  "author": "github-username",
  "summary": "One sentence describing actual code changes",
  "actual_changes": ["Change 1", "Change 2"],
  "alignment_with_description": "matches" | "partial" | "misleading",
  "breaking_changes": ["Breaking change 1"] | [],
  "test_coverage": "Description of test changes" | "none",
  "api_changes": true | false,
  "files_changed": {"total": 5, "by_type": {"go": 3, "yaml": 2}},
  "notable_observations": ["Observation 1"],
  "impact_level": "high" | "medium" | "low",
  "impact_statement": "One sentence business/user impact"
}
```

## Thresholds

- `valid_output_json`: 100% pass rate
- `breaking_changes_detection`: 90% pass rate
- `api_changes_detection`: 90% pass rate
- `impact_level_accuracy`: 85% pass rate
- `analysis_quality`: mean score ≥ 3.5
