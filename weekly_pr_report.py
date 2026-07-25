#!/usr/bin/env python3
"""
Weekly PR Report Generator for HyperShift
Optimized version with parallel API calls and batch processing
"""

import argparse
import asyncio
import base64
import json
import os
import re
import sys
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Set, Tuple
import subprocess
import time
import urllib.request
import anthropic

from textual.app import App, ComposeResult
from textual.screen import Screen
from textual.widgets import DataTable, Footer, Header, ProgressBar, RichLog, Static
from textual.binding import Binding

# LiteLLM pricing database URL
LITELLM_PRICING_URL = "https://raw.githubusercontent.com/BerriAI/litellm/main/model_prices_and_context_window.json"

# Module-level cache for pricing data
_pricing_cache = None


def fetch_model_pricing(model_id: str, is_vertex: bool = False) -> Optional[Dict]:
    """Fetch pricing for a model from the LiteLLM pricing database.

    Args:
        model_id: The model identifier (e.g., 'claude-sonnet-5', 'claude-opus-4-6')
        is_vertex: If True, looks up the Vertex AI variant (vertex_ai/{model_id})

    Returns:
        A dict with keys {'input', 'output', 'cache_write', 'cache_read'} where
        values are cost per million tokens, or None if the model is not found
        or the network request fails.
    """
    global _pricing_cache

    # Fetch pricing data if not cached
    if _pricing_cache is None:
        try:
            with urllib.request.urlopen(LITELLM_PRICING_URL, timeout=10) as response:
                _pricing_cache = json.loads(response.read().decode())
        except Exception as e:
            print(f"Warning: Failed to fetch model pricing: {e}", file=sys.stderr)
            return None

    # Construct lookup key
    lookup_key = f"vertex_ai/{model_id}" if is_vertex else model_id

    # Try exact match first
    if lookup_key in _pricing_cache:
        raw = _pricing_cache[lookup_key]
    else:
        # Try prefix matching (e.g., claude-sonnet-5-20250514 -> claude-sonnet-5)
        candidates = [k for k in _pricing_cache.keys() if k.startswith(lookup_key)]
        if candidates:
            raw = _pricing_cache[candidates[0]]
        else:
            print(f"Warning: Model '{lookup_key}' not found in pricing database", file=sys.stderr)
            return None

    # Extract rates and convert to cost per million tokens
    return {
        'input': raw.get('input_cost_per_token', 0) * 1_000_000,
        'output': raw.get('output_cost_per_token', 0) * 1_000_000,
        'cache_write': raw.get('cache_creation_input_token_cost', 0) * 1_000_000,
        'cache_read': raw.get('cache_read_input_token_cost', 0) * 1_000_000,
    }


# API pagination and limit constants
GITHUB_CONTRIBUTORS_PER_PAGE = 100
GITHUB_PR_SEARCH_LIMIT = 100
GITHUB_REVIEWS_LIMIT = 50
GITHUB_LABELS_LIMIT = 20
GITHUB_TIMELINE_ITEMS_LIMIT = 100
GITHUB_FILES_LIMIT = 50

class RepoFetchError(Exception):
    """Raised when fetching PRs from a repository fails (e.g. API 502)."""

    def __init__(self, repo: str, message: str = ""):
        self.repo = repo
        super().__init__(f"Failed to fetch PRs from {repo}: {message}" if message else
                         f"Failed to fetch PRs from {repo}")


# Default report period
DEFAULT_DAYS_AGO = 7

# Try to import aiohttp, fall back to requests if not available
try:
    import aiohttp
    HAS_AIOHTTP = True
except ImportError:
    import requests
    HAS_AIOHTTP = False
    print("Warning: aiohttp not available, using synchronous requests (slower)")

# Jira rate limiting constants (conservative for Jira Cloud)
JIRA_REQUEST_DELAY_SECONDS = 0.2  # 200ms between requests
JIRA_MAX_CONCURRENT_REQUESTS = 3  # Max 3 parallel requests
JIRA_BATCH_SIZE = 100  # Max tickets per bulkfetch call


class JiraClient:
    """Client for fetching Jira issues via REST API with batch support and rate limiting.

    Targets Jira Cloud at redhat.atlassian.net. Uses Basic auth (email + API token).
    Field mapping (Cloud IDs):
      parent           = native hierarchy (Story->Epic->Feature, replaces old custom fields)
      customfield_10978 = SFDC Cases Counter
      customfield_10979 = SFDC Cases Links
      customfield_10980 = SFDC Cases Open
    """

    FIELDS = 'summary,description,parent,issuetype,customfield_10978,customfield_10979,customfield_10980,issuelinks,labels,priority,status'

    def __init__(self):
        self.base_url = os.getenv('JIRA_URL', 'https://redhat.atlassian.net')
        self.token = os.getenv('JIRA_API_TOKEN') or os.getenv('JIRA_TOKEN')
        self.email = os.getenv('JIRA_USERNAME') or os.getenv('JIRA_EMAIL')
        self.enabled = bool(self.token and self.email)
        self.session: Optional["aiohttp.ClientSession"] = None
        self.semaphore = asyncio.Semaphore(JIRA_MAX_CONCURRENT_REQUESTS)
        self.request_count = 0

    def _get_headers(self) -> Dict:
        """Get authentication headers for Jira Cloud (Basic auth with email + API token)."""
        headers = {
            'Accept': 'application/json',
            'Content-Type': 'application/json',
        }
        if self.token and self.email:
            credentials = base64.b64encode(f'{self.email}:{self.token}'.encode()).decode()
            headers['Authorization'] = f'Basic {credentials}'
        return headers

    async def _fetch_json(self, url: str, *, json_body: Optional[Dict] = None,
                          _retry_count: int = 0) -> Optional[Dict]:
        """Fetch JSON from Jira with rate limiting and error handling.

        Uses POST if json_body is provided, GET otherwise.
        """
        MAX_RETRIES = 3
        async with self.semaphore:
            await asyncio.sleep(JIRA_REQUEST_DELAY_SECONDS)
            self.request_count += 1

            if HAS_AIOHTTP and self.session:
                try:
                    method = self.session.post if json_body is not None else self.session.get
                    kwargs = {'headers': self._get_headers()}
                    if json_body is not None:
                        kwargs['json'] = json_body
                    async with method(url, **kwargs) as response:
                        if response.status == 200:
                            return await response.json()
                        elif response.status == 401:
                            print("Warning: Jira authentication failed (401). Check JIRA_EMAIL and JIRA_TOKEN.")
                            return None
                        elif response.status == 404:
                            return None
                        elif response.status == 429:
                            if _retry_count >= MAX_RETRIES:
                                print(f"  Rate limited {MAX_RETRIES} times, giving up on {url}")
                                return None
                            print("  Rate limited! Waiting 30 seconds...")
                            await asyncio.sleep(30)
                            return await self._fetch_json(url, json_body=json_body, _retry_count=_retry_count + 1)
                        else:
                            text = await response.text()
                            print(f"Warning: Jira API returned {response.status}: {text[:200]}")
                            return None
                except Exception as e:
                    print(f"Error fetching {url}: {e}")
                    return None
            else:
                try:
                    if json_body is not None:
                        response = requests.post(url, headers=self._get_headers(), json=json_body, timeout=30)
                    else:
                        response = requests.get(url, headers=self._get_headers(), timeout=30)
                    if response.status_code == 200:
                        return response.json()
                    elif response.status_code == 401:
                        print("Warning: Jira authentication failed (401). Check JIRA_EMAIL and JIRA_TOKEN.")
                        return None
                    elif response.status_code == 404:
                        return None
                    elif response.status_code == 429:
                        if _retry_count >= MAX_RETRIES:
                            print(f"  Rate limited {MAX_RETRIES} times, giving up on {url}")
                            return None
                        print("  Rate limited! Waiting 30 seconds...")
                        time.sleep(30)
                        return await self._fetch_json(url, json_body=json_body, _retry_count=_retry_count + 1)
                    else:
                        return None
                except Exception as e:
                    print(f"Error fetching {url}: {e}")
                    return None

    async def fetch_issues_batch(self, ticket_keys: List[str]) -> Dict[str, Dict]:
        """Fetch multiple issues using POST /rest/api/3/issue/bulkfetch.

        Advantages over JQL search:
        - Up to 100 keys per call (vs 40 with JQL)
        - No JQL query construction/parsing overhead
        - Per-issue error handling via issueErrors
        """
        if not ticket_keys:
            return {}

        url = f'{self.base_url}/rest/api/3/issue/bulkfetch'
        payload = {
            'issueIdsOrKeys': ticket_keys,
            'fields': self.FIELDS.split(','),
            'expand': ['renderedFields'],
        }

        results = {}
        data = await self._fetch_json(url, json_body=payload)

        if data:
            for issue in data.get('issues', []):
                results[issue['key']] = self._parse_issue(issue)
            errors = data.get('issueErrors', {})
            if errors:
                print(f"  Warning: {len(errors)} issues had errors: {list(errors.keys())[:5]}")

        return results

    def _parse_issue(self, issue: Dict) -> Dict:
        """Parse Jira Cloud issue response into simplified dict.

        Uses the native `parent` field for hierarchy traversal. The parent field
        includes inline data (key, summary, status, priority, issuetype with
        hierarchyLevel) so we can walk Story->Epic->Feature in fewer API calls.
        """
        fields = issue.get('fields', {})

        # Extract parent key (Jira Cloud native hierarchy)
        parent_field = fields.get('parent') or {}
        parent_key = parent_field.get('key')

        # Determine issue type
        issue_type = fields.get('issuetype') or {}
        hierarchy_level = issue_type.get('hierarchyLevel')
        type_name = issue_type.get('name')

        # Get description (truncate for storage)
        description = fields.get('description') or ''
        if isinstance(description, dict):
            # Jira Cloud v3 returns ADF (Atlassian Document Format); extract text
            description = self._extract_text_from_adf(description)
        if len(description) > 2000:
            description = description[:2000] + '...'

        # Extract labels
        labels = []
        for label in fields.get('labels', []):
            if isinstance(label, dict):
                labels.append(label.get('name', ''))
            else:
                labels.append(label)

        # Extract priority and status
        priority = fields.get('priority', {})
        priority_name = priority.get('name') if isinstance(priority, dict) else None

        status = fields.get('status', {})
        status_name = status.get('name') if isinstance(status, dict) else None

        # Extract SFDC case data from renderedFields (Cloud Forge app fields are
        # encrypted in raw fields but decrypted in renderedFields)
        rendered = issue.get('renderedFields', {})
        sfdc_counter_str = rendered.get('customfield_10978') or '0'
        sfdc_open_str = rendered.get('customfield_10980') or '0'
        sfdc_links_str = rendered.get('customfield_10979') or ''

        try:
            sfdc_cases_total = int(sfdc_counter_str)
        except (ValueError, TypeError):
            sfdc_cases_total = 0
        try:
            sfdc_cases_open = int(sfdc_open_str)
        except (ValueError, TypeError):
            sfdc_cases_open = 0

        # Parse case IDs — rendered links are space-separated case numbers
        sfdc_case_ids = re.findall(r'\b(\d{6,8})\b', sfdc_links_str) if sfdc_links_str else []
        sfdc_cases_links_raw = sfdc_links_str

        return {
            'key': issue['key'],
            'summary': fields.get('summary', ''),
            'description': description,
            'parentKey': parent_key,
            'hierarchyLevel': hierarchy_level,
            'typeName': type_name,
            'labels': labels,
            'priority': priority_name,
            'status': status_name,
            'sfdcCasesTotal': sfdc_cases_total,
            'sfdcCasesOpen': sfdc_cases_open,
            'sfdcCaseIds': sfdc_case_ids,
            'sfdcCasesLinks': sfdc_cases_links_raw,
        }

    @staticmethod
    def _extract_text_from_adf(adf: Dict) -> str:
        """Extract plain text from Atlassian Document Format (ADF)."""
        texts = []
        def _walk(node):
            if isinstance(node, dict):
                if node.get('type') == 'text':
                    texts.append(node.get('text', ''))
                for child in node.get('content', []):
                    _walk(child)
            elif isinstance(node, list):
                for item in node:
                    _walk(item)
        _walk(adf)
        return '\n'.join(texts)

    async def fetch_all_tickets(self, tickets: Set[str], progress_callback=None) -> Dict[str, Dict]:
        """Fetch all tickets in batches with rate limiting."""
        ticket_list = list(tickets)
        all_results = {}

        for i in range(0, len(ticket_list), JIRA_BATCH_SIZE):
            batch = ticket_list[i:i + JIRA_BATCH_SIZE]
            batch_num = i // JIRA_BATCH_SIZE + 1
            total_batches = (len(ticket_list) + JIRA_BATCH_SIZE - 1) // JIRA_BATCH_SIZE
            msg = f"Jira batch {batch_num}/{total_batches} ({len(batch)} tickets)"
            print(f"  Fetching {msg}...")
            results = await self.fetch_issues_batch(batch)
            all_results.update(results)
            if progress_callback:
                await progress_callback(batch_num, total_batches, msg)

        return all_results

    async def build_hierarchy(self, tickets: Set[str], progress_callback=None) -> Dict[str, Dict]:
        """Build complete hierarchy including epics and OCPSTRAT parents."""
        if not tickets:
            return {}

        print(f"Fetching Jira data via REST API ({len(tickets)} tickets)...")

        if HAS_AIOHTTP:
            async with aiohttp.ClientSession() as session:
                self.session = session
                return await self._build_hierarchy_internal(tickets, progress_callback=progress_callback)
        else:
            return await self._build_hierarchy_internal(tickets, progress_callback=progress_callback)

    async def _build_hierarchy_internal(self, tickets: Set[str], progress_callback=None) -> Dict[str, Dict]:
        """Internal hierarchy builder using Jira Cloud native parent field.

        Fetches tickets, then iteratively resolves unfetched parents by
        enqueueing newly discovered parent keys from each batch.
        """
        ticket_data: Dict[str, Dict] = {}
        queue = tickets

        # Track progress by hierarchy rounds (ticket → epic → feature).
        # Typical depth is 3 levels; we grow the estimate if needed and
        # only report 100% after the final round completes.
        round_num = 0
        est_rounds = 3

        async def batch_log_callback(batch_num, total_in_round, msg):
            if progress_callback:
                await progress_callback(round_num, est_rounds, msg)

        while True:
            round_num += 1
            # Keep est_rounds > round_num so the bar never hits 100% mid-loop
            est_rounds = max(est_rounds, round_num + 1)
            fetched = await self.fetch_all_tickets(queue, progress_callback=batch_log_callback)
            ticket_data.update(fetched)

            queue = {
                data['parentKey']
                for data in fetched.values()
                if data.get('parentKey') and data['parentKey'] not in ticket_data
            }
            if not queue:
                break
            print(f"  Fetching {len(queue)} parent issues...")

        if progress_callback:
            await progress_callback(round_num, round_num, f"Hierarchy complete ({round_num} rounds)")
        print(f"  Total Jira API requests: {self.request_count}")
        return self._build_hierarchy_dict(ticket_data)

    def _build_hierarchy_dict(self, ticket_data: Dict[str, Dict]) -> Dict[str, Dict]:
        """Build the final hierarchy dict by walking each ticket's parent chain.

        Walks up from each ticket classifying ancestors by issue type:
          Epic    = issuetype 'Epic' or hierarchyLevel 1
          Feature = issuetype 'Feature' or hierarchyLevel 2
        """
        hierarchy = {}

        for key, data in ticket_data.items():
            epic_key = epic_summary = epic_description = None
            feature_key = feature_summary = None

            effective_priority = data.get('priority')
            seen = {key}
            current = data
            while current.get('parentKey') and current['parentKey'] not in seen:
                pk = current['parentKey']
                seen.add(pk)
                parent = ticket_data.get(pk)
                if not parent:
                    break

                ptype = parent.get('typeName', '')
                plevel = parent.get('hierarchyLevel')

                if not effective_priority:
                    effective_priority = parent.get('priority')

                if (plevel == 1 or ptype == 'Epic') and not epic_key:
                    epic_key = pk
                    epic_summary = parent.get('summary')
                    epic_description = parent.get('description')
                elif (plevel == 2 or ptype == 'Feature') and not feature_key:
                    feature_key = pk
                    feature_summary = parent.get('summary')

                current = parent

            hierarchy[key] = {
                'summary': data.get('summary', ''),
                'description': data.get('description', ''),
                'labels': data.get('labels', []),
                'priority': effective_priority,
                'status': data.get('status'),
                'epic': epic_key,
                'epicSummary': epic_summary,
                'epicDescription': epic_description,
                'ocpstrat': feature_key,
                'ocpstratSummary': feature_summary,
                'sfdcCasesTotal': data.get('sfdcCasesTotal', 0),
                'sfdcCasesOpen': data.get('sfdcCasesOpen', 0),
                'sfdcCaseIds': data.get('sfdcCaseIds', []),
                'sfdcCasesLinks': data.get('sfdcCasesLinks', ''),
            }

        return hierarchy


