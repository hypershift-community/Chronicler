"""Configuration module for Chronicler.

Loads TOML config, merges with defaults, and exposes typed dataclasses.
The sample config file is rendered from the dataclass schema so it never
drifts from the actual defaults.
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Dict, List, Optional, Union

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib

from chronicler.dirs import config_path


def _f(default, desc: str, **kwargs):
    """Shorthand for a dataclass field with a description in metadata."""
    meta = {"desc": desc}
    if isinstance(default, (list, dict)):
        return field(default_factory=lambda d=default: list(d) if isinstance(d, list) else dict(d),
                     metadata=meta, **kwargs)
    if callable(default) and not isinstance(default, (str, int, float, bool, type(None))):
        return field(default_factory=default, metadata=meta, **kwargs)
    return field(default=default, metadata=meta, **kwargs)


# ---------------------------------------------------------------------------
# Dataclass hierarchy
# ---------------------------------------------------------------------------

@dataclass
class RepoSecondary:
    name: str = _f("org/other-repo", "GitHub owner/name")
    filter: Optional[str] = _f(None, '"team-only" to include only team members\' PRs')
    path_filter: Optional[str] = _f(None, "Keyword for two-pass file-path filtering")


@dataclass
class ReposConfig:
    primary: str = _f("openshift/hypershift", "Primary GitHub repository (owner/name)")
    secondary: List[RepoSecondary] = field(
        default_factory=lambda: [
            RepoSecondary(name="openshift-eng/ai-helpers", filter="team-only"),
            RepoSecondary(name="openshift/enhancements", filter="team-only"),
            RepoSecondary(name="openshift/release", path_filter="hypershift"),
        ],
        metadata={"desc": "Additional repositories to scan"},
    )


@dataclass
class JiraCustomFields:
    sfdc_cases_total: str = _f("customfield_10978", "Custom field ID for SFDC cases counter")
    sfdc_cases_links: str = _f("customfield_10979", "Custom field ID for SFDC cases links")
    sfdc_cases_open: str = _f("customfield_10980", "Custom field ID for SFDC open cases")


@dataclass
class JiraConfig:
    url: str = _f("https://redhat.atlassian.net", "Jira instance base URL")
    ticket_prefixes: List[str] = _f(
        ["OCPBUGS", "CNTRLPLANE", "OCPSTRAT", "RFE", "HOSTEDCP"],
        "Ticket prefixes to recognize in PR bodies",
    )
    grouping_prefix: str = _f("OCPSTRAT", "Prefix used for grouping PRs by initiative")
    bug_prefix: str = _f("OCPBUGS", "Prefix that identifies bug tickets")
    custom_fields: JiraCustomFields = field(
        default_factory=JiraCustomFields,
        metadata={"desc": "Jira custom field IDs"},
    )


@dataclass
class OwnersConfig:
    file: str = _f("OWNERS_ALIASES", "Path to the owners/aliases file in the primary repo")
    include_groups: List[str] = _f(
        ["core-approvers", "core-reviewers", "konflux-approvers"],
        "Groups to include as team members",
    )
    exclude_groups: List[str] = _f(
        ["gcp-reviewers"],
        "Groups to exclude from team membership",
    )


@dataclass
class RosterConfig:
    """Explicit list of team member GitHub logins."""
    members: List[str] = _f(
        ["alice", "bob", "carol"],
        "GitHub logins of team members",
    )


@dataclass
class NoTeamConfig:
    """Sentinel: no team filtering configured."""
    pass


TeamConfig = Union[OwnersConfig, RosterConfig, NoTeamConfig]


@dataclass
class BotsConfig:
    logins: List[str] = _f(
        ["coderabbitai", "hypershift-jira-solve-ci", "dependabot"],
        "GitHub logins to treat as bots",
    )
    patterns: List[str] = _f(
        ["-bot", "-robot", "[bot]"],
        "Substrings in a login that mark it as a bot",
    )


@dataclass
class LlmConfig:
    model: str = _f("claude-sonnet-5", "Model identifier for blog generation")
    vertex_region: str = _f("us-east5", "Google Cloud region for Vertex AI")


@dataclass
class BlogConfig:
    output_dir: str = _f("docs/content/blog", "Directory for generated blog posts")
    format: str = _f("mkdocs-material", "Blog output format")


@dataclass
class ChroniclerConfig:
    project_name: str = _f("HyperShift", "Human-readable project name")
    repos: ReposConfig = field(default_factory=ReposConfig, metadata={"desc": "Repository settings"})
    jira: JiraConfig = field(default_factory=JiraConfig, metadata={"desc": "Jira settings"})
    team: TeamConfig = field(default_factory=OwnersConfig, metadata={"desc": "Team membership"})
    bots: BotsConfig = field(default_factory=BotsConfig, metadata={"desc": "Bot detection"})
    llm: LlmConfig = field(default_factory=LlmConfig, metadata={"desc": "LLM settings"})
    blog: BlogConfig = field(default_factory=BlogConfig, metadata={"desc": "Blog output settings"})

    @property
    def ticket_regex(self) -> re.Pattern:
        prefixes = "|".join(re.escape(p) for p in self.jira.ticket_prefixes)
        return re.compile(rf"\b(?:{prefixes})-\d+\b")

    @property
    def primary_owner(self) -> str:
        return self.repos.primary.split("/")[0]

    @property
    def primary_name(self) -> str:
        return self.repos.primary.split("/")[1]

    @property
    def repo_map(self) -> Dict[str, RepoSecondary]:
        return {r.name: r for r in self.repos.secondary}

    @property
    def jira_fields_csv(self) -> str:
        cf = self.jira.custom_fields
        base = "summary,description,parent,issuetype,issuelinks,labels,priority,status"
        return f"{base},{cf.sfdc_cases_total},{cf.sfdc_cases_links},{cf.sfdc_cases_open}"


# ---------------------------------------------------------------------------
# Sample config renderer
# ---------------------------------------------------------------------------

def _toml_value(val) -> str:
    """Format a Python value as a TOML literal."""
    if isinstance(val, bool):
        return "true" if val else "false"
    if isinstance(val, str):
        return f'"{val}"'
    if isinstance(val, (int, float)):
        return str(val)
    if isinstance(val, list):
        inner = ", ".join(_toml_value(v) for v in val)
        return f"[{inner}]"
    return str(val)


def _render_fields(instance, section: str, lines: list) -> None:
    """Render scalar fields of a dataclass as commented TOML key = value lines."""
    for f in fields(instance):
        val = getattr(instance, f.name)
        desc = f.metadata.get("desc", "")

        if val is None:
            continue
        if hasattr(val, "__dataclass_fields__"):
            subsection = f"{section}.{f.name}" if section else f.name
            lines.append("")
            if desc:
                lines.append(f"# {desc}")
            lines.append(f"# [{subsection}]")
            _render_fields(val, subsection, lines)
        elif isinstance(val, list) and val and hasattr(val[0], "__dataclass_fields__"):
            for item in val:
                subsection = f"{section}.{f.name}" if section else f.name
                lines.append("")
                if desc:
                    lines.append(f"# {desc}")
                    desc = ""
                lines.append(f"# [[{subsection}]]")
                _render_fields(item, "", lines)
        else:
            if desc:
                lines.append(f"# {desc}")
            lines.append(f"# {f.name} = {_toml_value(val)}")


def render_sample_config() -> str:
    """Render a fully-commented sample config from the dataclass defaults."""
    lines = [
        "# Chronicler configuration",
        "# All values below are commented out and show the defaults.",
        "# Uncomment and edit to override.",
    ]
    cfg = ChroniclerConfig()

    # project_name maps to [project] name = ...
    lines.append("")
    lines.append("# [project]")
    lines.append(f"# {fields(cfg)[0].metadata.get('desc', '')}")
    lines.append(f"# name = {_toml_value(cfg.project_name)}")

    # Remaining top-level fields are all nested dataclasses
    for f in fields(cfg):
        if f.name == "project_name":
            continue
        val = getattr(cfg, f.name)
        desc = f.metadata.get("desc", "")
        lines.append("")
        if desc:
            lines.append(f"# {desc}")

        if f.name == "team":
            _render_team_sample(lines)
        else:
            lines.append(f"# [{f.name}]")
            _render_fields(val, f.name, lines)

    return "\n".join(lines) + "\n"


def _render_team_sample(lines: list) -> None:
    """Render the team section showing both union variants."""
    # Option A: OWNERS file (default)
    lines.append("# Option A — derive roster from an OWNERS file (default):")
    lines.append("# [team.owners]")
    _render_fields(OwnersConfig(), "team.owners", lines)
    lines.append("#")
    # Option B: explicit roster
    lines.append("# Option B — list members explicitly:")
    lines.append("# [team]")
    _render_fields(RosterConfig(), "team", lines)
    lines.append("#")
    # Option C: no team filtering
    lines.append("# Option C — no team filtering (include all contributors):")
    lines.append("# [team]")
    lines.append("# # leave empty")


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------

def _build_secondary(raw: list) -> List[RepoSecondary]:
    return [
        RepoSecondary(
            name=entry["name"],
            filter=entry.get("filter"),
            path_filter=entry.get("path_filter"),
        )
        for entry in raw
    ]


def _build_config(data: dict) -> ChroniclerConfig:
    project_name = data.get("project", {}).get("name", "HyperShift")

    repos_raw = data.get("repos", {})
    repos = ReposConfig(
        primary=repos_raw.get("primary", ReposConfig.primary),
        secondary=(
            _build_secondary(repos_raw["secondary"])
            if "secondary" in repos_raw
            else ReposConfig().secondary
        ),
    )

    jira_raw = data.get("jira", {})
    cf_raw = jira_raw.get("custom_fields", {})
    jira = JiraConfig(
        url=jira_raw.get("url", JiraConfig.url),
        ticket_prefixes=jira_raw.get("ticket_prefixes", JiraConfig().ticket_prefixes),
        grouping_prefix=jira_raw.get("grouping_prefix", JiraConfig.grouping_prefix),
        bug_prefix=jira_raw.get("bug_prefix", JiraConfig.bug_prefix),
        custom_fields=JiraCustomFields(
            sfdc_cases_total=cf_raw.get("sfdc_cases_total", JiraCustomFields.sfdc_cases_total),
            sfdc_cases_links=cf_raw.get("sfdc_cases_links", JiraCustomFields.sfdc_cases_links),
            sfdc_cases_open=cf_raw.get("sfdc_cases_open", JiraCustomFields.sfdc_cases_open),
        ),
    )

    team_raw = data.get("team", {})
    owners_raw = team_raw.get("owners")
    members_raw = team_raw.get("members")
    if owners_raw is not None:
        team: TeamConfig = OwnersConfig(
            file=owners_raw.get("file", OwnersConfig.file),
            include_groups=owners_raw.get("include_groups", OwnersConfig().include_groups),
            exclude_groups=owners_raw.get("exclude_groups", OwnersConfig().exclude_groups),
        )
    elif members_raw is not None:
        team = RosterConfig(members=members_raw)
    elif "team" in data:
        team = NoTeamConfig()
    else:
        team = OwnersConfig()

    bots_raw = data.get("bots", {})
    bots = BotsConfig(
        logins=bots_raw.get("logins", BotsConfig().logins),
        patterns=bots_raw.get("patterns", BotsConfig().patterns),
    )

    llm_raw = data.get("llm", {})
    llm = LlmConfig(
        model=llm_raw.get("model", LlmConfig.model),
        vertex_region=llm_raw.get("vertex_region", LlmConfig.vertex_region),
    )

    blog_raw = data.get("blog", {})
    blog = BlogConfig(
        output_dir=blog_raw.get("output_dir", BlogConfig.output_dir),
        format=blog_raw.get("format", BlogConfig.format),
    )

    return ChroniclerConfig(
        project_name=project_name,
        repos=repos,
        jira=jira,
        team=team,
        bots=bots,
        llm=llm,
        blog=blog,
    )


def write_sample_config(path: Optional[Path] = None) -> Path:
    """Write a sample config derived from the schema. Returns the path written."""
    dest = path or config_path()
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(render_sample_config())
    return dest


def load_config(path: Optional[Path] = None) -> ChroniclerConfig:
    """Load config from a TOML file.

    If *path* is given explicitly (via --config), it must exist or ValueError
    is raised. If *path* is None, the default location is used; if that file
    doesn't exist a sample config is created there and defaults are returned.
    """
    explicit = path is not None
    resolved = Path(path) if explicit else config_path()

    if not resolved.exists():
        if explicit:
            raise ValueError(f"Config file not found: {resolved}")
        write_sample_config(resolved)
        return ChroniclerConfig()

    with open(resolved, "rb") as f:
        data = tomllib.load(f)

    if not data:
        return ChroniclerConfig()

    return _build_config(data)
