#!/usr/bin/env python3
"""Tests for weekly_pr_report module."""

import json
import pytest
from unittest.mock import patch, MagicMock
from chronicler.app import BLOG_PROMPT_TEMPLATE, PRReportGenerator, fetch_model_pricing


def test_blog_prompt_template_renders_without_errors():
    """Verify BLOG_PROMPT_TEMPLATE renders successfully with all required placeholders."""
    # Dummy/placeholder values for all format parameters
    params = {
        'project_name': 'TestProject',
        'aggregated_path': '/tmp/pr_deep_aggregated.json',
        'blog_data_path': '/tmp/blog_data.json',
        'template_path': 'docs/content/blog/2026-06-progress-report.md',
        'start_date': '2026-06-23',
        'end_date': '2026-07-22',
        'pr_count': 42,
        'contributor_count': 15,
        'blog_filename': '2026-07-progress-report.md',
        'blog_output_dir': 'docs/content/blog',
    }

    # Render the template
    result = BLOG_PROMPT_TEMPLATE.format(**params)

    # Assert result is a non-empty string
    assert isinstance(result, str)
    assert len(result) > 0

    # Assert key content sections are present (spot-check a few)
    assert 'Writing Style Guide' in result, "Missing 'Writing Style Guide' section"
    assert 'Beneath the Headlines' in result, "Missing 'Beneath the Headlines' reference"
    assert 'NEVER guess' in result, "Missing 'NEVER guess' instruction"
    assert 'S360 references' in result, "Missing 'S360 references' in sensitive content section"
    assert 'material-newspaper-variant-outline' in result, "Missing material icon reference"
    assert 'contributor_table' in result, "Missing 'contributor_table' reference"

    # Assert placeholders were actually replaced (check a few)
    assert '/tmp/pr_deep_aggregated.json' in result, "aggregated_path placeholder not replaced"
    assert '/tmp/blog_data.json' in result, "blog_data_path placeholder not replaced"
    assert '2026-06-23' in result, "start_date placeholder not replaced"
    assert '2026-07-22' in result, "end_date placeholder not replaced"
    assert '42' in result, "pr_count placeholder not replaced"
    assert '15' in result, "contributor_count placeholder not replaced"
    assert '2026-07-progress-report.md' in result, "blog_filename placeholder not replaced"
    assert 'docs/content/blog' in result, "blog_output_dir placeholder not replaced"


def test_blog_prompt_template_contains_required_instructions():
    """Verify the template contains critical instructions for blog generation."""
    # This test checks the raw template content (unformatted)

    # Check for phase instructions
    assert 'Phase 1: Write the blog post' in BLOG_PROMPT_TEMPLATE
    assert 'Phase 2: Update site navigation' in BLOG_PROMPT_TEMPLATE
    assert 'Phase 3: Preview' in BLOG_PROMPT_TEMPLATE

    # Check for style guidelines
    assert 'Problem-first storytelling' in BLOG_PROMPT_TEMPLATE
    assert 'Conversational but authoritative tone' in BLOG_PROMPT_TEMPLATE
    assert 'Technical depth with accessibility' in BLOG_PROMPT_TEMPLATE
    assert 'Historical context' in BLOG_PROMPT_TEMPLATE
    assert 'Credit contributors by GitHub handle' in BLOG_PROMPT_TEMPLATE

    # Check for structure elements
    assert 'material-newspaper-variant-outline' in BLOG_PROMPT_TEMPLATE
    assert 'stats_cards' in BLOG_PROMPT_TEMPLATE
    assert 'metrics_table' in BLOG_PROMPT_TEMPLATE
    assert 'top_reviewers_table' in BLOG_PROMPT_TEMPLATE

    # Check for sensitive content filtering
    assert 'Sensitive Content Filtering' in BLOG_PROMPT_TEMPLATE
    assert 'SFDC case' in BLOG_PROMPT_TEMPLATE
    assert 'compliance' in BLOG_PROMPT_TEMPLATE


