#!/usr/bin/env python3
"""Tests for weekly_pr_report module."""

import json
import pytest
from unittest.mock import patch, MagicMock
from weekly_pr_report import BLOG_PROMPT_TEMPLATE, fetch_model_pricing


def test_blog_prompt_template_renders_without_errors():
    """Verify BLOG_PROMPT_TEMPLATE renders successfully with all required placeholders."""
    # Dummy/placeholder values for all format parameters
    params = {
        'aggregated_path': '/tmp/pr_deep_aggregated.json',
        'blog_data_path': '/tmp/blog_data.json',
        'template_path': 'docs/content/blog/2026-06-progress-report.md',
        'start_date': '2026-06-23',
        'end_date': '2026-07-22',
        'pr_count': 42,
        'contributor_count': 15,
        'blog_filename': '2026-07-progress-report.md',
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
    assert 'material-star-shooting' in result, "Missing 'material-star-shooting' icon reference"
    assert 'contributor_table' in result, "Missing 'contributor_table' reference"

    # Assert placeholders were actually replaced (check a few)
    assert '/tmp/pr_deep_aggregated.json' in result, "aggregated_path placeholder not replaced"
    assert '/tmp/blog_data.json' in result, "blog_data_path placeholder not replaced"
    assert '2026-06-23' in result, "start_date placeholder not replaced"
    assert '2026-07-22' in result, "end_date placeholder not replaced"
    assert '42' in result, "pr_count placeholder not replaced"
    assert '15' in result, "contributor_count placeholder not replaced"
    assert '2026-07-progress-report.md' in result, "blog_filename placeholder not replaced"


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
    assert 'material-icon' in BLOG_PROMPT_TEMPLATE
    assert 'stats_cards' in BLOG_PROMPT_TEMPLATE
    assert 'metrics_table' in BLOG_PROMPT_TEMPLATE
    assert 'top_reviewers_table' in BLOG_PROMPT_TEMPLATE

    # Check for sensitive content filtering
    assert 'Sensitive Content Filtering' in BLOG_PROMPT_TEMPLATE
    assert 'SFDC case' in BLOG_PROMPT_TEMPLATE
    assert 'compliance' in BLOG_PROMPT_TEMPLATE


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
        import weekly_pr_report
        weekly_pr_report._pricing_cache = None

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
        import weekly_pr_report
        weekly_pr_report._pricing_cache = None

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
        import weekly_pr_report
        weekly_pr_report._pricing_cache = None

        result = fetch_model_pricing("claude-sonnet-5", is_vertex=False)

        # Should return None on network failure
        assert result is None