class PRReportGenerator:
    def __init__(self, since_date: str, end_date: Optional[str] = None, output_dir: str = '/tmp'):
        self.since_date = since_date
        self.end_date = end_date or datetime.now().strftime('%Y-%m-%d')
        self.output_dir = output_dir
        self.github_token = os.getenv('GITHUB_TOKEN') or self._get_gh_token()
        self.jira_url = os.getenv('JIRA_URL', 'https://redhat.atlassian.net')

        # Data storage
        self.prs: List[Dict] = []
        self.jira_hierarchy: Dict = {}
        self.hypershift_authors: Set[str] = set()
        self.repo_fetch_status: Dict[str, str] = {}  # repo -> "ok" | "failed"

    BOT_PATTERNS = ['-bot', '-robot', '[bot]']
    BOT_LOGINS = {'coderabbitai', 'hypershift-jira-solve-ci', 'dependabot'}

    @classmethod
    def is_bot(cls, login: str) -> bool:
        login_lower = login.lower()
        return login_lower in cls.BOT_LOGINS or any(p in login_lower for p in cls.BOT_PATTERNS)

    async def _github_graphql_request(self, session, query: str, variables: Dict,
                                       headers: Dict, max_retries: int = 3) -> Optional[Dict]:
        """Make a GitHub GraphQL request with rate limit handling.

        Handles 403/429 rate limit responses by respecting Retry-After headers.
        Returns parsed JSON data or None on failure.
        """
        for attempt in range(max_retries + 1):
            try:
                if HAS_AIOHTTP and session is not None:
                    async with session.post(
                        'https://api.github.com/graphql',
                        json={"query": query, "variables": variables},
                        headers=headers
                    ) as response:
                        if response.status in (403, 429):
                            retry_after = int(response.headers.get('Retry-After', 60))
                            print(f"  Rate limited (HTTP {response.status}), waiting {retry_after}s...")
                            await asyncio.sleep(retry_after)
                            continue
                        if response.status != 200:
                            print(f"  GitHub API returned {response.status}")
                            if attempt < max_retries:
                                await asyncio.sleep(2)
                                continue
                            return None
                        return await response.json()
                else:
                    response = requests.post(
                        'https://api.github.com/graphql',
                        json={"query": query, "variables": variables},
                        headers=headers,
                        timeout=30
                    )
                    if response.status_code in (403, 429):
                        retry_after = int(response.headers.get('Retry-After', 60))
                        print(f"  Rate limited (HTTP {response.status_code}), waiting {retry_after}s...")
                        time.sleep(retry_after)
                        continue
                    if response.status_code != 200:
                        print(f"  GitHub API returned {response.status_code}")
                        if attempt < max_retries:
                            time.sleep(2)
                            continue
                        return None
                    return response.json()
            except Exception as e:
                print(f"  Request error: {e}")
                if attempt < max_retries:
                    time.sleep(2)
                    continue
                return None
        return None

    def _get_gh_token(self) -> str:
        """Get GitHub token from gh CLI"""
        try:
            result = subprocess.run(
                ['gh', 'auth', 'token'],
                capture_output=True,
                text=True,
                check=True
            )
            return result.stdout.strip()
        except Exception as e:
            print(f"Error getting GitHub token: {e}")
            sys.exit(1)

    async def fetch_repository_contributors(self, repo_owner: str, repo_name: str) -> Set[str]:
        """Fetch repository contributors using GitHub REST API"""
        headers = {
            "Authorization": f"Bearer {self.github_token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28"
        }

        contributors = set()
        page = 1

        if HAS_AIOHTTP:
            async with aiohttp.ClientSession() as session:
                while True:
                    url = f"https://api.github.com/repos/{repo_owner}/{repo_name}/contributors?per_page={GITHUB_CONTRIBUTORS_PER_PAGE}&page={page}"
                    async with session.get(url, headers=headers) as response:
                        if response.status != 200:
                            break
                        data = await response.json()

                    if not data:
                        break

                    for contributor in data:
                        contributors.add(contributor['login'])

                    if len(data) < GITHUB_CONTRIBUTORS_PER_PAGE:
                        break

                    page += 1
        else:
            while True:
                url = f"https://api.github.com/repos/{repo_owner}/{repo_name}/contributors?per_page={GITHUB_CONTRIBUTORS_PER_PAGE}&page={page}"
                response = requests.get(url, headers=headers)
                if response.status_code != 200:
                    break
                data = response.json()

                if not data:
                    break

                for contributor in data:
                    contributors.add(contributor['login'])

                if len(data) < GITHUB_CONTRIBUTORS_PER_PAGE:
                    break

                page += 1

        return contributors

    async def fetch_prs_graphql(self, repo_owner: str, repo_name: str) -> List[Dict]:
        """Fetch PRs using GitHub GraphQL API with search query for date filtering.

        Raises RepoFetchError if the API failed entirely (e.g. 502).
        """
        # Use search query to filter by merge date range (range syntax: START..END)
        search_query = f"repo:{repo_owner}/{repo_name} is:pr is:merged merged:{self.since_date}..{self.end_date}"

        query = f"""
        query($searchQuery: String!, $cursor: String) {{
          search(query: $searchQuery, type: ISSUE, first: {GITHUB_PR_SEARCH_LIMIT}, after: $cursor) {{
            pageInfo {{
              hasNextPage
              endCursor
            }}
            nodes {{
              ... on PullRequest {{
                number
                title
                url
                author {{ login }}
                createdAt
                mergedAt
                isDraft
                body
                labels(first: {GITHUB_LABELS_LIMIT}) {{ nodes {{ name }} }}
                reviews(first: {GITHUB_REVIEWS_LIMIT}) {{
                  nodes {{
                    author {{ login }}
                    state
                    submittedAt
                  }}
                }}
                timelineItems(first: {GITHUB_TIMELINE_ITEMS_LIMIT}, itemTypes: [READY_FOR_REVIEW_EVENT, CONVERT_TO_DRAFT_EVENT]) {{
                  nodes {{
                    __typename
                    ... on ReadyForReviewEvent {{ createdAt }}
                    ... on ConvertToDraftEvent {{ createdAt }}
                  }}
                }}
              }}
            }}
          }}
        }}
        """

        headers = {
            "Authorization": f"Bearer {self.github_token}",
            "Content-Type": "application/json"
        }

        prs = []
        cursor = None
        got_any_page = False

        session = aiohttp.ClientSession() if HAS_AIOHTTP else None
        try:
            while True:
                variables = {"searchQuery": search_query, "cursor": cursor}
                data = await self._github_graphql_request(session, query, variables, headers)
                if not data:
                    break

                if 'errors' in data:
                    print(f"GraphQL errors: {data['errors']}")
                    break

                if 'data' not in data or not data['data']['search']:
                    break

                got_any_page = True
                for pr in data['data']['search']['nodes']:
                    if pr:  # Skip null entries
                        prs.append(self._process_pr_data(pr, f"{repo_owner}/{repo_name}"))

                page_info = data['data']['search']['pageInfo']
                if not page_info['hasNextPage']:
                    break
                cursor = page_info['endCursor']
        finally:
            if session:
                await session.close()

        if not got_any_page:
            raise RepoFetchError(f"{repo_owner}/{repo_name}", "API returned no data")
        return prs

    async def fetch_release_prs_graphql(self) -> List[Dict]:
        """Fetch PRs from openshift/release that touch HyperShift-related paths.

        Uses a two-pass approach because openshift/release has hundreds of PRs
        per week, and fetching full details (reviews, timeline, files) for all
        of them exceeds GitHub's GraphQL complexity limit (502).

        Pass 1: Use "hypershift" as a search keyword to pre-filter PRs (GitHub
                searches titles, bodies, and file paths). This reduces ~300 PRs
                to ~30-40 candidates. Fetch files to confirm path match.
        Pass 2: Fetch full details (reviews, timeline) only for confirmed
                matches using node IDs.
        """
        repo_owner = 'openshift'
        repo_name = 'release'
        # Add "hypershift" keyword to pre-filter at the GitHub search level
        search_query = (
            f"repo:{repo_owner}/{repo_name} is:pr is:merged "
            f"merged:{self.since_date}..{self.end_date} hypershift"
        )

        # Pass 1: fetch candidates with files for path verification
        filter_query = f"""
        query($searchQuery: String!, $cursor: String) {{
          search(query: $searchQuery, type: ISSUE, first: 100, after: $cursor) {{
            pageInfo {{
              hasNextPage
              endCursor
            }}
            nodes {{
              ... on PullRequest {{
                id
                number
                title
                files(first: {GITHUB_FILES_LIMIT}) {{
                  pageInfo {{
                    hasNextPage
                  }}
                  nodes {{
                    path
                  }}
                }}
              }}
            }}
          }}
        }}
        """

        headers = {
            "Authorization": f"Bearer {self.github_token}",
            "Content-Type": "application/json"
        }

        def is_hypershift_related(pr_data: Dict) -> bool:
            """Check if PR touches any HyperShift-related paths."""
            files = pr_data.get('files', {}).get('nodes', [])
            for file_node in files:
                path = file_node.get('path', '')
                if 'hypershift' in path.lower():
                    return True
            return False

        def files_incomplete(pr_data: Dict) -> bool:
            """Check if files were truncated by the GraphQL limit."""
            return pr_data.get('files', {}).get('pageInfo', {}).get('hasNextPage', False)

        async def check_remaining_files_async(session, pr_number: int) -> bool:
            """Use REST API to check all files when GraphQL result was truncated."""
            page = 1
            per_page = 100
            while True:
                url = f"https://api.github.com/repos/{repo_owner}/{repo_name}/pulls/{pr_number}/files"
                params = {"per_page": per_page, "page": page}
                try:
                    if HAS_AIOHTTP:
                        async with session.get(url, headers=headers, params=params) as resp:
                            if resp.status != 200:
                                return False
                            batch = await resp.json()
                    else:
                        resp = requests.get(url, headers=headers, params=params, timeout=30)
                        if resp.status_code != 200:
                            return False
                        batch = resp.json()
                except Exception:
                    return False
                for f in batch:
                    if 'hypershift' in f.get('filename', '').lower():
                        return True
                if len(batch) < per_page:
                    break
                page += 1
            return False

        # Pass 1: find HyperShift-related PR node IDs
        matched_ids = []
        cursor = None
        total_scanned = 0
        got_any_page = False

        session = aiohttp.ClientSession() if HAS_AIOHTTP else None
        try:
            while True:
                variables = {"searchQuery": search_query, "cursor": cursor}
                data = await self._github_graphql_request(session, filter_query, variables, headers)
                if not data:
                    break

                if 'errors' in data:
                    print(f"  Pass 1 GraphQL errors: {data['errors']}")
                    break

                if 'data' not in data or not data['data']['search']:
                    break

                got_any_page = True
                for pr in data['data']['search']['nodes']:
                    if pr:
                        total_scanned += 1
                        if is_hypershift_related(pr):
                            matched_ids.append(pr['id'])
                        elif files_incomplete(pr):
                            if await check_remaining_files_async(session, pr['number']):
                                matched_ids.append(pr['id'])

                page_info = data['data']['search']['pageInfo']
                if not page_info['hasNextPage']:
                    break
                cursor = page_info['endCursor']
        finally:
            if session:
                await session.close()

        if not got_any_page:
            raise RepoFetchError(f"{repo_owner}/{repo_name}", "API returned no data")

        print(f"  Pass 1: scanned {total_scanned} candidates, {len(matched_ids)} confirmed by file path")

        if not matched_ids:
            return []

        # Pass 2: fetch full details for matched PRs by node ID
        prs = []
        bot_count = 0
        batch_size = 20
        session = aiohttp.ClientSession() if HAS_AIOHTTP else None
        try:
            for i in range(0, len(matched_ids), batch_size):
                batch_ids = matched_ids[i:i + batch_size]
                node_fragments = ""
                for j, node_id in enumerate(batch_ids):
                    node_fragments += f"""
                    pr{j}: node(id: "{node_id}") {{
                      ... on PullRequest {{
                        number
                        title
                        url
                        author {{ login }}
                        createdAt
                        mergedAt
                        isDraft
                        body
                        labels(first: {GITHUB_LABELS_LIMIT}) {{ nodes {{ name }} }}
                        reviews(first: {GITHUB_REVIEWS_LIMIT}) {{
                          nodes {{
                            author {{ login }}
                            state
                            submittedAt
                          }}
                        }}
                        timelineItems(first: {GITHUB_TIMELINE_ITEMS_LIMIT}, itemTypes: [READY_FOR_REVIEW_EVENT, CONVERT_TO_DRAFT_EVENT]) {{
                          nodes {{
                            __typename
                            ... on ReadyForReviewEvent {{ createdAt }}
                            ... on ConvertToDraftEvent {{ createdAt }}
                          }}
                        }}
                      }}
                    }}
                    """
                detail_query = f"query {{ {node_fragments} }}"
                data = await self._github_graphql_request(session, detail_query, {}, headers)
                if not data or 'data' not in data:
                    print(f"  Pass 2: failed to fetch batch {i // batch_size + 1}")
                    continue

                for j in range(len(batch_ids)):
                    pr = data['data'].get(f'pr{j}')
                    if pr:
                        pr_data = self._process_pr_data(pr, f"{repo_owner}/{repo_name}")
                        author = pr.get('author', {}).get('login', '') if pr.get('author') else ''
                        if self.is_bot(author):
                            pr_data['is_bot'] = True
                            bot_count += 1
                        prs.append(pr_data)
        finally:
            if session:
                await session.close()

        bot_note = f", {bot_count} bot-authored" if bot_count else ""
        print(f"  Scanned {total_scanned} release PRs, found {len(prs)} HyperShift-related{bot_note}")
        return prs

    def _process_pr_data(self, pr: Dict, repo: str) -> Dict:
        """Process PR data from GraphQL response"""
        # Extract reviewers and approvers
        reviewers = set()
        approvers = set()

        if pr.get('reviews', {}).get('nodes'):
            for review in pr['reviews']['nodes']:
                if review['author']:
                    login = review['author']['login']
                    reviewers.add(login)
                    if review['state'] == 'APPROVED':
                        approvers.add(login)

        # Calculate timeline
        created_at = datetime.fromisoformat(pr['createdAt'].replace('Z', '+00:00'))
        merged_at = datetime.fromisoformat(pr['mergedAt'].replace('Z', '+00:00'))

        # Find when it became ready
        ready_at = created_at
        was_draft = pr['isDraft']

        if pr.get('timelineItems', {}).get('nodes'):
            for item in pr['timelineItems']['nodes']:
                if item['__typename'] == 'ReadyForReviewEvent':
                    ready_at = datetime.fromisoformat(item['createdAt'].replace('Z', '+00:00'))
                    was_draft = True
                    break

        # Calculate hours
        draft_to_ready_hours = None
        if was_draft and ready_at > created_at:
            draft_to_ready_hours = (ready_at - created_at).total_seconds() / 3600

        ready_to_merge_hours = (merged_at - ready_at).total_seconds() / 3600

        # Extract Jira tickets with word boundaries for accurate matching
        text = f"{pr['title']}\n{pr['body'] or ''}"
        jira_tickets = list(set(re.findall(
            r'\b(?:OCPBUGS|CNTRLPLANE|OCPSTRAT|RFE|HOSTEDCP)-\d+\b',
            text
        )))

        # Extract labels
        labels = [label['name'] for label in pr.get('labels', {}).get('nodes', [])]

        return {
            'repo': repo,
            'number': pr['number'],
            'title': pr['title'],
            'url': pr['url'],
            'author': pr['author']['login'] if pr['author'] else 'ghost',
            'createdAt': pr['createdAt'],
            'mergedAt': pr['mergedAt'],
            'readyAt': ready_at.isoformat(),
            'wasDraft': was_draft,
            'draftToReadyHours': draft_to_ready_hours,
            'readyToMergeHours': ready_to_merge_hours,
            'reviewers': sorted(list(reviewers)),
            'approvers': sorted(list(approvers)),
            'jiraTickets': list(set(jira_tickets)),
            'labels': labels,
            'body': pr['body']
        }

    def _load_fetch_status(self) -> Dict[str, str]:
        """Load repo fetch status from previous run."""
        status_path = os.path.join(self.output_dir, 'fetch_status.json')
        try:
            with open(status_path, 'r') as f:
                return json.load(f)
        except FileNotFoundError:
            return {}

    def _save_fetch_status(self):
        """Save repo fetch status for --resume."""
        status_path = os.path.join(self.output_dir, 'fetch_status.json')
        with open(status_path, 'w') as f:
            json.dump(self.repo_fetch_status, f, indent=2)

    def _load_existing_prs(self) -> List[Dict]:
        """Load existing PR data from previous run."""
        data_path = os.path.join(self.output_dir, 'hypershift_pr_details_fast.json')
        try:
            with open(data_path, 'r') as f:
                return json.load(f)
        except FileNotFoundError:
            return []

    async def fetch_all_prs(self, resume: bool = False, progress_callback=None):
        """Fetch PRs from all repositories in parallel.

        Args:
            resume: If True, load existing data and only re-fetch repos that failed.
            progress_callback: Optional async callback(current, total, message) for progress reporting.
        """
        # On resume, determine which repos need re-fetching
        skip_repos: Set[str] = set()
        existing_prs: List[Dict] = []
        if resume:
            prev_status = self._load_fetch_status()
            skip_repos = {repo for repo, status in prev_status.items() if status == 'ok'}
            # Carry forward successful status so _save_fetch_status preserves them
            for repo in skip_repos:
                self.repo_fetch_status[repo] = 'ok'
            if skip_repos:
                existing_prs = self._load_existing_prs()
                existing_prs = [pr for pr in existing_prs if pr['repo'] in skip_repos]
                failed_repos = sorted(set(prev_status.keys()) - skip_repos)
                print(f"Resuming: skipping {', '.join(sorted(skip_repos))}")
                print(f"Re-fetching: {', '.join(failed_repos)}")
                print(f"Loaded {len(existing_prs)} existing PRs")
            else:
                print("No previous fetch status found, running full fetch")

        print("Fetching HyperShift contributors...")
        if progress_callback:
            await progress_callback(0, 4, "Fetching HyperShift contributors...")

        # Get HyperShift contributors first
        self.hypershift_authors = await self.fetch_repository_contributors('openshift', 'hypershift')
        print(f"Found {len(self.hypershift_authors)} HyperShift contributors")

        print("Fetching PRs from repositories...")
        if progress_callback:
            await progress_callback(1, 4, f"Found {len(self.hypershift_authors)} contributors, fetching PRs...")

        # Build list of standard repos to fetch (excluding skipped ones)
        standard_repos = [
            ('openshift', 'hypershift'),
            ('openshift-eng', 'ai-helpers'),
            ('openshift', 'enhancements'),
        ]
        repos_to_fetch = [
            (owner, name) for owner, name in standard_repos
            if f"{owner}/{name}" not in skip_repos
        ]

        if HAS_AIOHTTP and repos_to_fetch:
            tasks = [self.fetch_prs_graphql(owner, name) for owner, name in repos_to_fetch]
            results = await asyncio.gather(*tasks, return_exceptions=True)
        else:
            results = []
            for owner, name in repos_to_fetch:
                try:
                    results.append(await self.fetch_prs_graphql(owner, name))
                except RepoFetchError as e:
                    results.append(e)

        # Map results back to repos, tracking success/failure
        repo_prs: Dict[str, List[Dict]] = {}
        for (owner, name), result in zip(repos_to_fetch, results):
            full = f"{owner}/{name}"
            if isinstance(result, BaseException):
                print(f"  {result}")
                self.repo_fetch_status[full] = 'failed'
                repo_prs[full] = []
            else:
                self.repo_fetch_status[full] = 'ok'
                repo_prs[full] = result

        hypershift_prs = repo_prs.get('openshift/hypershift', [])

        # Filter ai-helpers PRs to only HyperShift contributors
        filtered_ai_helpers = [
            pr for pr in repo_prs.get('openshift-eng/ai-helpers', [])
            if pr['author'] in self.hypershift_authors
        ]

        # Filter enhancements PRs to only HyperShift contributors
        filtered_enhancements = [
            pr for pr in repo_prs.get('openshift/enhancements', [])
            if pr['author'] in self.hypershift_authors
        ]

        if progress_callback:
            await progress_callback(2, 4, "Fetched standard repos, fetching release PRs...")

        # Fetch openshift/release PRs filtered by HyperShift-related paths
        release_prs = []
        if 'openshift/release' not in skip_repos:
            print("Fetching openshift/release PRs (filtering by HyperShift paths)...")
            try:
                release_prs = await self.fetch_release_prs_graphql()
                self.repo_fetch_status['openshift/release'] = 'ok'
            except RepoFetchError as e:
                print(f"  {e}")
                self.repo_fetch_status['openshift/release'] = 'failed'

        new_prs = hypershift_prs + filtered_ai_helpers + filtered_enhancements + release_prs
        self.prs = existing_prs + new_prs

        # Save fetch status for --resume
        self._save_fetch_status()

        failed = [r for r, s in self.repo_fetch_status.items() if s == 'failed']
        msg = f"Found {len(new_prs)} new PRs ({len(hypershift_prs)} hypershift, {len(filtered_ai_helpers)} ai-helpers, {len(filtered_enhancements)} enhancements, {len(release_prs)} release)"
        print(msg)
        if progress_callback:
            await progress_callback(4, 4, msg)
        if existing_prs:
            print(f"Total after merge: {len(self.prs)} PRs")
        if failed:
            print(f"Failed repos: {', '.join(failed)} (re-run with --resume to retry)")

    async def load_jira_hierarchy(self, progress_callback=None):
        """Load Jira hierarchy - either via direct API or from cache.

        If JIRA_EMAIL + JIRA_TOKEN are set, fetches data directly via Jira Cloud REST API.
        Otherwise, falls back to loading from cache file (populated by MCP tools).
        """
        # Extract all unique Jira tickets
        all_tickets = set()
        for pr in self.prs:
            all_tickets.update(pr['jiraTickets'])

        print(f"Found {len(all_tickets)} unique Jira tickets")

        if not all_tickets:
            self.jira_hierarchy = {}
            return

        # Check if we should use direct Jira API
        jira_client = JiraClient()

        if jira_client.enabled:
            # Fetch directly via Jira REST API
            self.jira_hierarchy = await jira_client.build_hierarchy(all_tickets, progress_callback=progress_callback)

            # Save to cache for future runs
            jira_cache_path = os.path.join(self.output_dir, 'jira_hierarchy.json')
            if self.jira_hierarchy:
                with open(jira_cache_path, 'w') as f:
                    json.dump(self.jira_hierarchy, f, indent=2)
                print(f"Saved Jira hierarchy to cache ({len(self.jira_hierarchy)} entries)")
        else:
            # Fall back to loading from cache
            jira_cache_path = os.path.join(self.output_dir, 'jira_hierarchy.json')
            print("Jira credentials not set (need JIRA_USERNAME/JIRA_API_TOKEN or JIRA_EMAIL/JIRA_TOKEN), loading from cache")
            try:
                # Check if cache is stale (older than report end date)
                cache_mtime = datetime.fromtimestamp(os.path.getmtime(jira_cache_path))
                report_end = datetime.strptime(self.end_date, '%Y-%m-%d')
                if cache_mtime < report_end:
                    print(f"Warning: Jira cache is stale (last updated {cache_mtime.strftime('%Y-%m-%d')}, "
                          f"report ends {self.end_date}). Invalidating cache.")
                    os.remove(jira_cache_path)
                    raise FileNotFoundError
                with open(jira_cache_path, 'r') as f:
                    self.jira_hierarchy = json.load(f)
                print(f"Loaded Jira hierarchy cache with {len(self.jira_hierarchy)} entries")
            except FileNotFoundError:
                print(f"Warning: Jira hierarchy cache not found at {jira_cache_path}")
                print("Set JIRA_USERNAME + JIRA_API_TOKEN (or JIRA_EMAIL + JIRA_TOKEN) to fetch Jira data directly")
                self.jira_hierarchy = {}

    def generate_report(self, output_path: str):
        """Generate markdown report"""
        print("Generating report...")

        with open(output_path, 'w') as f:
            # Header
            f.write(f"# Weekly PR Report: openshift/hypershift\n")
            f.write(f"**Period:** {self.since_date} to {self.end_date}\n")

            # Count by repo
            hypershift_count = len([pr for pr in self.prs if pr['repo'] == 'openshift/hypershift'])
            ai_helpers_count = len([pr for pr in self.prs if pr['repo'] == 'openshift-eng/ai-helpers'])
            enhancements_count = len([pr for pr in self.prs if pr['repo'] == 'openshift/enhancements'])
            release_count = len([pr for pr in self.prs if pr['repo'] == 'openshift/release'])

            f.write(f"**Total PRs:** {len(self.prs)} ({hypershift_count} hypershift, {ai_helpers_count} ai-helpers, {enhancements_count} enhancements, {release_count} release)\n\n")
            f.write("---\n\n")

            # Summary Statistics
            f.write("## Summary Statistics\n\n")
            f.write("### Repository Breakdown\n")
            f.write(f"- **openshift/hypershift:** {hypershift_count} PRs\n")
            f.write(f"- **openshift-eng/ai-helpers:** {ai_helpers_count} PRs\n")
            f.write(f"- **openshift/enhancements:** {enhancements_count} PRs\n")
            f.write(f"- **openshift/release:** {release_count} PRs\n\n")

            # Group by OCPSTRAT
            f.write("### Epic/Feature Groupings\n\n")

            # Build OCPSTRAT groups
            ocpstrat_groups = {}
            ungrouped_prs = []

            for pr in self.prs:
                grouped = False
                for ticket in pr['jiraTickets']:
                    if ticket in self.jira_hierarchy:
                        info = self.jira_hierarchy[ticket]
                        if info.get('ocpstrat'):
                            ocpstrat_key = info['ocpstrat']
                            if ocpstrat_key not in ocpstrat_groups:
                                ocpstrat_groups[ocpstrat_key] = {
                                    'summary': info.get('ocpstratSummary', 'N/A'),
                                    'prs': []
                                }
                            ocpstrat_groups[ocpstrat_key]['prs'].append(pr)
                            grouped = True
                            break

                if not grouped:
                    ungrouped_prs.append(pr)

            # Write OCPSTRAT groups (PRs sorted by title within each group)
            for ocpstrat_key in sorted(ocpstrat_groups.keys()):
                group = ocpstrat_groups[ocpstrat_key]
                f.write(f"#### [{ocpstrat_key}]({self.jira_url}/browse/{ocpstrat_key}): {group['summary']}\n")
                for pr in sorted(group['prs'], key=lambda p: p['title'].lower()):
                    f.write(f"- PR [#{pr['number']}]({pr['url']}) - {self._linkify_jira_tickets(pr['title'])}\n")
                f.write("\n")

            # Write ungrouped PRs (sorted by title)
            if ungrouped_prs:
                f.write("#### PRs without OCPSTRAT linkage\n")
                for pr in sorted(ungrouped_prs, key=lambda p: p['title'].lower()):
                    f.write(f"- PR [#{pr['number']}]({pr['url']}) ({pr['repo'].split('/')[1]}) - {self._linkify_jira_tickets(pr['title'])}\n")
                f.write("\n")

            # Timing metrics
            draft_times = [pr['draftToReadyHours'] for pr in self.prs if pr['draftToReadyHours'] is not None]
            merge_times = [pr['readyToMergeHours'] for pr in self.prs if pr['readyToMergeHours'] is not None]

            f.write("### Timing Metrics\n")
            if draft_times:
                f.write(f"- Draft→Ready: Average {sum(draft_times)/len(draft_times):.1f} hours, Median {sorted(draft_times)[len(draft_times)//2]:.1f} hours\n")
            if merge_times:
                f.write(f"- Ready→Merge: Average {sum(merge_times)/len(merge_times):.1f} hours, Median {sorted(merge_times)[len(merge_times)//2]:.1f} hours\n")

            # Fastest/slowest
            if merge_times:
                fastest_pr = min(self.prs, key=lambda pr: pr['readyToMergeHours'] or float('inf'))
                slowest_pr = max(self.prs, key=lambda pr: pr['readyToMergeHours'] or 0)
                f.write(f"- Fastest merge: PR [#{fastest_pr['number']}]({fastest_pr['url']}) ({fastest_pr['readyToMergeHours']:.1f} hours)\n")
                f.write(f"- Slowest merge: PR [#{slowest_pr['number']}]({slowest_pr['url']}) ({slowest_pr['readyToMergeHours']:.1f} hours)\n\n")

            # Review activity
            all_reviewers = {}
            for pr in self.prs:
                for reviewer in pr['reviewers']:
                    all_reviewers[reviewer] = all_reviewers.get(reviewer, 0) + 1

            f.write("### Review Activity\n")
            f.write(f"- Total unique reviewers: {len(all_reviewers)}\n")
            f.write("- Most active reviewers:\n")
            for reviewer, count in sorted(all_reviewers.items(), key=lambda x: x[1], reverse=True)[:5]:
                bot_marker = " (bot)" if self.is_bot(reviewer) else ""
                f.write(f"  - @{reviewer}: {count} PRs{bot_marker}\n")
            f.write("\n")

            # Busiest merge days
            merge_days = {}
            for pr in self.prs:
                day = pr['mergedAt'].split('T')[0]
                merge_days[day] = merge_days.get(day, 0) + 1

            f.write("### Busiest Merge Days\n")
            for day, count in sorted(merge_days.items(), key=lambda x: x[1], reverse=True)[:5]:
                f.write(f"- {day}: {count} PRs\n")
            f.write("\n")

            # Categorize PRs into sections
            bug_prs = []       # OCPBUGS tickets (any repo)
            ai_helpers_prs = []  # openshift-eng/ai-helpers
            enhancement_prs = []  # openshift/enhancements
            feature_prs = []   # Everything else (features, improvements, CI)

            for pr in self.prs:
                if pr['repo'] == 'openshift-eng/ai-helpers':
                    ai_helpers_prs.append(pr)
                elif pr['repo'] == 'openshift/enhancements':
                    enhancement_prs.append(pr)
                elif any(t.startswith('OCPBUGS') for t in pr.get('jiraTickets', [])):
                    bug_prs.append(pr)
                else:
                    feature_prs.append(pr)

            # Write each section
            self._write_section(f, "Bug Fixes (OCPBUGS)", bug_prs, show_sfdc=True)
            self._write_section(f, "Enhancements", enhancement_prs)
            self._write_section(f, "AI Helpers", ai_helpers_prs)
            self._write_section(f, "Features & Improvements", feature_prs)

        print(f"Report written to {output_path}")

    def _get_pr_sfdc_info(self, pr: Dict) -> Tuple[int, int, List[str]]:
        """Get SFDC case info for a PR from its Jira tickets.

        Returns:
            Tuple of (cases_total, cases_open, case_ids)
        """
        cases_total = 0
        cases_open = 0
        case_ids = []
        for ticket in pr.get('jiraTickets', []):
            if ticket in self.jira_hierarchy:
                info = self.jira_hierarchy[ticket]
                t = info.get('sfdcCasesTotal', 0)
                o = info.get('sfdcCasesOpen', 0)
                ids = info.get('sfdcCaseIds', [])
                if t > cases_total:
                    cases_total = t
                    cases_open = o
                    case_ids = ids
        return cases_total, cases_open, case_ids

    def _write_section(self, f, title: str, prs: List[Dict], show_sfdc: bool = False):
        """Write a report section with summary table and detailed PR list."""
        f.write(f"---\n\n## {title}\n\n")
        f.write(f"**{len(prs)} PRs**\n\n")

        if not prs:
            f.write("_No PRs in this category._\n\n")
            return

        # Summary table
        if show_sfdc:
            f.write("| PR | Author | Title | Priority | SFDC Cases | Cases Open | Case IDs |\n")
            f.write("|-----|--------|-------|----------|------------|------------|----------|\n")
        else:
            f.write("| PR | Author | Title | Priority | Repo |\n")
            f.write("|-----|--------|-------|----------|------|\n")

        sorted_prs = sorted(prs, key=lambda pr: pr['mergedAt'], reverse=True)

        for pr in sorted_prs:
            priority = self._get_pr_jira_priority(pr)
            title_text = self._linkify_jira_tickets(pr['title'][:80])
            repo_short = pr['repo'].split('/')[-1]
            pr_link = f"[#{pr['number']}]({pr['url']})"

            if show_sfdc:
                cases_total, cases_open, case_ids = self._get_pr_sfdc_info(pr)
                cases_str = str(cases_total) if cases_total else '-'
                open_str = str(cases_open) if cases_open else '-'
                ids_str = ', '.join(case_ids) if case_ids else '-'
                f.write(f"| {pr_link} | @{pr['author']} | {title_text} | {priority} | {cases_str} | {open_str} | {ids_str} |\n")
            else:
                f.write(f"| {pr_link} | @{pr['author']} | {title_text} | {priority} | {repo_short} |\n")

        f.write("\n")

        # Detailed PR list
        for pr in sorted_prs:
            f.write(f"### PR [#{pr['number']}]({pr['url']}): {self._linkify_jira_tickets(pr['title'])}\n")
            f.write(f"**Repository:** {pr['repo']}  \n")
            f.write(f"**Author:** @{pr['author']}  \n")
            f.write(f"**Merged:** {pr['mergedAt']}\n\n")

            # Topic (first line or two of body)
            if pr['body']:
                body_lines = pr['body'].strip().split('\n')
                topic = ' '.join(body_lines[:2])[:200]
                f.write(f"**Topic:** {topic}...\n\n")

            # Jira hierarchy
            if pr['jiraTickets']:
                f.write("**Jira Hierarchy:**\n")
                for ticket in pr['jiraTickets']:
                    if ticket in self.jira_hierarchy:
                        info = self.jira_hierarchy[ticket]
                        f.write(f"- Ticket: [{ticket}]({self.jira_url}/browse/{ticket}) - \"{info.get('summary', 'N/A')}\"\n")
                        if info.get('epic'):
                            f.write(f"  - Epic: [{info['epic']}]({self.jira_url}/browse/{info['epic']}) - \"{info.get('epicSummary', 'N/A')}\"\n")
                        if info.get('ocpstrat'):
                            f.write(f"  - OCPSTRAT: [{info['ocpstrat']}]({self.jira_url}/browse/{info['ocpstrat']}) - \"{info.get('ocpstratSummary', 'N/A')}\"\n")
                        # Show SFDC info if available
                        sfdc_total = info.get('sfdcCasesTotal', 0)
                        if sfdc_total:
                            sfdc_open = info.get('sfdcCasesOpen', 0)
                            sfdc_ids = info.get('sfdcCaseIds', [])
                            f.write(f"  - SFDC: {sfdc_total} cases ({sfdc_open} open)")
                            if sfdc_ids:
                                f.write(f" — Case IDs: {', '.join(sfdc_ids)}")
                            f.write("\n")
                    else:
                        f.write(f"- Ticket: [{ticket}]({self.jira_url}/browse/{ticket}) (hierarchy not available)\n")
            else:
                f.write("**Jira:** No Jira linkage\n")
            f.write("\n")

            # Reviewers and approvers
            if pr['reviewers']:
                f.write(f"**Reviewers:** {', '.join('@' + r for r in pr['reviewers'])}  \n")
            if pr['approvers']:
                f.write(f"**Approvers:** {', '.join('@' + a for a in pr['approvers'])}  \n")
            f.write("\n")

            # Timeline
            f.write("**Timeline:**\n")
            f.write(f"- Created: {pr['createdAt']}\n")
            if pr['wasDraft'] and pr['draftToReadyHours']:
                f.write(f"- Ready: {pr['readyAt']} (Draft→Ready: {pr['draftToReadyHours']:.1f} hours)\n")
            else:
                f.write(f"- Ready: Created ready\n")
            f.write(f"- Merged: {pr['mergedAt']} (Ready→Merge: {pr['readyToMergeHours']:.1f} hours)\n\n")

            # OCPSTRAT Impact
            impact = self._generate_impact_statement(pr)
            f.write(f"**OCPSTRAT Impact:** {impact}\n\n")

            # Labels
            if pr['labels']:
                f.write(f"**Labels:** {', '.join(pr['labels'])}\n")

            f.write("\n---\n\n")

    def _generate_impact_statement(self, pr: Dict) -> str:
        """Generate an OCPSTRAT impact statement based on PR data and Jira info"""
        title = pr['title'].lower()
        labels = [label.lower() for label in pr['labels']]

        # Try to use Jira ticket description for better context
        if pr['jiraTickets']:
            for ticket in pr['jiraTickets']:
                if ticket in self.jira_hierarchy:
                    jira_info = self.jira_hierarchy[ticket]

                    # Use Jira summary and description for richer impact statement
                    summary = jira_info.get('summary', '')
                    description = jira_info.get('description', '')

                    # If we have an OCPSTRAT parent, use its context
                    if jira_info.get('ocpstrat'):
                        ocpstrat_summary = jira_info.get('ocpstratSummary', '')
                        if ocpstrat_summary:
                            # Extract key goal from OCPSTRAT summary
                            return f"Advances {ocpstrat_summary}: {summary}"

                    # Otherwise use the ticket summary
                    if summary:
                        return summary

        # Fallback to label-based heuristics
        if 'critical' in labels or 'severity/critical' in labels:
            return f"Critical fix: {pr['title']}"
        elif 'bugfix' in labels or 'bug' in labels:
            return f"Resolved bug affecting {self._extract_component(pr)}"
        elif 'enhancement' in labels or 'feature' in labels:
            return f"Enhanced {self._extract_component(pr)} functionality"
        elif 'backport' in labels:
            return f"Backported critical fix for {self._extract_version(pr)}"
        else:
            return f"Improved {self._extract_component(pr)}"

    def _extract_component(self, pr: Dict) -> str:
        """Extract component from PR title or labels"""
        title = pr['title'].lower()

        # Common patterns
        if 'aws' in title: return 'AWS platform support'
        if 'azure' in title or 'aro' in title: return 'Azure platform support'
        if 'gcp' in title: return 'GCP platform support'
        if 'nodepool' in title: return 'NodePool management'
        if 'hosted' in title or 'hcp' in title: return 'hosted control plane'
        if 'cli' in title: return 'CLI tooling'
        if 'operator' in title: return 'operator functionality'
        if 'test' in title: return 'testing infrastructure'

        return 'core functionality'

    def _extract_version(self, pr: Dict) -> str:
        """Extract version from PR labels"""
        for label in pr['labels']:
            if 'backport' in label.lower():
                # Extract version like "4.20" from "backport-4.20"
                match = re.search(r'(\d+\.\d+)', label)
                if match:
                    return f"OCP {match.group(1)}"
        return 'earlier release'

    def _linkify_jira_tickets(self, text: str) -> str:
        """Convert Jira ticket IDs in text to markdown links"""
        def replace_ticket(match):
            ticket = match.group(0)
            return f"[{ticket}]({self.jira_url}/browse/{ticket})"

        return re.sub(
            r'(?:OCPBUGS|CNTRLPLANE|OCPSTRAT|RFE|HOSTEDCP)-\d+',
            replace_ticket,
            text
        )

    def save_raw_data(self, output_path: str):
        """Save raw PR data to JSON"""
        with open(output_path, 'w') as f:
            json.dump(self.prs, f, indent=2)
        print(f"Raw data saved to {output_path}")

    def save_summary_data(self, output_path: str):
        """Save compact PR summary for LLM analysis (no large body text).

        Outputs a compact format optimized for LLM consumption:
        - Stats and period info
        - PRs grouped by OCPSTRAT initiative
        - Ungrouped PRs listed separately
        - No redundant duplication of PR data
        """
        # Build compact PR records
        def compact_pr(pr: Dict) -> Dict:
            """Create a compact PR record with essential fields only."""
            # Get first OCPSTRAT from Jira tickets
            ocpstrat = None
            jira_summary = None
            for ticket in pr.get('jiraTickets', []):
                if ticket in self.jira_hierarchy:
                    h = self.jira_hierarchy[ticket]
                    jira_summary = h.get('summary', '')
                    if h.get('ocpstrat'):
                        ocpstrat = h['ocpstrat']
                        break

            # Get SFDC info
            sfdc_total, sfdc_open, sfdc_ids = self._get_pr_sfdc_info(pr)

            result = {
                'repo': pr['repo'].split('/')[-1],  # Just repo name, not owner
                'number': pr['number'],
                'title': pr['title'],
                'author': pr['author'],
                'topic': self._get_pr_topic(pr),
                'priority': self._get_pr_jira_priority(pr),
                'jira': pr.get('jiraTickets', []),
                'jiraSummary': jira_summary,
                'ocpstrat': ocpstrat,
                'mergeHours': round(pr.get('readyToMergeHours') or 0, 1),
            }

            # Only include SFDC fields when there are cases
            if sfdc_total:
                result['sfdcCasesTotal'] = sfdc_total
                result['sfdcCasesOpen'] = sfdc_open
                result['sfdcCaseIds'] = sfdc_ids

            return result

        compact_prs = [compact_pr(pr) for pr in self.prs]

        # Group by OCPSTRAT
        ocpstrat_groups = {}
        ungrouped = []

        for pr in compact_prs:
            ocpstrat = pr.get('ocpstrat')
            if ocpstrat:
                if ocpstrat not in ocpstrat_groups:
                    # Find OCPSTRAT summary from jira_hierarchy
                    ocpstrat_summary = self.jira_hierarchy.get(ocpstrat, {}).get('summary', '')
                    ocpstrat_groups[ocpstrat] = {
                        'key': ocpstrat,
                        'summary': ocpstrat_summary,
                        'prs': []
                    }
                # Remove ocpstrat from PR since it's redundant in grouped context
                pr_copy = {k: v for k, v in pr.items() if k != 'ocpstrat'}
                ocpstrat_groups[ocpstrat]['prs'].append(pr_copy)
            else:
                ungrouped.append(pr)

        # Calculate timing stats
        merge_times = [pr['mergeHours'] for pr in compact_prs if pr['mergeHours'] > 0]
        avg_merge = round(sum(merge_times) / len(merge_times), 1) if merge_times else 0

        # Find most active reviewer
        reviewer_counts = {}
        for pr in self.prs:
            for r in pr.get('reviewers', []):
                reviewer_counts[r] = reviewer_counts.get(r, 0) + 1
        top_reviewer = max(reviewer_counts.items(), key=lambda x: x[1]) if reviewer_counts else ('N/A', 0)

        # Score all PRs for deep analysis selection
        scored_prs = self.score_prs_for_deep_analysis(limit=len(self.prs))
        scored_list = [{
            'rank': i + 1,
            'score': pr['score'],
            'repo': pr['repo'].split('/')[-1],
            'number': pr['number'],
            'title': pr['title'][:80],
            'priority': pr.get('priority', '-'),
            'topic': pr.get('topic', '-'),
            'pr_id': f"{pr['repo']}#{pr['number']}",
        } for i, pr in enumerate(scored_prs)]

        output = {
            'period': f"{self.since_date} to {self.end_date}",
            'stats': {
                'total': len(compact_prs),
                'hypershift': len([p for p in compact_prs if p['repo'] == 'hypershift']),
                'ai_helpers': len([p for p in compact_prs if p['repo'] == 'ai-helpers']),
                'enhancements': len([p for p in compact_prs if p['repo'] == 'enhancements']),
                'release': len([p for p in compact_prs if p['repo'] == 'release']),
                'authors': len(set(p['author'] for p in compact_prs)),
                'avgMergeHours': avg_merge,
                'topReviewer': f"@{top_reviewer[0]} ({top_reviewer[1]} PRs)",
            },
            'initiatives': list(ocpstrat_groups.values()),
            'other': ungrouped,
            'scored': scored_list,  # Pre-scored for deep analysis selection
        }

        with open(output_path, 'w') as f:
            json.dump(output, f, indent=2)
        print(f"Summary data saved to {output_path}")

    @staticmethod
    def _parse_owners_aliases(path: str = 'OWNERS_ALIASES') -> Dict[str, Set[str]]:
        """Parse OWNERS_ALIASES YAML file into a dict of group -> set of logins."""
        import yaml
        aliases: Dict[str, Set[str]] = {}
        if not os.path.exists(path):
            return aliases
        with open(path) as f:
            data = yaml.safe_load(f)
        for group, members in (data or {}).get('aliases', {}).items():
            aliases[group] = set(members or [])
        return aliases

    @staticmethod
    def _get_hs_team(aliases: Dict[str, Set[str]]) -> Set[str]:
        """Compute the HyperShift team set from OWNERS_ALIASES groups.

        Formula: (core-approvers ∪ core-reviewers ∪ konflux-approvers) - gcp-reviewers
        """
        return ((aliases.get('core-approvers', set())
                 | aliases.get('core-reviewers', set())
                 | aliases.get('konflux-approvers', set()))
                - aliases.get('gcp-reviewers', set()))

    def generate_blog_data(self, output_dir: str):
        """Generate blog_data.json with contributor table, metrics, and pre-rendered markdown.

        Produces all the deterministic data needed for a Material-styled blog post:
        contributor counts per repo, bugfix counts, GitHub URLs, metrics summary,
        and pre-rendered markdown fragments (stats cards, tables).
        """
        start = self.since_date
        end = self.end_date

        # Determine HyperShift team from OWNERS_ALIASES
        aliases = self._parse_owners_aliases()
        hs_team = self._get_hs_team(aliases)
        if hs_team:
            print(f"  HyperShift team ({len(hs_team)} members from OWNERS_ALIASES): "
                  f"{', '.join(sorted(hs_team))}")
        else:
            print("  Warning: could not parse OWNERS_ALIASES, ai-helpers filtering disabled")

        # --- Collect per-author, per-repo PR data ---
        repo_map = {
            'openshift/hypershift': 'hypershift',
            'openshift/release': 'release',
            'openshift-eng/ai-helpers': 'ai-helpers',
            'openshift/enhancements': 'enhancements',
        }
        # author -> repo -> list of PR dicts
        author_prs: Dict[str, Dict[str, List[Dict]]] = {}
        for pr in self.prs:
            author = pr['author']
            if self.is_bot(author):
                continue
            repo = pr['repo']
            if author not in author_prs:
                author_prs[author] = {}
            if repo not in author_prs[author]:
                author_prs[author][repo] = []
            author_prs[author][repo].append(pr)

        # --- Contributor inclusion rules ---
        hs_authors = {a for a, repos in author_prs.items() if 'openshift/hypershift' in repos}
        release_authors = {a for a, repos in author_prs.items() if 'openshift/release' in repos}
        enhancement_authors = {a for a, repos in author_prs.items() if 'openshift/enhancements' in repos}
        ai_authors = {a for a, repos in author_prs.items() if 'openshift-eng/ai-helpers' in repos}

        # ai-helpers PRs only count if the author is on the HyperShift team (from OWNERS_ALIASES)
        included = hs_authors | release_authors | enhancement_authors | (ai_authors & hs_team)

        # --- Bugfix detection ---
        def is_bugfix(pr: Dict) -> bool:
            return (any('OCPBUGS' in t for t in pr.get('jiraTickets', []))
                    or 'OCPBUGS' in pr.get('title', ''))

        # --- Build GitHub URL for a count ---
        def make_url(repo: str, author: str, prs_list: List[Dict]) -> str:
            if len(prs_list) == 1:
                return prs_list[0]['url']
            q = f"is%3Apr+is%3Amerged+author%3A{author}+merged%3A{start}..{end}"
            if repo == 'openshift/release':
                q += '+hypershift'
            return f"https://github.com/{repo}/pulls?q={q}"

        def make_bug_url(author: str, bug_prs: List[Dict]) -> str:
            if len(bug_prs) == 1:
                return bug_prs[0]['url']
            return (f"https://github.com/search?q=org%3Aopenshift+is%3Apr+is%3Amerged+"
                    f"author%3A{author}+merged%3A{start}..{end}+OCPBUGS&type=pullrequests")

        # --- Build contributor records ---
        contributors = []
        for author in sorted(included):
            repos_data = {}
            total = 0
            for full_repo in repo_map:
                prs_in_repo = author_prs.get(author, {}).get(full_repo, [])
                # Only include ai-helpers if author is on hs_team
                if full_repo == 'openshift-eng/ai-helpers' and author not in hs_team:
                    continue
                count = len(prs_in_repo)
                if count > 0:
                    repos_data[full_repo] = {
                        'count': count,
                        'url': make_url(full_repo, author, prs_in_repo),
                    }
                    total += count

            bug_prs_list = [
                pr
                for repo_name, repo_prs in author_prs.get(author, {}).items()
                if not (repo_name == 'openshift-eng/ai-helpers' and author not in hs_team)
                for pr in repo_prs
                if is_bugfix(pr)
            ]
            bug_count = len(bug_prs_list)

            entry = {
                'login': author,
                'repos': repos_data,
                'bugs': {'count': bug_count, 'url': make_bug_url(author, bug_prs_list)} if bug_count else {'count': 0},
                'total': total,
            }
            # Flag release-only contributors for LLM spot-checking
            if author in release_authors and author not in (hs_authors | enhancement_authors):
                entry['release_only'] = True
                # Include their release PR numbers for easy verification
                release_prs = author_prs.get(author, {}).get('openshift/release', [])
                entry['release_pr_numbers'] = [f"openshift/release#{pr['number']}" for pr in release_prs]

            contributors.append(entry)

        contributors.sort(key=lambda c: c['total'], reverse=True)

        # --- Metrics ---
        merge_times = [pr['readyToMergeHours'] for pr in self.prs
                       if pr.get('readyToMergeHours') is not None and pr['readyToMergeHours'] > 0]
        avg_merge = round(sum(merge_times) / len(merge_times), 1) if merge_times else 0
        bot_prs = len([pr for pr in self.prs if self.is_bot(pr['author'])])

        customer_fixes = 0
        for pr in self.prs:
            sfdc_total, _, _ = self._get_pr_sfdc_info(pr)
            if sfdc_total > 0:
                customer_fixes += 1

        # Try to read breaking changes / API changes from deep analysis if available
        breaking_changes = 0
        api_changes = 0
        high_impact = 0
        for path in [os.path.join(output_dir, '.work', 'pr_deep_aggregated.json'),
                     os.path.join('.work', 'pr_deep_aggregated.json')]:
            if os.path.exists(path):
                try:
                    with open(path) as f:
                        agg = json.load(f)
                    summary = agg.get('summary', {})
                    breaking_changes = summary.get('breaking_changes_count', 0)
                    api_changes = summary.get('api_changes_count', 0)
                    high_impact = summary.get('high_impact_count', 0)
                    break
                except (json.JSONDecodeError, KeyError):
                    pass

        by_repo = {}
        for full_repo in repo_map:
            by_repo[full_repo] = len([pr for pr in self.prs if pr['repo'] == full_repo])

        reviewer_counts: Dict[str, int] = {}
        for pr in self.prs:
            for r in pr.get('reviewers', []):
                if self.is_bot(r):
                    continue
                reviewer_counts[r] = reviewer_counts.get(r, 0) + 1
        top_reviewers = sorted(reviewer_counts.items(), key=lambda x: x[1], reverse=True)[:5]

        stats = {
            'total_prs': len(self.prs),
            'contributor_count': len(contributors),
            'bot_prs': bot_prs,
            'breaking_changes': breaking_changes,
            'api_changes': api_changes,
            'avg_merge_hours': avg_merge,
            'high_impact': high_impact,
            'customer_reported_fixes': customer_fixes,
            'by_repo': by_repo,
        }

        # --- Pre-render markdown fragments ---

        lg = '{ .lg }'
        stats_cards = (
            '<div class="grid cards" markdown>\n\n'
            f'-   :octicons-git-pull-request-24:{lg} **{stats["total_prs"]}** PRs merged\n'
            f'-   :octicons-people-24:{lg} **{stats["contributor_count"]}** contributors\n'
            f'-   :octicons-alert-24:{lg} **{stats["breaking_changes"]}** breaking changes\n'
            f'-   :octicons-clock-24:{lg} **{stats["avg_merge_hours"]}h** avg merge time\n\n'
            '</div>'
        )

        metrics_rows = [
            ('Total PRs merged', stats['total_prs']),
            ('Unique contributors', stats['contributor_count']),
            ('Bot PRs', stats['bot_prs']),
            ('HyperShift repo PRs', by_repo.get('openshift/hypershift', 0)),
            ('Release repo PRs', by_repo.get('openshift/release', 0)),
            ('AI-helpers repo PRs', by_repo.get('openshift-eng/ai-helpers', 0)),
            ('Enhancement proposals', by_repo.get('openshift/enhancements', 0)),
            ('Average merge time', f'{avg_merge} hours'),
            ('High-impact PRs', high_impact),
            ('Breaking changes', breaking_changes),
            ('API changes', api_changes),
            ('Customer-reported fixes', customer_fixes),
        ]
        metrics_table = '| Metric | Value |\n|--------|-------|\n'
        for label, value in metrics_rows:
            metrics_table += f'| {label} | {value} |\n'

        reviewers_table = '| Reviewer | PRs Reviewed |\n|----------|-------------|\n'
        for login, count in top_reviewers:
            reviewers_table += f'| [@{login}](https://github.com/{login}) | {count} |\n'

        contrib_header = ('| Contributor | hypershift | release | ai-helpers | enhancements '
                          '| :material-bug: bugs | Total |\n'
                          '|------------|:-:|:-:|:-:|:-:|:-:|:-:|\n')
        contrib_rows = ''
        for c in contributors:
            row_cells = [f'| [@{c["login"]}](https://github.com/{c["login"]})']
            for full_repo in ['openshift/hypershift', 'openshift/release',
                              'openshift-eng/ai-helpers', 'openshift/enhancements']:
                repo_data = c['repos'].get(full_repo)
                if repo_data:
                    row_cells.append(f'[{repo_data["count"]}]({repo_data["url"]})')
                else:
                    row_cells.append('')
            if c['bugs']['count'] > 0:
                row_cells.append(f'[{c["bugs"]["count"]}]({c["bugs"]["url"]})')
            else:
                row_cells.append('')
            row_cells.append(f'**{c["total"]}**')
            contrib_rows += ' | '.join(row_cells) + ' |\n'

        contributor_table = contrib_header + contrib_rows

        # --- Assemble output ---
        output = {
            'period': {'start': start, 'end': end},
            'stats': stats,
            'hs_team': sorted(hs_team),
            'top_reviewers': [{'login': login, 'count': count} for login, count in top_reviewers],
            'contributors': contributors,
            'markdown': {
                'stats_cards': stats_cards,
                'metrics_table': metrics_table,
                'top_reviewers_table': reviewers_table,
                'contributor_table': contributor_table,
            },
        }

        blog_data_path = os.path.join(output_dir, 'blog_data.json')
        with open(blog_data_path, 'w') as f:
            json.dump(output, f, indent=2)
        print(f"Blog data saved to {blog_data_path}")

    def parse_pr_identifiers(self, pr_ids: List[str]) -> List[Tuple[str, int]]:
        """Parse PR identifiers in owner/repo#number format.

        Args:
            pr_ids: List of strings like "openshift/hypershift#7709"

        Returns:
            List of (repo, pr_number) tuples
        """
        parsed = []
        for pr_id in pr_ids:
            match = re.match(r'^([^/]+/[^#]+)#(\d+)$', pr_id)
            if match:
                repo = match.group(1)
                pr_num = int(match.group(2))
                parsed.append((repo, pr_num))
            else:
                print(f"Warning: Invalid PR format '{pr_id}', expected owner/repo#number")
        return parsed

    def _parse_conventional_commit(self, title: str) -> Tuple[Optional[str], Optional[str]]:
        """Parse conventional commit type and scope from PR title.

        Conventional commit format: type(scope): description
        Or with Jira prefix: TICKET-123: type(scope): description

        Returns:
            Tuple of (type, scope) or (None, None) if not found
        """
        # Strip Jira ticket prefix if present (e.g., "CNTRLPLANE-123: ")
        title_stripped = re.sub(r'^(?:\[.*?\]\s*)?(?:[A-Z]+-\d+:\s*)+', '', title)

        # Match conventional commit: type(scope): or type:
        match = re.match(r'^(\w+)(?:\(([^)]+)\))?:\s*', title_stripped)
        if match:
            commit_type = match.group(1).lower()
            scope = match.group(2).lower() if match.group(2) else None
            return commit_type, scope

        return None, None

    def _get_pr_topic(self, pr: Dict) -> str:
        """Derive topic category from PR title using conventional commit format."""
        title = pr.get('title', '')

        # Try to parse conventional commit format first
        commit_type, scope = self._parse_conventional_commit(title)

        if commit_type:
            # Return type with scope if available (e.g., "feat:aws", "fix:nodepool")
            if scope:
                return f"{commit_type}:{scope}"
            return commit_type

        # Fallback: check for OCPBUGS (bug fix)
        if any(t.startswith('OCPBUGS') for t in pr.get('jiraTickets', [])):
            return 'fix'

        # Fallback: CI for release repo
        if pr['repo'] == 'openshift/release':
            return 'ci'

        # Enhancement proposals
        if pr['repo'] == 'openshift/enhancements':
            return 'enhancement'

        return '-'

    def _get_pr_jira_priority(self, pr: Dict) -> str:
        """Get highest Jira priority from PR's tickets."""
        priority_order = ['Critical', 'Blocker', 'Major', 'Normal', 'Minor', 'Undefined']
        highest_priority = '-'
        highest_idx = len(priority_order)

        for ticket in pr.get('jiraTickets', []):
            if ticket in self.jira_hierarchy:
                priority = self.jira_hierarchy[ticket].get('priority', 'Undefined')
                try:
                    idx = priority_order.index(priority)
                    if idx < highest_idx:
                        highest_idx = idx
                        highest_priority = priority
                except ValueError:
                    pass

        return highest_priority

    def score_prs_for_deep_analysis(self, limit: int = 20) -> List[Dict]:
        """Score PRs by importance for deep analysis selection.

        Scoring criteria (higher = more important):
        - Enhancement proposals (openshift/enhancements): +200 points (always selected)
        - Jira priority: Critical=100, Blocker=100, Major=50, Normal=20, Minor=10
        - SDK/API/migration work: +30 points
        - Feature work (feat in title): +15 points
        - Bug fixes (OCPBUGS): +10 points
        - Has Jira ticket: +5 points
        - Non-bot author in openshift/release: +10 points

        Args:
            limit: Maximum number of PRs to return

        Returns:
            List of PR dicts with 'score', 'score_reasons', 'topic', and 'priority' fields,
            sorted by score descending
        """
        # Priority scores (higher = more important)
        priority_scores = {
            'Critical': 100,
            'Blocker': 100,
            'Major': 50,
            'Normal': 20,
            'Minor': 10,
            'Undefined': 5,
        }

        scored_prs = []

        for pr in self.prs:
            score = 0
            reasons = []

            # Get topic and priority for display
            topic = self._get_pr_topic(pr)
            priority = self._get_pr_jira_priority(pr)

            # Score based on highest Jira ticket priority (once per PR)
            if priority != '-':
                pscore = priority_scores.get(priority, 5)
                score += pscore
                if pscore >= 50:
                    reasons.append(priority)

            # Bonus for SDK/API/migration work (significant changes)
            title_lower = pr.get('title', '').lower()
            if any(term in title_lower for term in ['sdk', 'migrate', 'api', 'breaking']):
                score += 30
                reasons.append('SDK/API')
            elif 'feat' in title_lower or 'feature' in title_lower:
                score += 15
                reasons.append('feature')

            # Bug fixes get moderate priority
            if any(t.startswith('OCPBUGS') for t in pr.get('jiraTickets', [])):
                score += 10
                reasons.append('bugfix')

            # Having any Jira ticket is better than none
            if pr.get('jiraTickets'):
                score += 5

            # Enhancement proposals are always high-priority for deep analysis
            if pr['repo'] == 'openshift/enhancements':
                score += 200
                reasons.append('enhancement')

            # For release repo, prefer non-bot PRs (manual CI changes)
            if pr['repo'] == 'openshift/release':
                if not self.is_bot(pr.get('author', '')):
                    score += 10
                    reasons.append('manual-CI')

            # Store score in PR
            pr_copy = pr.copy()
            pr_copy['score'] = score
            pr_copy['score_reasons'] = reasons
            pr_copy['topic'] = topic
            pr_copy['priority'] = priority
            scored_prs.append(pr_copy)

        # Sort by score descending
        scored_prs.sort(key=lambda x: -x['score'])

        return scored_prs[:limit]

    def print_scored_prs(self, scored_prs: List[Dict]):
        """Print scored PRs in a readable format with priority, author, and topic."""
        print("\nPR Selection by Importance Score:")
        print("-" * 140)
        print(f"{'#':<3} {'Score':<6} {'Priority':<10} {'Author':<20} {'Repo':<12} {'PR':<7} {'Topic':<14} {'Title':<50}")
        print("-" * 140)

        for i, pr in enumerate(scored_prs, 1):
            repo_short = pr['repo'].replace('openshift/', '').replace('openshift-eng/', '')
            title_trunc = pr['title'][:48] + '..' if len(pr['title']) > 50 else pr['title']
            topic = pr.get('topic', '-')[:14]
            author = pr.get('author', '-')[:18]
            print(f"{i:<3} {pr['score']:<6} {pr.get('priority', '-'):<10} {author:<20} {repo_short:<12} #{pr['number']:<6} {topic:<14} {title_trunc}")

        print("-" * 140)
        print(f"Selected {len(scored_prs)} PRs for deep analysis\n")

    async def fetch_pr_diffs(self, pr_list: List[Tuple[str, int]], progress_callback=None) -> Dict[str, Dict]:
        """Fetch diffs for selected PRs via GitHub REST API.

        Args:
            pr_list: List of (repo, pr_number) tuples
            progress_callback: Optional async callback(current, total, message) for progress reporting.

        Returns:
            Dict mapping "owner_repo_number" key to diff data
        """
        headers = {
            "Authorization": f"Bearer {self.github_token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28"
        }

        async def fetch_single_diff(session, repo: str, number: int) -> Dict:
            """Fetch diff for a single PR with pagination."""
            url = f"https://api.github.com/repos/{repo}/pulls/{number}/files"
            per_page = 100

            try:
                files = []
                page = 1
                while True:
                    params = {"per_page": per_page, "page": page}
                    if HAS_AIOHTTP:
                        async with session.get(url, headers=headers, params=params) as response:
                            if response.status != 200:
                                return {'repo': repo, 'number': number, 'error': f"HTTP {response.status}"}
                            batch = await response.json()
                    else:
                        response = requests.get(url, headers=headers, params=params, timeout=30)
                        if response.status_code != 200:
                            return {'repo': repo, 'number': number, 'error': f"HTTP {response.status_code}"}
                        batch = response.json()

                    files.extend(batch)
                    if len(batch) < per_page:
                        break
                    page += 1

                # Process file patches, skip vendor dirs, truncate large ones
                patches = []
                for f in files:
                    filename = f['filename']
                    dirs = set(filename.split('/')[:-1])
                    if 'vendor' in dirs:
                        continue
                    patch = f.get('patch', '')
                    if len(patch) > 5000:
                        patch = patch[:5000] + "\n... [truncated]"
                    patches.append({
                        'filename': filename,
                        'status': f['status'],
                        'additions': f['additions'],
                        'deletions': f['deletions'],
                        'patch': patch
                    })

                return {
                    'repo': repo,
                    'number': number,
                    'files': patches,
                    'total_additions': sum(f['additions'] for f in files),
                    'total_deletions': sum(f['deletions'] for f in files),
                    'total_files': len(files)
                }
            except Exception as e:
                return {'repo': repo, 'number': number, 'error': str(e)}

        results = {}
        completed = 0
        total = len(pr_list)

        async def fetch_and_report(session, repo, num):
            nonlocal completed
            r = await fetch_single_diff(session, repo, num)
            completed += 1
            if progress_callback:
                await progress_callback(completed, total, f"Diff {completed}/{total}: {repo}#{num}")
            return r

        if HAS_AIOHTTP:
            async with aiohttp.ClientSession() as session:
                tasks = [fetch_and_report(session, repo, num) for repo, num in pr_list]
                responses = await asyncio.gather(*tasks)
                for r in responses:
                    key = f"{r['repo'].replace('/', '_')}_{r['number']}"
                    results[key] = r
        else:
            for repo, num in pr_list:
                r = await fetch_and_report(None, repo, num)
                key = f"{repo.replace('/', '_')}_{num}"
                results[key] = r

        errors = [k for k, v in results.items() if 'error' in v]
        if errors:
            print(f"  Warning: Failed to fetch {len(errors)} PRs")

        return results

    def write_deep_pr_files(self, diffs: Dict[str, Dict], output_dir: str = '.work/pr_deep'):
        """Write per-PR JSON files combining metadata + Jira + diff.

        Args:
            diffs: Dict from fetch_pr_diffs()
            output_dir: Directory to write files
        """
        os.makedirs(output_dir, exist_ok=True)

        written = 0
        for key, diff_data in diffs.items():
            if 'error' in diff_data:
                continue

            repo = diff_data['repo']
            number = diff_data['number']

            # Find matching PR metadata
            pr = next((p for p in self.prs if p['repo'] == repo and p['number'] == number), None)

            if not pr:
                print(f"  Warning: No metadata found for {repo}#{number}")
                continue

            # Build combined JSON
            combined = {
                'repo': pr['repo'],
                'number': pr['number'],
                'title': pr['title'],
                'url': pr['url'],
                'author': pr['author'],
                'body': pr['body'],
                'jiraTickets': pr['jiraTickets'],
                'labels': pr['labels'],
                'mergedAt': pr['mergedAt'],
                'jiraHierarchy': {t: self.jira_hierarchy[t] for t in pr['jiraTickets'] if t in self.jira_hierarchy},
                'diff': {
                    'files': diff_data.get('files', []),
                    'total_additions': diff_data.get('total_additions', 0),
                    'total_deletions': diff_data.get('total_deletions', 0),
                    'total_files': diff_data.get('total_files', 0)
                }
            }

            filepath = os.path.join(output_dir, f"{key}.json")
            with open(filepath, 'w') as f:
                json.dump(combined, f, indent=2)
            written += 1

        print(f"Wrote {written} per-PR JSON files to {output_dir}/")

    async def analyze_prs_with_llm(self, deep_dir: str, metadata_keys: set = None,
                                    analyze_keys: set = None, progress_callback=None):
        """Analyze PR diffs using direct Anthropic API calls (Sonnet).

        Args:
            deep_dir: Directory containing PR JSON files from write_deep_pr_files()
            metadata_keys: Set of file keys that should get metadata-only analysis (no diff in prompt)
            analyze_keys: If set, only analyze files whose key is in this set (prevents
                          leftover files from previous runs being analyzed). None = all files.
            progress_callback: Optional async callback(current, total, message) for progress reporting.
        """
        if metadata_keys is None:
            metadata_keys = set()

        # Find PR files to analyze
        import glob
        pr_files = sorted(glob.glob(os.path.join(deep_dir, '*.json')))
        pr_files = [f for f in pr_files if not f.endswith('_analysis.json')]

        # Filter to only selected keys when provided
        if analyze_keys is not None:
            pr_files = [f for f in pr_files
                        if os.path.basename(f).replace('.json', '') in analyze_keys]

        # Skip already analyzed (resume support)
        to_analyze = []
        for f in pr_files:
            analysis_path = f.replace('.json', '_analysis.json')
            if os.path.exists(analysis_path):
                print(f"  Skipping {os.path.basename(f)} (already analyzed)")
            else:
                to_analyze.append(f)

        if not to_analyze:
            print("All PRs already analyzed. Use --no-resume to re-analyze.")
            return

        # Use Vertex AI if configured, otherwise direct Anthropic API
        vertex_project = os.environ.get('ANTHROPIC_VERTEX_PROJECT_ID') or os.environ.get('GOOGLE_CLOUD_PROJECT')
        vertex_region = os.environ.get('CLOUD_ML_REGION', 'us-east5')
        if vertex_project:
            client = anthropic.AsyncAnthropicVertex(project_id=vertex_project, region=vertex_region)
            backend = f"Vertex AI ({vertex_region})"
        else:
            client = anthropic.AsyncAnthropic()
            backend = "Anthropic API"

        print(f"\nAnalyzing {len(to_analyze)} PRs with Sonnet via {backend}...")

        semaphore = asyncio.Semaphore(10)
        total_input_tokens = 0
        total_output_tokens = 0
        total_cache_write_tokens = 0
        total_cache_read_tokens = 0
        actual_model = None

        ANALYSIS_SYSTEM_PROMPT = """You are a code review analyst. Analyze the PR data provided and output a JSON object with exactly these fields:

{
  "repo": "owner/repo",
  "number": 1234,
  "author": "github-username",
  "summary": "One sentence describing actual code changes",
  "actual_changes": ["Change 1", "Change 2"],
  "alignment_with_description": "matches" or "partial" or "misleading",
  "breaking_changes": ["Breaking change 1"] or [],
  "test_coverage": "Description of test changes" or "none",
  "api_changes": true or false,
  "files_changed": {"total": 5, "by_type": {"go": 3, "yaml": 2}},
  "notable_observations": ["Observation 1"],
  "impact_level": "high" or "medium" or "low",
  "impact_statement": "One sentence business/user impact"
}

CRITICAL: Use the "author" field from the input data exactly. Never guess authors.
Output ONLY the JSON object, no markdown fences, no explanation."""

        async def analyze_single(filepath: str):
            nonlocal total_input_tokens, total_output_tokens, total_cache_write_tokens, total_cache_read_tokens, actual_model
            basename = os.path.basename(filepath)
            key = basename.replace('.json', '')

            with open(filepath) as f:
                pr_data = json.load(f)

            # For metadata-only analysis, strip the diff to save tokens
            is_light = key in metadata_keys
            if is_light:
                pr_data_for_prompt = {
                    'repo': pr_data.get('repo', ''),
                    'number': pr_data.get('number', 0),
                    'title': pr_data.get('title', ''),
                    'author': pr_data.get('author', ''),
                    'body': pr_data.get('body', ''),
                    'labels': pr_data.get('labels', []),
                    'jiraTickets': pr_data.get('jiraTickets', []),
                    'jiraHierarchy': pr_data.get('jiraHierarchy', {}),
                }
                user_prompt = f"Analyze this PR based on its description (no diff available):\n\n{json.dumps(pr_data_for_prompt, indent=2)}"
            else:
                user_prompt = f"Analyze this PR diff:\n\n{json.dumps(pr_data, indent=2)}"

            async with semaphore:
                try:
                    response = await client.messages.create(
                        model="claude-sonnet-5",
                        max_tokens=1024,
                        system=ANALYSIS_SYSTEM_PROMPT,
                        messages=[{"role": "user", "content": user_prompt}],
                    )

                    total_input_tokens += response.usage.input_tokens
                    total_output_tokens += response.usage.output_tokens
                    total_cache_write_tokens += getattr(response.usage, 'cache_creation_input_tokens', 0) or 0
                    total_cache_read_tokens += getattr(response.usage, 'cache_read_input_tokens', 0) or 0

                    # Capture actual model name from first successful response
                    if actual_model is None:
                        actual_model = response.model

                    # Parse the JSON response
                    response_text = response.content[0].text.strip()
                    # Handle potential markdown fences
                    if response_text.startswith('```'):
                        response_text = response_text.split('\n', 1)[1]
                        if response_text.endswith('```'):
                            response_text = response_text[:-3].strip()

                    analysis = json.loads(response_text)

                    # Write analysis file
                    analysis_path = filepath.replace('.json', '_analysis.json')
                    with open(analysis_path, 'w') as f:
                        json.dump(analysis, f, indent=2)

                    mode = "L" if is_light else "D"
                    print(f"  [{mode}] {basename}: {analysis.get('impact_level', '?')} impact - {analysis.get('summary', '?')[:60]}")
                    return analysis

                except json.JSONDecodeError as e:
                    print(f"  ERROR {basename}: Failed to parse LLM response as JSON: {e}")
                    return None
                except Exception as e:
                    print(f"  ERROR {basename}: {e}")
                    return None

        completed_count = 0

        async def analyze_and_report(filepath: str):
            nonlocal completed_count
            result = await analyze_single(filepath)
            completed_count += 1
            if progress_callback:
                await progress_callback(
                    completed_count, len(to_analyze),
                    f"Analyzed {os.path.basename(filepath)}"
                )
            return result

        # Run all analyses concurrently (semaphore limits to 10 at a time)
        results = await asyncio.gather(*[analyze_and_report(f) for f in to_analyze])

        successful = [r for r in results if r is not None]
        failed = len(results) - len(successful)

        # Fetch pricing for the actual model used
        is_vertex = vertex_project is not None
        pricing = fetch_model_pricing(actual_model or "claude-sonnet-5", is_vertex=is_vertex)

        print(f"\n  Analysis complete:")
        print(f"    Successful: {len(successful)}/{len(results)}")
        if failed:
            print(f"    Failed:     {failed}")

        # Display model name
        model_display = actual_model or "claude-sonnet-5"
        if is_vertex:
            model_display += " (Vertex AI)"
        print(f"    Model:      {model_display}")

        # Display token counts
        token_parts = [f"{total_input_tokens:,} input", f"{total_output_tokens:,} output"]
        if total_cache_write_tokens > 0:
            token_parts.append(f"{total_cache_write_tokens:,} cache write")
        if total_cache_read_tokens > 0:
            token_parts.append(f"{total_cache_read_tokens:,} cache read")
        print(f"    Tokens:     {' + '.join(token_parts)}")

        # Display cost estimate if pricing is available
        if pricing:
            input_cost = total_input_tokens * pricing['input'] / 1_000_000
            output_cost = total_output_tokens * pricing['output'] / 1_000_000
            cache_write_cost = total_cache_write_tokens * pricing['cache_write'] / 1_000_000
            cache_read_cost = total_cache_read_tokens * pricing['cache_read'] / 1_000_000
            total_cost = input_cost + output_cost + cache_write_cost + cache_read_cost

            cost_parts = [f"${input_cost:.2f} input", f"${output_cost:.2f} output"]
            if cache_write_cost > 0:
                cost_parts.append(f"${cache_write_cost:.2f} cache write")
            if cache_read_cost > 0:
                cost_parts.append(f"${cache_read_cost:.2f} cache read")
            print(f"    Est. cost:  ${total_cost:.2f} ({' + '.join(cost_parts)})")
        else:
            print(f"    (pricing unavailable — see https://docs.anthropic.com/en/docs/about-claude/pricing)")

        # Aggregate all analyses
        self.aggregate_analyses(deep_dir)

    def aggregate_analyses(self, deep_dir: str):
        """Aggregate all _analysis.json files into pr_deep_aggregated.json."""
        import glob
        from datetime import datetime

        analysis_files = sorted(glob.glob(os.path.join(deep_dir, '*_analysis.json')))
        analyses = []
        for f in analysis_files:
            try:
                with open(f) as fh:
                    analyses.append(json.load(fh))
            except (json.JSONDecodeError, IOError) as e:
                print(f"  Warning: Could not read {os.path.basename(f)}: {e}")

        aggregated = {
            'generated_at': datetime.utcnow().isoformat() + 'Z',
            'prs_analyzed': len(analyses),
            'analyses': analyses,
            'summary': {
                'breaking_changes_count': sum(1 for a in analyses if a.get('breaking_changes')),
                'api_changes_count': sum(1 for a in analyses if a.get('api_changes')),
                'high_impact_count': sum(1 for a in analyses if a.get('impact_level') == 'high'),
            }
        }

        output_path = os.path.join(os.path.dirname(deep_dir), 'pr_deep_aggregated.json')
        with open(output_path, 'w') as f:
            json.dump(aggregated, f, indent=2)

        print(f"\n  Aggregated {len(analyses)} analyses to {output_path}")
        print(f"    Breaking changes: {aggregated['summary']['breaking_changes_count']}")
        print(f"    API changes:      {aggregated['summary']['api_changes_count']}")
        print(f"    High impact:      {aggregated['summary']['high_impact_count']}")


