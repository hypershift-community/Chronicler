"""Tests for chronicler.config module."""

import os
import textwrap
from pathlib import Path
from unittest.mock import patch

import pytest

from chronicler.config import (
    BlogConfig,
    BotsConfig,
    ChroniclerConfig,
    JiraConfig,
    LlmConfig,
    NoTeamConfig,
    OwnersConfig,
    ReposConfig,
    RepoSecondary,
    RosterConfig,
    load_config,
    render_sample_config,
    write_sample_config,
)


class TestDefaults:
    """Default config matches current HyperShift behavior."""

    def test_default_project_name(self):
        cfg = ChroniclerConfig()
        assert cfg.project_name == "HyperShift"

    def test_default_primary_repo(self):
        cfg = ChroniclerConfig()
        assert cfg.repos.primary == "openshift/hypershift"

    def test_default_secondary_repos(self):
        cfg = ChroniclerConfig()
        names = [r.name for r in cfg.repos.secondary]
        assert "openshift-eng/ai-helpers" in names
        assert "openshift/enhancements" in names
        assert "openshift/release" in names

    def test_default_jira_url(self):
        cfg = ChroniclerConfig()
        assert cfg.jira.url == "https://redhat.atlassian.net"

    def test_default_ticket_prefixes(self):
        cfg = ChroniclerConfig()
        assert "OCPBUGS" in cfg.jira.ticket_prefixes
        assert "CNTRLPLANE" in cfg.jira.ticket_prefixes

    def test_default_team_is_owners(self):
        cfg = ChroniclerConfig()
        assert isinstance(cfg.team, OwnersConfig)
        assert cfg.team.file == "OWNERS_ALIASES"

    def test_default_bots(self):
        cfg = ChroniclerConfig()
        assert "dependabot" in cfg.bots.logins

    def test_default_llm(self):
        cfg = ChroniclerConfig()
        assert cfg.llm.model == "claude-sonnet-5"
        assert cfg.llm.vertex_region == "us-east5"


class TestDerivedProperties:

    def test_ticket_regex_matches(self):
        cfg = ChroniclerConfig()
        assert cfg.ticket_regex.search("fixes OCPBUGS-1234")
        assert cfg.ticket_regex.search("see CNTRLPLANE-42")
        assert not cfg.ticket_regex.search("UNKNOWN-999")

    def test_primary_owner_and_name(self):
        cfg = ChroniclerConfig()
        assert cfg.primary_owner == "openshift"
        assert cfg.primary_name == "hypershift"

    def test_repo_map(self):
        cfg = ChroniclerConfig()
        assert "openshift/release" in cfg.repo_map
        assert cfg.repo_map["openshift/release"].path_filter == "hypershift"

    def test_jira_fields_csv(self):
        cfg = ChroniclerConfig()
        csv = cfg.jira_fields_csv
        assert "customfield_10978" in csv
        assert "customfield_10979" in csv
        assert "customfield_10980" in csv
        assert csv.startswith("summary,description,")


class TestLoadConfig:

    def test_empty_file_returns_defaults(self, tmp_path):
        cfg_file = tmp_path / "config.toml"
        cfg_file.write_bytes(b"")
        cfg = load_config(cfg_file)
        assert cfg.project_name == "HyperShift"

    def test_explicit_missing_file_raises(self, tmp_path):
        with pytest.raises(ValueError, match="not found"):
            load_config(tmp_path / "nope.toml")

    def test_partial_config_merges_with_defaults(self, tmp_path):
        cfg_file = tmp_path / "config.toml"
        cfg_file.write_text(textwrap.dedent("""\
            [project]
            name = "Karpenter"

            [llm]
            model = "claude-opus-5"
        """))
        cfg = load_config(cfg_file)
        assert cfg.project_name == "Karpenter"
        assert cfg.llm.model == "claude-opus-5"
        assert cfg.llm.vertex_region == "us-east5"  # kept default
        assert cfg.repos.primary == "openshift/hypershift"  # kept default

    def test_custom_repos(self, tmp_path):
        cfg_file = tmp_path / "config.toml"
        cfg_file.write_text(textwrap.dedent("""\
            [repos]
            primary = "my-org/my-repo"

            [[repos.secondary]]
            name = "my-org/docs"
            filter = "team-only"
        """))
        cfg = load_config(cfg_file)
        assert cfg.repos.primary == "my-org/my-repo"
        assert len(cfg.repos.secondary) == 1
        assert cfg.repos.secondary[0].name == "my-org/docs"

    def test_custom_jira_prefixes(self, tmp_path):
        cfg_file = tmp_path / "config.toml"
        cfg_file.write_text(textwrap.dedent("""\
            [jira]
            ticket_prefixes = ["PROJ", "BUGS"]
            grouping_prefix = "PROJ"
        """))
        cfg = load_config(cfg_file)
        assert cfg.jira.ticket_prefixes == ["PROJ", "BUGS"]
        assert cfg.ticket_regex.search("PROJ-42")
        assert not cfg.ticket_regex.search("OCPBUGS-1")


