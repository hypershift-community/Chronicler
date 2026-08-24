# Chronicler

> From commits to chronicles — automated progress reporting

Chronicler scans merged GitHub PRs across multiple repositories, enriches them
with Jira ticket data (epics, features, SFDC customer cases), scores them by
importance, and produces polished blog posts — all from a single CLI command.

## Features

- **Multi-repo PR scanning** — configurable list of GitHub repositories with
  per-repo filters: all PRs, team-members only, or path-based keyword filtering
- **Jira integration** — fetches linked tickets via REST API, walks the
  Story → Epic → Feature hierarchy, and extracts SFDC customer-case data
- **PR scoring** — ranks PRs by importance (priority, customer impact, labels)
  for prioritizing deep analysis
- **Interactive TUI** — Textual-based terminal UI for tagging PRs as
  Deep / Light / Ignore with keyboard shortcuts
- **LLM analysis** — code-review of PR diffs via Claude Sonnet
  (Anthropic direct or Vertex AI), with per-model cost tracking from
  the LiteLLM pricing database
- **Blog generation** — produces Material for MkDocs-styled blog posts with
  contributor tables, metrics cards, and review stats
- **Resume support** — skip re-fetching repos that succeeded; re-fetch only
  those that failed
- **Team membership** — derive roster from an `OWNERS_ALIASES` file, list
  members explicitly, or disable team filtering entirely
- **Bot filtering** — configurable bot-login patterns to exclude automated PRs
- **Config auto-generation** — writes a fully-commented sample `config.toml`
  from the dataclass schema on first run, so config never drifts from code

## Installation