BLOG_PROMPT_TEMPLATE = """You are writing a monthly progress report blog post for the HyperShift project.

## Input Files

Read these files to understand what happened this month:
1. {aggregated_path} — aggregated PR analysis with per-PR summaries, breaking changes, and impact levels
2. {blog_data_path} — contributor tables, metrics, and pre-rendered markdown sections (stats_cards, metrics_table, top_reviewers_table, contributor_table)

## Style Reference

Read the most recent blog post for styling reference:
- {template_path}

## Date Range

Period: {start_date} to {end_date}
Total PRs merged: {pr_count}
Contributors: {contributor_count}

## Writing Style Guide

1. **Problem-first storytelling**: Don't just say what changed — explain the problem that existed before, why it mattered, and how the change addresses it. Give readers the "why" before the "what."

2. **Conversational but authoritative tone**: Write like a knowledgeable engineer explaining work to interested peers over coffee. Avoid marketing language and buzzwords. Be direct, specific, and occasionally witty.

3. **Technical depth with accessibility**: Go deep on the technical details — show code patterns, explain algorithms, discuss trade-offs. But structure explanations so readers can follow even if they're not experts in that specific area.

4. **Historical context**: When relevant, explain what the previous approach was and why it's being changed. "The old emptyBucket function relied on X, which had problems Y and Z. The new approach does W instead."

5. **Credit contributors by GitHub handle**: Use @username format, sourced from the `author` field in the PR data JSON. NEVER guess or infer authors from PR descriptions or code content.

6. **Thematic grouping over chronological listing**: Group related changes into coherent narratives rather than listing PRs in order. A single section might cover 1-3 related PRs that tell one story.

7. **Highlight interesting edge cases and trade-offs**: Readers love learning about subtle problems — TLS ServerName workarounds, race conditions, pre-stable dependencies. These are what make the report worth reading beyond just a changelog.

8. **Don't cover everything**: Select 5-8 of the most interesting/impactful changes for deep narrative treatment. Minor fixes and routine maintenance can be briefly mentioned or grouped into a "Beneath the Headlines" section.

## Instructions

Follow the structure, formatting, and MkDocs Material styling from the style reference template exactly. Pre-rendered markdown sections in blog_data.json (stats_cards, metrics_table, top_reviewers_table, contributor_table) should be inserted verbatim.

The output file is docs/content/blog/{blog_filename}.

### Phase 0: Story selection (interactive)

Read the input files. Then propose candidate stories in two tiers:

**Deep stories** (5-8 candidates for full narrative treatment — 3-8 paragraphs each):
```
1. [Title] — [one-sentence pitch explaining why it's interesting] (PRs: #NNN, #NNN)
2. ...
```

**Beneath the Headlines** (5-10 candidates for paragraph-length coverage):
```
A. [Title] — [one-sentence pitch] (PRs: #NNN)
B. ...
```

After listing them, ask the user which deep stories to develop and which smaller items to include (e.g. "Develop deep stories 1,3,5,7 and beneath-the-headlines A,C,D,F?"). Wait for the user's selection before proceeding.

### Phase 1: Write the blog post

Based on the user's story selection, write the full blog post following the Writing Style Guide above and the structure from the style reference template. Write directly to docs/content/blog/{blog_filename}.

### Phase 2: Update site navigation

1. Update docs/content/blog/index.md — add new card entry at the TOP of the grid (after the opening `<div class="grid cards" markdown>` line), using this template:

    -   :material-newspaper-variant-outline:{{{{ .lg .middle }}}} **[Month] [Year] Progress Report**

        ---

        [Description matching the frontmatter description]. [N] PRs from [N] contributors.

        [:octicons-arrow-right-24: Read the report]({blog_filename})

2. Update docs/mkdocs.yml — add new blog entry to nav after blog/index.md, before older entries.

### Phase 3: Preview

1. Run `make docs-aggregate`
2. Start `cd docs && mkdocs serve` for user preview
3. Iterate with the user on edits

## Sensitive Content Filtering (blog is public)

- S360 references → "compliance"
- Remove SFDC case count/link references
- Don't include internal-only Jira links
- Don't mention specific customer names
"""


