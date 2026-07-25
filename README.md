# Repository Metrics

Tools for analyzing repository metrics and statistics.

## Setup

This project uses [uv](https://github.com/astral-sh/uv) for dependency management.

```bash
# Install uv if you don't have it
curl -LsSf https://astral.sh/uv/install.sh | sh

# Install dependencies (to be run inside the directory where pyproject.toml is)
uv sync --dev
```

## Tools

### PR Report Generator

Generates comprehensive PR reports for openshift/hypershift, openshift-eng/ai-helpers, openshift/enhancements, and openshift/release repositories. Defaults to the last 7 days but supports custom date ranges.

**Features:**

- Covers multiple repositories:
  - openshift/hypershift (all PRs)
  - openshift-eng/ai-helpers (PRs by HyperShift contributors)
  - openshift/enhancements (PRs by HyperShift contributors)
  - openshift/release (HyperShift-related paths only)
- Uses GitHub GraphQL API for efficient PR fetching
- Parallel async API calls with aiohttp
- Filters PRs by merge date
- Extracts complete PR timeline (draft→ready→merge)
- Identifies reviewers and approvers
- Groups PRs by OCPSTRAT parent
- Generates timing metrics and statistics
- Auto-generates OCPSTRAT impact statements
- Textual TUI for interactive PR categorization (Deep/Light/Ignore)
- Direct LLM analysis via Anthropic/Vertex API (Sonnet) for PR code review
- Generates Material-styled blog posts for docs site via exec into Claude Code
- Fetches pricing from LiteLLM database for accurate cost reporting
- Supports Jira Cloud REST API integration (direct or MCP fallback)
- PR scoring system for prioritizing deep analysis

**Performance:** ~2 seconds for basic report (90-180x faster than previous agent-based approach)

**Usage:**

```bash
# Basic report (last 7 days)
uv run pr-report

# Monthly report with date range
uv run pr-report 2026-06-23 --end 2026-07-22 --output-dir ~/reports/2026-07

# Full pipeline: interactive selection + LLM analysis + blog post
uv run pr-report 2026-06-23 --end 2026-07-22 \
    --output-dir ~/reports/2026-07 --resume --select --analyze --blog-data --blog
```

**Flags:**

- `--resume`: Skip re-fetching data if output files already exist
- `--select`: Launch Textual TUI for PR categorization (D=Deep, L=Light, I=Ignore)
- `--analyze`: Run LLM analysis on selected PRs via direct Sonnet API calls
- `--blog-data`: Generate blog_data.json with contributor tables and metrics
- `--blog`: Exec into a clean Claude Code session for blog writing (requires `--analyze` and `--blog-data` data)
- `--score`: Output ranked PR list by importance
- `--output-dir DIR`: Directory for output files

**Environment Variables:**

| Variable | Required | Description |
|---|---|---|
| `GITHUB_TOKEN` | Yes | GitHub token (auto-detected from `gh auth token` if not set) |
| `ANTHROPIC_VERTEX_PROJECT_ID` | For --analyze (Vertex) | Google Cloud project ID for Vertex AI |
| `CLOUD_ML_REGION` | No | Vertex AI region (default: us-east5) |
| `ANTHROPIC_API_KEY` | For --analyze (direct) | Anthropic API key — used only if Vertex project is not set |
| `JIRA_EMAIL` | No | Atlassian account email for Jira Cloud integration |
| `JIRA_TOKEN` | No | Jira Cloud API token |

**Output:**

The tool generates multiple output files depending on flags used:

- `weekly_pr_report_fast.md` — data report with metrics
- `hypershift_pr_details_fast.json` — raw PR data
- `pr_scored.json` — ranked PR list (with --score)
- `pr_deep/*.json` — per-PR data with diffs (with --select)
- `pr_deep/*_analysis.json` — per-PR LLM analysis (with --analyze)
- `pr_deep_aggregated.json` — aggregated analysis (with --analyze)
- `blog_data.json` — contributor tables and metrics (with --blog-data)

### AI-Assisted Commits Analyzer

Analyzes git commits to identify those assisted by AI tools (Claude, GPT, etc.).

**Usage:**

```bash
# Run with default (last 2 weeks)
uv run python ai_assisted_commits.py

# Analyze last N commits
uv run python ai_assisted_commits.py -n 100

# Analyze since relative date
uv run python ai_assisted_commits.py --since "1 month ago"

# Analyze since specific date
uv run python ai_assisted_commits.py --since "2025-09-01"
```

**Note:** You cannot specify both `--since` and `-n/--max-count` at the same time.

**Output:**

```text
=== AI-Assisted Commits Report (2 weeks ago) ===

Absolute Numbers:
  Total commits: 48
    - Merge commits: 25
    - Non-merge commits: 23
  AI-assisted commits: 13
    - AI-assisted non-merge: 11
    - AI-assisted merge: 2

Percentages:
  Overall AI-assisted: 27.1% (13/48)
  Non-merge AI-assisted: 47.8% (11/23)
```

## Testing

### Unit Tests

```bash
# Run all tests
uv run pytest

# Run with verbose output
uv run pytest -v

# Run specific test files
uv run pytest test_ai_assisted_commits.py -v
uv run pytest test_weekly_pr_report.py -v
```

### PR Analysis Evals

The `evals/` directory contains [agent-eval-harness](https://github.com/opendatahub-io/agent-eval-harness) test cases that validate the LLM analysis quality. Four real-world PR fixtures test breaking change detection, API change detection, impact level accuracy, and light-mode (no-diff) analysis.

```bash
# Install the eval harness (one-time)
pip install agent-eval-harness
# — or if you have the ai-helpers repo cloned:
pip install -e /path/to/ai-helpers

# Run the eval suite
agent-eval-harness run contrib/repo_metrics/evals/eval-pr-analysis.yaml
```

See [`evals/README.md`](evals/README.md) for test case details, judge descriptions, and pass thresholds.