Chronicler uses [uv](https://github.com/astral-sh/uv) for dependency
management.

```bash
# Install uv (if needed)
curl -LsSf https://astral.sh/uv/install.sh | sh

# Clone and install
git clone https://github.com/hypershift-community/Chronicler.git
cd Chronicler
uv sync
```

After `uv sync`, the `chronicler` entry point is available:

```bash
uv run chronicler --help
```

## Quick start

```bash
# Report for the last 7 days (default)
uv run chronicler

# Report for a specific date range
uv run chronicler 2026-07-01 --end 2026-07-31 --output-dir ~/reports/july

# Full pipeline: fetch → select → analyze → blog
uv run chronicler 2026-07-01 --end 2026-07-31 \
    --output-dir ~/reports/july \
    --resume --select --analyze --blog-data --blog
```

## CLI reference

```
chronicler [since_date] [OPTIONS]
```

| Flag | Description |
|------|-------------|
| `since_date` | Start date (`YYYY-MM-DD`). Default: 7 days ago |
| `--end DATE` | End date (`YYYY-MM-DD`). Default: today |
| `--config PATH` | Path to TOML config file (default: platform-specific, see below) |
| `--output-dir DIR` | Directory for output files (default: `/tmp`) |
| `--resume` | Skip re-fetching repos that succeeded on a previous run |
| `--select` | Launch interactive TUI for PR categorisation (D=Deep, L=Light, I=Ignore) |
| `--analyze` | Run LLM analysis on selected PRs via Claude Sonnet |
| `--blog-data` | Generate `blog_data.json` with contributor tables and metrics |
| `--blog` | Exec into a clean Claude Code session for blog writing |
| `--score` | Output ranked PR list by importance |
| `--score-limit N` | Number of PRs in scored output (default: 20) |
| `--deep PR [PR ...]` | Fetch diffs for specific PRs (`owner/repo#number` format) |

## Configuration

Chronicler uses a TOML config file. On first run, a fully-commented sample
is auto-generated from the dataclass schema and written to the
platform-specific config directory (see [Platform directories](#platform-directories)).

Pass `--config PATH` to use a custom location. If the default path does not
exist, Chronicler creates it with sensible defaults.

### Example config

```toml
[project]
name = "MyProject"

[[repos]]
name = "org/main-repo"
filter = "all"                        # "all" | "team"
description = "The primary repository"

[[repos]]
name = "org/enhancements"
filter = "team"                       # only PRs by team members
category = "enhancement"
description = "Design proposals"

[[repos]]
name = "org/release"
filter = "all"
path_filter = "myproject"             # two-pass file-path keyword filter
category = "ci"
description = "CI/CD job definitions"

[jira]
url = "https://your-instance.atlassian.net"
ticket_prefixes = ["PROJ", "BUGS"]    # recognised in PR titles/bodies
grouping_prefix = "PROJ"              # used for initiative grouping
bug_prefix = "BUGS"                   # identifies bug tickets

# Team membership — pick ONE of the three options:

# Option A: derive from an OWNERS_ALIASES YAML file (default)
[team.owners]
file = "OWNERS_ALIASES"
include_groups = ["core-approvers", "core-reviewers"]
exclude_groups = ["gcp-reviewers"]

# Option B: list members explicitly
# [team]
# members = ["alice", "bob", "carol"]

# Option C: no team filtering (include all contributors)
# [team]
# # leave empty

[bots]
logins = ["dependabot", "renovate"]
patterns = ["-bot", "[bot]"]

[llm]
model = "claude-sonnet-5"
vertex_region = "us-east5"

[blog]
output_dir = "docs/content/blog"
format = "mkdocs-material"
```

## Environment variables

| Variable | Required | Description |
|----------|----------|-------------|
| `GITHUB_TOKEN` | Yes | GitHub PAT. Auto-detected from `gh auth token` if not set |
| `ANTHROPIC_VERTEX_PROJECT_ID` | For `--analyze` (Vertex) | Google Cloud project ID for Vertex AI |
| `CLOUD_ML_REGION` | No | Vertex AI region (default: `us-east5`) |
| `ANTHROPIC_API_KEY` | For `--analyze` (direct) | Anthropic API key — used only if Vertex project is not set |
| `JIRA_EMAIL` | For Jira | Atlassian account email |
| `JIRA_TOKEN` | For Jira | Jira Cloud API token |
| `JIRA_URL` | No | Override the Jira base URL from config |

## Platform directories

Chronicler stores its config file at a platform-native location using
[platformdirs](https://github.com/platformdirs/platformdirs). `XDG_*`
environment variables are respected on all platforms.

| Platform | Config path |
|----------|-------------|
| Linux | `~/.config/chronicler/config.toml` |
| macOS | `~/Library/Application Support/chronicler/config.toml` |
| Windows | `%LOCALAPPDATA%\chronicler\config.toml` |

Data and cache directories follow the same convention under `chronicler/`.

## Output files

Depending on the flags used, Chronicler produces:

| File | Flag | Description |
|------|------|-------------|
| `pr_details.json` | *(always)* | Raw PR data |
| `weekly_pr_report_fast.md` | *(always)* | Data report with metrics |
| `pr_scored.json` | `--score` | Ranked PR list |
| `pr_deep/*.json` | `--select` | Per-PR data with diffs |
| `pr_deep/*_analysis.json` | `--analyze` | Per-PR LLM analysis |
| `pr_deep_aggregated.json` | `--analyze` | Aggregated analysis |
| `blog_data.json` | `--blog-data` | Contributor tables and metrics |

## Development

### Running tests

```bash
uv sync --dev
uv run pytest
uv run pytest -v          # verbose output
```

### PR analysis evals

The `evals/` directory contains
[agent-eval-harness](https://github.com/opendatahub-io/agent-eval-harness)
test cases that validate LLM analysis quality. Four real-world PR fixtures
test breaking-change detection, API-change detection, impact-level accuracy,
and light-mode (no-diff) analysis.

```bash
pip install agent-eval-harness
agent-eval-harness run evals/eval-pr-analysis.yaml
```

See [`evals/README.md`](evals/README.md) for test-case details, judge
descriptions, and pass thresholds.

## License

Apache License 2.0 — see [LICENSE](LICENSE) for details.