class SelectionScreen(Screen):
    """Interactive PR selection screen used both standalone and inside PipelineApp.

    Categories:
      D = Deep:     fetch diff + LLM analysis now
      Z = Lazy:     fetch diff now, LLM analysis deferred
      M = Metadata: no diff, LLM analyzes description only
      I = Ignore:   skip entirely
    """

    CSS = """
    DataTable {
        height: 1fr;
    }
    #status-bar {
        dock: bottom;
        height: 1;
        background: $accent;
        color: $text;
        padding: 0 1;
    }
    """

    BINDINGS = [
        Binding("d", "set_category('D')", "Deep", show=True),
        Binding("z", "set_category('Z')", "Lazy", show=True),
        Binding("m", "set_category('M')", "Metadata", show=True),
        Binding("i", "set_category('I')", "Ignore", show=True),
        Binding("enter", "confirm", "Confirm", show=True, priority=True),
        Binding("q", "cancel", "Cancel", show=True),
    ]

    def __init__(self, scored_prs: list):
        super().__init__()
        self.scored_prs = scored_prs
        self.categories = {}
        self._col_keys = None
        for pr in scored_prs:
            key = f"{pr['repo']}#{pr['number']}"
            score = pr.get('score', 0)
            if score >= 50:
                self.categories[key] = 'D'
            elif score >= 10:
                self.categories[key] = 'Z'
            else:
                self.categories[key] = 'I'

    def compose(self) -> ComposeResult:
        yield Header()
        yield DataTable(id="pr-table")
        yield Static(id="status-bar")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one(DataTable)
        table.cursor_type = "row"
        self._col_keys = table.add_columns("Cat", "Score", "Priority", "Repo", "PR#", "Author", "Title")
        for pr in self.scored_prs:
            key = f"{pr['repo']}#{pr['number']}"
            cat = self.categories[key]
            repo_short = pr['repo'].split('/')[-1] if '/' in pr['repo'] else pr['repo']
            table.add_row(
                cat,
                str(pr.get('score', 0)),
                pr.get('priority', '-'),
                repo_short,
                str(pr['number']),
                pr.get('author', ''),
                pr.get('title', '')[:80],
                key=key,
            )
        self._update_status()

    def action_set_category(self, category: str) -> None:
        table = self.query_one(DataTable)
        if table.cursor_row is not None:
            row_key, _ = table.coordinate_to_cell_key(table.cursor_coordinate)
            key = row_key.value
            self.categories[key] = category
            table.update_cell(row_key, self._col_keys[0], category)
            if table.cursor_row < table.row_count - 1:
                table.move_cursor(row=table.cursor_row + 1)
            self._update_status()

    def _update_status(self) -> None:
        counts = {'D': 0, 'Z': 0, 'M': 0, 'I': 0}
        for cat in self.categories.values():
            counts[cat] = counts.get(cat, 0) + 1
        est_cost = counts['D'] * 0.03 + counts['M'] * 0.003
        status = self.query_one("#status-bar", Static)
        status.update(
            f"  D(eep): {counts['D']}  Z(lazy): {counts['Z']}  "
            f"M(etadata): {counts['M']}  I(gnore): {counts['I']}  │  "
            f"Est. LLM cost: ~${est_cost:.2f}  │  Enter=confirm  q=cancel"
        )

    def action_confirm(self) -> None:
        result = {'deep': [], 'lazy': [], 'metadata': [], 'ignore': []}
        for pr in self.scored_prs:
            key = f"{pr['repo']}#{pr['number']}"
            cat = self.categories.get(key, 'I')
            if cat == 'D':
                result['deep'].append(pr)
            elif cat == 'Z':
                result['lazy'].append(pr)
            elif cat == 'M':
                result['metadata'].append(pr)
            else:
                result['ignore'].append(pr)
        self.dismiss(result)

    def action_cancel(self) -> None:
        self.dismiss(None)