def test_blog_prompt_template_uses_custom_project_name():
    """Verify rendered template uses the custom project name, not 'HyperShift'."""
    params = {
        'project_name': 'Karpenter',
        'aggregated_path': '/tmp/agg.json',
        'blog_data_path': '/tmp/blog.json',
        'template_path': 'docs/blog/template.md',
        'start_date': '2026-08-01',
        'end_date': '2026-08-31',
        'pr_count': 10,
        'contributor_count': 5,
        'blog_filename': '2026-08-progress-report.md',
        'blog_output_dir': 'site/blog',
    }
    result = BLOG_PROMPT_TEMPLATE.format(**params)

    # Project name should appear (from placeholder)
    assert 'Karpenter' in result, "Custom project name not found in rendered template"
    # No hardcoded HyperShift text should remain
    assert 'HyperShift' not in result, "Hardcoded 'HyperShift' found in rendered template"


def test_blog_prompt_template_uses_custom_output_dir():
    """Verify rendered template uses blog_output_dir, not hardcoded 'docs/content/blog'."""
    params = {
        'project_name': 'MyProject',
        'aggregated_path': '/tmp/agg.json',
        'blog_data_path': '/tmp/blog.json',
        'template_path': 'custom/blog/template.md',
        'start_date': '2026-08-01',
        'end_date': '2026-08-31',
        'pr_count': 10,
        'contributor_count': 5,
        'blog_filename': '2026-08-progress-report.md',
        'blog_output_dir': 'custom/blog/output',
    }
    result = BLOG_PROMPT_TEMPLATE.format(**params)

    # The custom output dir should appear in the output file path
    assert 'custom/blog/output/2026-08-progress-report.md' in result, \
        "Blog output path not using blog_output_dir"
    # Navigation update instruction should also use the custom dir
    assert 'custom/blog/output/index.md' in result, \
        "Navigation index path not using blog_output_dir"


def test_blog_prompt_template_no_hardcoded_blog_path():
    """Verify the raw template has no hardcoded 'docs/content/blog' paths."""
    # The raw template should only contain {blog_output_dir} placeholders,
    # not hardcoded docs/content/blog paths
    assert 'docs/content/blog/' not in BLOG_PROMPT_TEMPLATE, \
        "Template contains hardcoded 'docs/content/blog/' path — should use {blog_output_dir}"


@patch.dict('os.environ', {'GITHUB_TOKEN': 'fake-token'})
def test_generate_blog_data_contributor_columns(tmp_path):
    """Verify generate_blog_data dynamically generates contributor columns from config repos."""
    from chronicler.config import ChroniclerConfig, RepoConfig, BlogConfig, NoTeamConfig

    custom_config = ChroniclerConfig(
        project_name="TestProject",
        repos=[
            RepoConfig(name="myorg/alpha", filter="all"),
            RepoConfig(name="myorg/beta", filter="all"),
        ],
        team=NoTeamConfig(),
        blog=BlogConfig(output_dir="custom/blog"),
    )

    generator = PRReportGenerator(
        since_date="2026-08-01",
        end_date="2026-08-31",
        output_dir=str(tmp_path),
        config=custom_config,
    )
    # Simulate one PR per repo
    generator.prs = [
        {
            'repo': 'myorg/alpha',
            'number': 1,
            'title': 'Fix thing',
            'url': 'https://github.com/myorg/alpha/pull/1',
            'author': 'alice',
            'createdAt': '2026-08-15T00:00:00Z',
            'mergedAt': '2026-08-15T12:00:00Z',
            'readyAt': '2026-08-15T00:00:00Z',
            'wasDraft': False,
            'draftToReadyHours': None,
            'readyToMergeHours': 12.0,
            'reviewers': ['bob'],
            'approvers': ['bob'],
            'jiraTickets': [],
            'labels': [],
            'body': 'Some body text',
        },
    ]
    generator.jira_hierarchy = {}

    generator.generate_blog_data(str(tmp_path))

    blog_data_path = tmp_path / 'blog_data.json'
    assert blog_data_path.exists(), "blog_data.json was not created"

    with open(blog_data_path) as f:
        data = json.load(f)

    # Contributor table should have columns for the configured repos
    contrib_table = data['markdown']['contributor_table']
    assert 'alpha' in contrib_table, "Column for 'alpha' repo missing"
    assert 'beta' in contrib_table, "Column for 'beta' repo missing"
    # Should NOT contain HyperShift-specific repo names
    assert 'hypershift' not in contrib_table.lower(), \
        "Contributor table contains hardcoded 'hypershift' column"