class TestTeamUnion:

    def test_owners_config_from_toml(self, tmp_path):
        cfg_file = tmp_path / "config.toml"
        cfg_file.write_text(textwrap.dedent("""\
            [team.owners]
            file = "CODEOWNERS"
            include_groups = ["maintainers"]
            exclude_groups = []
        """))
        cfg = load_config(cfg_file)
        assert isinstance(cfg.team, OwnersConfig)
        assert cfg.team.file == "CODEOWNERS"
        assert cfg.team.include_groups == ["maintainers"]

    def test_roster_config_from_toml(self, tmp_path):
        cfg_file = tmp_path / "config.toml"
        cfg_file.write_text(textwrap.dedent("""\
            [team]
            members = ["alice", "bob"]
        """))
        cfg = load_config(cfg_file)
        assert isinstance(cfg.team, RosterConfig)
        assert cfg.team.members == ["alice", "bob"]

    def test_empty_team_section_gives_no_team(self, tmp_path):
        cfg_file = tmp_path / "config.toml"
        cfg_file.write_text(textwrap.dedent("""\
            [team]
        """))
        cfg = load_config(cfg_file)
        assert isinstance(cfg.team, NoTeamConfig)

    def test_no_team_section_gives_owners_default(self, tmp_path):
        cfg_file = tmp_path / "config.toml"
        cfg_file.write_text(textwrap.dedent("""\
            [project]
            name = "Test"
        """))
        cfg = load_config(cfg_file)
        assert isinstance(cfg.team, OwnersConfig)


class TestSampleConfig:

    def test_write_sample_creates_file(self, tmp_path):
        dest = tmp_path / "chronicler" / "config.toml"
        result = write_sample_config(dest)
        assert result == dest
        assert dest.exists()
        content = dest.read_text()
        assert "Chronicler configuration" in content

    def test_sample_is_valid_toml_when_uncommented(self):
        """Uncomment only TOML-structural lines and parse.

        The team section shows mutually exclusive options, so we only
        uncomment Option A (owners) and skip B/C.
        """
        import re
        sample = render_sample_config()
        toml_lines = []
        skip_until_blank = False
        in_option_a = False
        for line in sample.splitlines():
            # Skip Option B and C blocks entirely
            if "Option B" in line or "Option C" in line:
                skip_until_blank = True
                continue
            if "Option A" in line:
                in_option_a = True
                continue
            if skip_until_blank:
                if not line.strip() or not line.startswith("#"):
                    skip_until_blank = False
                    if not line.strip():
                        toml_lines.append("")
                continue

            stripped = line.lstrip("# ").strip()
            if not stripped:
                toml_lines.append("")
                continue
            if re.match(r"\[+[\w.]+\]+$", stripped):
                toml_lines.append(stripped)
            elif re.match(r'\w[\w_]*\s*=\s*.+', stripped):
                toml_lines.append(stripped)

        import sys
        if sys.version_info >= (3, 11):
            import tomllib
        else:
            import tomli as tomllib
        tomllib.loads("\n".join(toml_lines))

    def test_sample_contains_all_sections(self):
        sample = render_sample_config()
        for section in ["[project]", "[repos]", "[jira]", "[bots]", "[llm]", "[blog]"]:
            assert section in sample, f"Missing {section}"
        assert "team.owners" in sample
        assert "team]" in sample

    def test_auto_create_on_missing_default(self, tmp_path):
        with patch("chronicler.config.config_path", return_value=tmp_path / "config.toml"):
            cfg = load_config()
            assert cfg.project_name == "HyperShift"
            assert (tmp_path / "config.toml").exists()