class PRSelectorApp(App):
    """Standalone app wrapper around SelectionScreen (kept for backwards compat)."""

    def __init__(self, scored_prs: list):
        super().__init__()
        self.scored_prs = scored_prs
        self.result = None

    def on_mount(self) -> None:
        def handle_result(result):
            self.result = result
            self.exit(result)
        self.push_screen(SelectionScreen(self.scored_prs), handle_result)


class PipelineApp(App):
    """Full-pipeline TUI: fetching → Jira → reports → selection → diffs → LLM."""

    CSS = """
    #phase-label {
        height: 1;
        padding: 0 1;
        background: $accent;
        color: $text;
    }
    #progress {
        height: 1;
        margin: 0 1;
    }
    #log {
        height: 1fr;
    }
    """

    BINDINGS = [
        Binding("ctrl+c", "quit", "Quit", show=False),
    ]

    def __init__(self, generator: 'PRReportGenerator', args):
        super().__init__()
        self.generator = generator
        self.args = args
        self.pipeline_result = None

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static("Initializing...", id="phase-label")
        yield ProgressBar(id="progress", total=100)
        yield RichLog(id="log", highlight=True, markup=True)
        yield Footer()

    def on_mount(self) -> None:
        self.run_worker(self._run_pipeline(), exclusive=True)

    def _set_phase(self, phase: str, total: int = 100) -> None:
        self.query_one("#phase-label", Static).update(f"[bold]{phase}[/bold]")
        pb = self.query_one("#progress", ProgressBar)
        pb.total = total
        pb.progress = 0

    def _log(self, message: str) -> None:
        self.query_one(RichLog).write(message)

    def _make_callback(self):
        async def callback(current: int, total: int, message: str) -> None:
            pb = self.query_one("#progress", ProgressBar)
            pb.total = total
            pb.progress = current
            self.query_one(RichLog).write(message)
        return callback

    async def _run_pipeline(self) -> None:
        args = self.args
        generator = self.generator
        output_dir = args.output_dir

        try:
            # Phase 1: Fetch PRs
            self._set_phase("Fetching PRs", total=4)
            self._log("Starting PR fetch...")
            await generator.fetch_all_prs(
                resume=args.resume,
                progress_callback=self._make_callback(),
            )
            self._log(f"[green]✓[/green] Fetched {len(generator.prs)} PRs total")

            # Phase 2: Jira hierarchy
            self._set_phase("Loading Jira hierarchy")
            await generator.load_jira_hierarchy(
                progress_callback=self._make_callback(),
            )
            self._log(f"[green]✓[/green] Loaded {len(generator.jira_hierarchy)} Jira entries")

            # Phase 3: Generate reports (sync, fast)
            self._set_phase("Generating reports")
            generator.generate_report(os.path.join(output_dir, 'weekly_pr_report_fast.md'))
            generator.save_raw_data(os.path.join(output_dir, 'hypershift_pr_details_fast.json'))
            generator.save_summary_data(os.path.join(output_dir, 'hypershift_pr_summary.json'))
            if args.blog_data:
                generator.generate_blog_data(output_dir)
            self._log("[green]✓[/green] Reports generated")

            # Phase 4: PR Selection
            #
            # Categories:
            #   D (Deep):     fetch diff + full LLM analysis now
            #   Z (Lazy):     fetch diff + metadata-only LLM analysis now;
            #                 diff on disk for deeper pass if blog selects it
            #   M (Metadata): no diff, metadata-only LLM analysis
            #   I (Ignore):   skip entirely
            metadata_keys: set = set()  # keys getting metadata-only analysis (Z + M)
            analyze_keys: set | None = None  # keys to analyze (D + Z + M); None = all files
            diff_prs: list = []  # PR identifiers needing diff fetch (D + Z)
            deep_prs: list = args.deep or []

            if args.select:
                self._set_phase("PR Selection (interactive)")
                all_scored = generator.score_prs_for_deep_analysis(limit=len(generator.prs))
                selections = await self.push_screen_wait(SelectionScreen(all_scored))
                if selections is None:
                    self._log("[yellow]Selection cancelled[/yellow]")
                    self.exit(None)
                    return

                deep_selected = selections['deep']
                lazy_selected = selections['lazy']
                metadata_selected = selections['metadata']
                self._log(
                    f"[green]✓[/green] Selected: {len(deep_selected)} deep, "
                    f"{len(lazy_selected)} lazy, {len(metadata_selected)} metadata, "
                    f"{len(selections['ignore'])} ignored"
                )

                # Deep + Lazy both need diffs fetched
                deep_prs = [f"{pr['repo']}#{pr['number']}" for pr in deep_selected]
                lazy_ids = [f"{pr['repo']}#{pr['number']}" for pr in lazy_selected]
                diff_prs = deep_prs + lazy_ids

                # Track which keys get analyzed and which use metadata-only
                analyze_keys = set()
                for pr in deep_selected:
                    analyze_keys.add(f"{pr['repo'].replace('/', '_')}_{pr['number']}")
                for pr in lazy_selected:
                    key = f"{pr['repo'].replace('/', '_')}_{pr['number']}"
                    analyze_keys.add(key)
                    metadata_keys.add(key)

                # Metadata PRs: save description-only files for analysis
                deep_dir = os.path.join(output_dir, 'pr_deep')
                os.makedirs(deep_dir, exist_ok=True)
                for pr in metadata_selected:
                    key = f"{pr['repo'].replace('/', '_')}_{pr['number']}"
                    meta_path = os.path.join(deep_dir, f"{key}.json")
                    if not os.path.exists(meta_path):
                        meta_data = {
                            'repo': pr['repo'],
                            'number': pr['number'],
                            'title': pr['title'],
                            'url': pr.get('url', ''),
                            'author': pr['author'],
                            'body': pr.get('body', ''),
                            'labels': pr.get('labels', []),
                            'jiraTickets': pr.get('jiraTickets', []),
                            'mergedAt': pr.get('mergedAt', ''),
                            'jiraHierarchy': {},
                        }
                        for ticket in pr.get('jiraTickets', []):
                            if ticket in generator.jira_hierarchy:
                                meta_data['jiraHierarchy'][ticket] = generator.jira_hierarchy[ticket]
                        with open(meta_path, 'w') as f:
                            json.dump(meta_data, f, indent=2)
                    analyze_keys.add(key)
                    metadata_keys.add(key)
            else:
                diff_prs = deep_prs

            # Phase 5: Fetch diffs (Deep + Lazy)
            if diff_prs:
                pr_list = generator.parse_pr_identifiers(diff_prs)
                if pr_list:
                    self._set_phase("Fetching diffs", total=len(pr_list))
                    diffs = await generator.fetch_pr_diffs(
                        pr_list,
                        progress_callback=self._make_callback(),
                    )
                    deep_dir = os.path.join(output_dir, 'pr_deep')
                    generator.write_deep_pr_files(diffs, output_dir=deep_dir)
                    successful = {k: v for k, v in diffs.items() if 'error' not in v}
                    self._log(f"[green]✓[/green] Fetched {len(successful)}/{len(diffs)} diffs")

            # Phase 6: LLM analysis (Deep + Lazy + Metadata; not Ignore)
            if args.analyze:
                if not diff_prs and not args.select:
                    self._log("[red]Error: --analyze requires --deep or --select[/red]")
                    self.exit(None)
                    return
                deep_dir = os.path.join(output_dir, 'pr_deep')
                if not os.path.exists(deep_dir):
                    self._log(f"[red]Error: {deep_dir} not found. Run with --deep first.[/red]")
                    self.exit(None)
                    return
                self._set_phase("LLM Analysis")
                await generator.analyze_prs_with_llm(
                    deep_dir,
                    metadata_keys=metadata_keys,
                    analyze_keys=analyze_keys,
                    progress_callback=self._make_callback(),
                )
                self._log("[green]✓[/green] LLM analysis complete")

            # Done
            self._set_phase("Done")
            pb = self.query_one("#progress", ProgressBar)
            pb.progress = pb.total
            self._log("[green][bold]Pipeline complete![/bold][/green]")
            self.pipeline_result = {'deep_prs': deep_prs, 'metadata_keys': metadata_keys}
            self.exit(self.pipeline_result)

        except Exception as e:
            import traceback as _tb
            self._log(f"[red]Error: {e}[/red]")
            self._log(_tb.format_exc())