@patch.dict('os.environ', {'GITHUB_TOKEN': 'fake-token'})
def test_generate_blog_data_no_hypershift_in_output(tmp_path):
    """Verify no HyperShift-specific text in blog_data.json with custom config."""
    from chronicler.config import ChroniclerConfig, RepoConfig, BlogConfig, NoTeamConfig, JiraConfig

    custom_config = ChroniclerConfig(
        project_name="Karpenter",
        repos=[
            RepoConfig(name="karpenter/core", filter="all"),
        ],
        team=NoTeamConfig(),
        blog=BlogConfig(output_dir="site/posts"),
        jira=JiraConfig(
            ticket_prefixes=["KARP"],
            grouping_prefix="KARP",
            bug_prefix="KARP",
        ),
    )

    generator = PRReportGenerator(
        since_date="2026-08-01",
        end_date="2026-08-31",
        output_dir=str(tmp_path),
        config=custom_config,
    )
    generator.prs = []
    generator.jira_hierarchy = {}

    generator.generate_blog_data(str(tmp_path))

    blog_data_path = tmp_path / 'blog_data.json'
    with open(blog_data_path) as f:
        raw_content = f.read()

    # No HyperShift-specific text should appear in the output
    assert 'hypershift' not in raw_content.lower(), \
        "blog_data.json contains 'hypershift' text with custom config"
    assert 'HyperShift' not in raw_content, \
        "blog_data.json contains 'HyperShift' text with custom config"


def test_fetch_model_pricing_returns_dict():
    """Verify fetch_model_pricing returns a dict with expected keys."""
    mock_pricing_data = {
        "claude-sonnet-5": {
            "input_cost_per_token": 0.000002,
            "output_cost_per_token": 0.00001,
            "cache_creation_input_token_cost": 0.0000025,
            "cache_read_input_token_cost": 0.0000002,
        }
    }

    with patch('urllib.request.urlopen') as mock_urlopen:
        # Mock the HTTP response
        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps(mock_pricing_data).encode()
        mock_response.__enter__.return_value = mock_response
        mock_urlopen.return_value = mock_response

        # Clear the cache first
        import chronicler.app
        chronicler.app._pricing_cache = None

        result = fetch_model_pricing("claude-sonnet-5", is_vertex=False)

        # Verify the result structure
        assert result is not None
        assert isinstance(result, dict)
        assert 'input' in result
        assert 'output' in result
        assert 'cache_write' in result
        assert 'cache_read' in result

        # Verify the values are converted to per-million-token rates
        assert result['input'] == pytest.approx(2.0)  # 0.000002 * 1_000_000
        assert result['output'] == pytest.approx(10.0)  # 0.00001 * 1_000_000
        assert result['cache_write'] == pytest.approx(2.5)  # 0.0000025 * 1_000_000
        assert result['cache_read'] == pytest.approx(0.2)  # 0.0000002 * 1_000_000


def test_fetch_model_pricing_vertex_prefix():
    """Verify that is_vertex=True looks up vertex_ai/ prefixed keys."""
    mock_pricing_data = {
        "vertex_ai/claude-sonnet-5": {
            "input_cost_per_token": 0.000002,
            "output_cost_per_token": 0.00001,
            "cache_creation_input_token_cost": 0.0000025,
            "cache_read_input_token_cost": 0.0000002,
        }
    }

    with patch('urllib.request.urlopen') as mock_urlopen:
        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps(mock_pricing_data).encode()
        mock_response.__enter__.return_value = mock_response
        mock_urlopen.return_value = mock_response

        # Clear the cache
        import chronicler.app
        chronicler.app._pricing_cache = None

        result = fetch_model_pricing("claude-sonnet-5", is_vertex=True)

        assert result is not None
        assert result['input'] == 2.0
        assert result['output'] == 10.0


def test_fetch_model_pricing_network_failure():
    """Verify that network errors return None without raising."""
    with patch('urllib.request.urlopen') as mock_urlopen:
        # Simulate a network error
        mock_urlopen.side_effect = Exception("Network error")

        # Clear the cache
        import chronicler.app
        chronicler.app._pricing_cache = None

        result = fetch_model_pricing("claude-sonnet-5", is_vertex=False)

        # Should return None on network failure
        assert result is None