def _valid_date(value: str) -> str:
    try:
        datetime.strptime(value, '%Y-%m-%d')
    except ValueError:
        raise argparse.ArgumentTypeError(f"Invalid date: {value}. Use YYYY-MM-DD format.")
    return value


def parse_args():
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(
        description='Generate weekly PR report for HyperShift repositories',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s 2026-02-05
      Standard report since date (until today)

  %(prog)s 2026-02-05 --end 2026-02-12
      Report for specific date range

  %(prog)s 2026-02-05 --end 2026-02-12 --score
      Output scored PR list for deep analysis selection

  %(prog)s 2026-02-05 --score --score-limit 30
      Output top 30 scored PRs

  %(prog)s 2026-02-05 --deep openshift/hypershift#7709 openshift/release#74707
      Fetch diffs for specific PRs (for deep analysis)

  %(prog)s 2026-02-05 --end 2026-02-12 --resume --output-dir DIR
      Resume a previous run, re-fetching only repos that failed

PR format: owner/repo#number (e.g., openshift/hypershift#7657)

Scoring criteria (higher = more important):
  - Jira priority: Critical/Blocker=100, Major=50, Normal=20, Minor=10
  - SDK/API/migration work: +30 points
  - Feature work: +15 points
  - Bug fixes (OCPBUGS): +10 points
  - Manual CI changes (non-bot in release repo): +10 points
        """
    )
    parser.add_argument(
        'since_date',
        nargs='?',
        type=_valid_date,
        default=(datetime.now() - timedelta(days=DEFAULT_DAYS_AGO)).strftime('%Y-%m-%d'),
        help='Start date in YYYY-MM-DD format (default: 7 days ago)'
    )
    parser.add_argument(
        '--end',
        dest='end_date',
        type=_valid_date,
        default=datetime.now().strftime('%Y-%m-%d'),
        help='End date in YYYY-MM-DD format (default: today)'
    )
    parser.add_argument(
        '--deep',
        nargs='+',
        metavar='PR',
        help='Fetch diffs for specified PRs (owner/repo#number format)'
    )
    parser.add_argument(
        '--score',
        action='store_true',
        help='Output scored PR list for deep analysis selection'
    )
    parser.add_argument(
        '--score-limit',
        type=int,
        default=20,
        metavar='N',
        help='Number of PRs to include in scored output (default: 20)'
    )
    parser.add_argument(
        '--output-dir',
        default='/tmp',
        metavar='DIR',
        help='Directory for output files (default: /tmp)'
    )
    parser.add_argument(
        '--resume',
        action='store_true',
        help='Resume a previous run: load existing data and re-fetch only repos that failed'
    )
    parser.add_argument(
        '--blog-data',
        action='store_true',
        help='Generate blog_data.json with contributor table, metrics, and pre-rendered markdown'
    )
    parser.add_argument(
        '--analyze',
        action='store_true',
        help='Run LLM analysis (Sonnet) on PR diffs. Requires ANTHROPIC_API_KEY env var'
    )
    parser.add_argument(
        '--select',
        action='store_true',
        help='Launch interactive TUI to select PRs for deep/light/ignore analysis'
    )
    parser.add_argument(
        '--blog',
        action='store_true',
        help='After data generation, exec into a clean Claude Code session for blog writing'
    )
    return parser.parse_args()


async def main():
    start_time = time.time()

    args = parse_args()
    since_date = args.since_date
    end_date = args.end_date
    output_dir = args.output_dir
    os.makedirs(output_dir, exist_ok=True)

    generator = PRReportGenerator(since_date, end_date, output_dir=output_dir)

    if args.select:
        # TUI mode: full pipeline in a single Textual app window
        app = PipelineApp(generator, args)
        result = await app.run_async()
        if result is None:
            sys.exit(0)
        elapsed = time.time() - start_time
        print(f"\nData generation done in {elapsed:.2f} seconds.")
    else:
        # Headless mode: existing print-based flow (suitable for CI/scripting)
        deep_prs = args.deep or []
        score_mode = args.score
        score_limit = args.score_limit

        print(f"Generating PR report for: {since_date} to {end_date}")
        print(f"Using {'async (aiohttp)' if HAS_AIOHTTP else 'sync (requests)'} mode")
        if args.resume:
            print("Resume mode: will re-fetch only repos that failed previously")
        if deep_prs:
            print(f"Deep mode: will fetch diffs for {len(deep_prs)} PRs")
        if score_mode:
            print(f"Score mode: will output top {score_limit} PRs by importance")
        if args.blog_data:
            print("Blog data mode: will generate blog_data.json")
        if args.analyze:
            print("Analyze mode: will run LLM analysis on PR diffs")
        if args.blog:
            print("Blog mode: will exec into Claude Code for blog writing after data generation")
        print()

        await generator.fetch_all_prs(resume=args.resume)
        await generator.load_jira_hierarchy()

        generator.generate_report(os.path.join(output_dir, 'weekly_pr_report_fast.md'))
        generator.save_raw_data(os.path.join(output_dir, 'hypershift_pr_details_fast.json'))
        generator.save_summary_data(os.path.join(output_dir, 'hypershift_pr_summary.json'))

        if args.blog_data:
            generator.generate_blog_data(output_dir)

        if score_mode:
            scored_prs = generator.score_prs_for_deep_analysis(limit=score_limit)
            generator.print_scored_prs(scored_prs)

            scored_output = [{
                'repo': pr['repo'],
                'number': pr['number'],
                'author': pr['author'],
                'title': pr['title'],
                'score': pr['score'],
                'score_reasons': pr['score_reasons'],
                'pr_id': f"{pr['repo']}#{pr['number']}"
            } for pr in scored_prs]

            scored_path = os.path.join(output_dir, 'pr_scored.json')
            with open(scored_path, 'w') as f:
                json.dump(scored_output, f, indent=2)
            print(f"Scored PRs saved to {scored_path}")

            print("\nPR list for --deep flag:")
            print(' '.join([pr['pr_id'] for pr in scored_output]))

        if deep_prs:
            pr_list = generator.parse_pr_identifiers(deep_prs)
            if pr_list:
                print(f"\nFetching diffs for {len(pr_list)} PRs...")
                diffs = await generator.fetch_pr_diffs(pr_list)
                deep_dir = os.path.join(output_dir, 'pr_deep')
                generator.write_deep_pr_files(diffs, output_dir=deep_dir)

                successful = {k: v for k, v in diffs.items() if 'error' not in v}
                failed = {k: v for k, v in diffs.items() if 'error' in v}
                total_additions = sum(d.get('total_additions', 0) for d in successful.values())
                total_deletions = sum(d.get('total_deletions', 0) for d in successful.values())
                total_files = sum(d.get('total_files', 0) for d in successful.values())
                total_patches = sum(len(d.get('files', [])) for d in successful.values())
                vendor_skipped = total_files - total_patches

                print(f"\n  Deep analysis summary:")
                print(f"    PRs fetched:    {len(successful)}/{len(diffs)}")
                print(f"    Total files:    {total_files} ({vendor_skipped} vendor files skipped)")
                print(f"    Lines changed:  +{total_additions} -{total_deletions}")
                if failed:
                    print(f"    Failed:         {', '.join(v.get('error', '?') for v in failed.values())}")

        if args.analyze:
            if not deep_prs:
                print("Error: --analyze requires --deep to specify PRs")
                sys.exit(1)
            deep_dir = os.path.join(output_dir, 'pr_deep')
            if not os.path.exists(deep_dir):
                print(f"Error: {deep_dir} not found. Run with --deep first.")
                sys.exit(1)
            await generator.analyze_prs_with_llm(deep_dir)

        elapsed = time.time() - start_time
        print(f"\nDone in {elapsed:.2f} seconds!")

    # Blog mode: exec into clean Claude Code session (works after both TUI and headless)
    if args.blog:
        import glob as _glob

        aggregated_path = os.path.join(output_dir, 'pr_deep_aggregated.json')
        blog_data_path = os.path.join(output_dir, 'blog_data.json')

        # Verify required files exist
        missing = []
        if not os.path.exists(aggregated_path):
            missing.append(f"  - {aggregated_path} (run with --analyze)")
        if not os.path.exists(blog_data_path):
            missing.append(f"  - {blog_data_path} (run with --blog-data)")
        if missing:
            print(f"\nError: Missing required files for blog generation:")
            print('\n'.join(missing))
            sys.exit(1)

        # Find the most recent blog post as style reference
        blog_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                'docs', 'content', 'blog')
        existing_blogs = sorted(_glob.glob(os.path.join(blog_dir, '*-progress-report.md')))
        template_path = existing_blogs[-1] if existing_blogs else 'docs/content/blog/2026-06-progress-report.md'

        # Get stats from blog_data.json
        with open(blog_data_path) as f:
            blog_data = json.load(f)
        pr_count = blog_data.get('stats', {}).get('total_prs', '?')
        contributor_count = blog_data.get('stats', {}).get('contributor_count', '?')

        # Build blog filename from dates
        end_dt = datetime.strptime(end_date, '%Y-%m-%d')
        blog_filename = f"{end_dt.strftime('%Y-%m')}-progress-report.md"

        prompt = BLOG_PROMPT_TEMPLATE.format(
            aggregated_path=aggregated_path,
            blog_data_path=blog_data_path,
            template_path=template_path,
            start_date=since_date,
            end_date=end_date,
            pr_count=pr_count,
            contributor_count=contributor_count,
            blog_filename=blog_filename,
        )

        print(f"Launching Claude Code for blog writing...")
        print(f"  Aggregated analysis: {aggregated_path}")
        print(f"  Blog data: {blog_data_path}")
        print(f"  Style reference: {template_path}")

        os.execvp('claude', ['claude', prompt])


def cli():
    asyncio.run(main())


if __name__ == '__main__':
    cli()
