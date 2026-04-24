#!/usr/bin/env python3
"""Gather everything needed to triage a single GitHub PR.

Standalone port of the gather_pr_triage_data tool from BerriAI's PR review
agent. Prints a single JSON object to stdout describing the PR's checks,
diff files, Greptile score, and (where applicable) CircleCI failure logs.

Dependencies: only stdlib + httpx. Install with: pip install httpx

Required env:
    GITHUB_TOKEN   - PAT with public_repo (or repo) scope. Optional for
                     public repos but strongly recommended (60 req/hr without).

Optional env:
    CIRCLECI_TOKEN - CircleCI project or personal token. When set, raw
                     failure log tails are spliced in for failing CircleCI
                     jobs; without it, only GitHub's check-run summary is
                     used.

Usage:
    python gather_pr_triage_data.py https://github.com/owner/repo/pull/123
    python gather_pr_triage_data.py owner/repo#123

The output JSON shape is described in references/verdict-schema.md and
matches what the litellm-pr-reviewer SKILL.md expects.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
from typing import Any

import httpx

GITHUB_API = "https://api.github.com"
CIRCLECI_V11 = "https://circleci.com/api/v1.1"
OTHER_PRS_SAMPLE_SIZE = 3
MAX_PATCH_CHARS = 2000
MAX_LOG_CHARS = 3000

_CIRCLECI_JOB_URL_RE = re.compile(
    r"https?://app\.circleci\.com/pipelines/(?:gh|github)/"
    r"(?P<owner>[^/]+)/(?P<repo>[^/]+)"
    r"/\d+/workflows/[^/]+/jobs/(?P<build_num>\d+)"
)
_CIRCLECI_LEGACY_URL_RE = re.compile(
    r"https?://circleci\.com/(?:gh|github)/"
    r"(?P<owner>[^/]+)/(?P<repo>[^/]+)/(?P<build_num>\d+)"
)
_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_GREPTILE_LOGIN_RE = re.compile(r"greptile", re.IGNORECASE)
_GREPTILE_SCORE_RE = re.compile(
    r"confidence\s*score[^0-9]{0,10}([1-5])\s*/\s*5", re.IGNORECASE
)
_GREPTILE_SCORE_FALLBACK_RE = re.compile(r"\b([1-5])\s*/\s*5\b")
_CIRCLECI_NAME_RE = re.compile(r"(^|/)circleci(\s*[:/]|\b)", re.IGNORECASE)
_VERIA_LOGIN_RE = re.compile(r"^veria-ai(\[bot\])?$", re.IGNORECASE)
_VERIA_FINDING_RE = re.compile(
    r"^\s*\*\*(?P<severity>Critical|High|Medium|Low)\s*:",
    re.IGNORECASE | re.MULTILINE,
)
_VERIA_HIGH_SEVERITIES = {"high", "critical"}

PR_URL_RE = re.compile(
    r"github\.com/(?P<owner>[^/]+)/(?P<repo>[^/]+)/pull/(?P<num>\d+)"
)
PR_SHORT_RE = re.compile(r"^(?P<owner>[^/\s]+)/(?P<repo>[^#\s]+)#(?P<num>\d+)$")


def parse_pr_url(url: str) -> tuple[str, str, int]:
    m = PR_URL_RE.search(url) or PR_SHORT_RE.match(url.strip())
    if not m:
        raise ValueError(f"Not a recognised PR reference: {url}")
    return m["owner"], m["repo"], int(m["num"])


# --------------------------------------------------------------------------- #
# GitHub HTTP helpers                                                          #
# --------------------------------------------------------------------------- #


async def _gh(
    client: httpx.AsyncClient,
    token: str | None,
    path: str,
    **params: Any,
) -> Any:
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    r = await client.get(
        f"{GITHUB_API}{path}",
        params=params or None,
        headers=headers,
        timeout=30.0,
    )
    r.raise_for_status()
    return r.json()


async def _gh_list(
    client: httpx.AsyncClient,
    token: str | None,
    path: str,
    *,
    per_page: int = 100,
    list_key: str | None = None,
    **params: Any,
) -> list[dict]:
    """Page through a GitHub list endpoint until exhausted.

    GitHub list endpoints silently truncate at the per_page boundary. Any
    caller that wants "all of them" must loop -- this wraps that.
    list_key is for envelope responses (e.g. /status -> 'statuses').
    """
    items: list[dict] = []
    page = 1
    while True:
        data = await _gh(
            client, token, path, per_page=per_page, page=page, **params
        )
        batch = data.get(list_key, []) if list_key else data
        if not isinstance(batch, list):
            break
        items.extend(batch)
        if len(batch) < per_page:
            break
        page += 1
    return items


# --------------------------------------------------------------------------- #
# Check enumeration                                                            #
# --------------------------------------------------------------------------- #


async def _list_check_runs(
    client: httpx.AsyncClient,
    token: str | None,
    owner: str,
    repo: str,
    sha: str,
) -> list[dict]:
    """Page /commits/{sha}/check-runs and dedupe to latest-per-name."""
    runs = await _gh_list(
        client,
        token,
        f"/repos/{owner}/{repo}/commits/{sha}/check-runs",
        list_key="check_runs",
    )
    latest: dict[str, dict] = {}
    for r in runs:
        latest[r["name"]] = r
    return list(latest.values())


async def _list_classic_statuses(
    client: httpx.AsyncClient,
    token: str | None,
    owner: str,
    repo: str,
    sha: str,
) -> list[dict]:
    """Return classic commit statuses, shaped to look like check-runs."""
    statuses = await _gh_list(
        client,
        token,
        f"/repos/{owner}/{repo}/commits/{sha}/status",
        list_key="statuses",
    )
    out: list[dict] = []
    for s in statuses:
        state = s.get("state")  # success | failure | error | pending
        conclusion = {
            "success": "success",
            "failure": "failure",
            "error": "failure",
            "pending": None,
        }.get(state)
        out.append(
            {
                "id": None,
                "name": s["context"],
                "conclusion": conclusion,
                "status": "completed" if conclusion else "in_progress",
                "html_url": s.get("target_url"),
                "output": {"summary": s.get("description"), "text": None},
            }
        )
    return out


async def _all_checks(
    client: httpx.AsyncClient,
    token: str | None,
    owner: str,
    repo: str,
    sha: str,
) -> list[dict]:
    """Combined view: check-runs win on collisions with classic statuses."""
    runs, statuses = await asyncio.gather(
        _list_check_runs(client, token, owner, repo, sha),
        _list_classic_statuses(client, token, owner, repo, sha),
    )
    by_name: dict[str, dict] = {s["name"]: s for s in statuses}
    for r in runs:
        by_name[r["name"]] = r
    return list(by_name.values())


def _has_circleci_checks(checks: list[dict]) -> bool:
    """True iff any check-run/status is from CircleCI."""
    for c in checks or []:
        name = c.get("name") or ""
        if _CIRCLECI_NAME_RE.search(name):
            return True
        app = c.get("app") or {}
        slug = (app.get("slug") or "").lower()
        if "circleci" in slug:
            return True
        html_url = c.get("html_url") or ""
        if "circleci.com" in html_url:
            return True
    return False


# --------------------------------------------------------------------------- #
# Per-failure enrichment                                                       #
# --------------------------------------------------------------------------- #


async def _fetch_annotations(
    client: httpx.AsyncClient,
    token: str | None,
    owner: str,
    repo: str,
    run_id: int | None,
) -> list[str]:
    if run_id is None:
        return []
    try:
        ann = await _gh(
            client,
            token,
            f"/repos/{owner}/{repo}/check-runs/{run_id}/annotations",
            per_page=20,
        )
    except httpx.HTTPStatusError:
        return []
    out: list[str] = []
    for a in ann or []:
        msg = (a.get("message") or "").strip()
        path = a.get("path") or ""
        line = a.get("start_line")
        out.append(f"{path}:{line}: {msg}"[:300])
    return out


async def _fetch_v11_failure_log(
    client: httpx.AsyncClient,
    circleci_token: str,
    owner: str,
    repo: str,
    build_num: int,
) -> str | None:
    """Fetch the failing step's log tail for a single CircleCI v1.1 build."""
    headers = {"Circle-Token": circleci_token, "Accept": "application/json"}
    try:
        r = await client.get(
            f"{CIRCLECI_V11}/project/github/{owner}/{repo}/{build_num}",
            headers=headers,
            timeout=30.0,
        )
        r.raise_for_status()
        build = r.json()
    except (httpx.HTTPError, ValueError):
        return None

    for step in build.get("steps") or []:
        for action in step.get("actions") or []:
            if action.get("status") not in (
                "failed",
                "timedout",
                "infrastructure_fail",
            ):
                continue
            output_url = action.get("output_url")
            if not output_url:
                continue
            try:
                lr = await client.get(output_url, timeout=30.0)
                lr.raise_for_status()
                parts = lr.json()
            except (httpx.HTTPError, ValueError):
                continue
            text = "\n".join(
                (p.get("message") or "") for p in parts if isinstance(p, dict)
            )
            text = _ANSI_ESCAPE_RE.sub("", text)
            if not text.strip():
                continue
            if len(text) > MAX_LOG_CHARS:
                text = "...[truncated]\n" + text[-MAX_LOG_CHARS:]
            return text
    return None


async def _fetch_circleci_failure_log(
    client: httpx.AsyncClient,
    circleci_token: str,
    html_url: str | None,
) -> str | None:
    if not html_url:
        return None
    m = _CIRCLECI_JOB_URL_RE.search(html_url) or _CIRCLECI_LEGACY_URL_RE.search(
        html_url
    )
    if not m:
        return None
    return await _fetch_v11_failure_log(
        client,
        circleci_token,
        m["owner"],
        m["repo"],
        int(m["build_num"]),
    )


# --------------------------------------------------------------------------- #
# PR-level fetches                                                             #
# --------------------------------------------------------------------------- #


async def _fetch_diff(
    client: httpx.AsyncClient,
    token: str | None,
    owner: str,
    repo: str,
    pr_number: int,
) -> list[dict]:
    files = await _gh_list(
        client, token, f"/repos/{owner}/{repo}/pulls/{pr_number}/files"
    )
    out: list[dict] = []
    for f in files:
        patch = f.get("patch")
        if patch and len(patch) > MAX_PATCH_CHARS:
            patch = patch[:MAX_PATCH_CHARS] + "\n...[truncated]"
        out.append(
            {
                "filename": f["filename"],
                "status": f.get("status", "modified"),
                "additions": f.get("additions", 0),
                "deletions": f.get("deletions", 0),
                "patch": patch,
            }
        )
    return out


async def _fetch_other_open_prs(
    client: httpx.AsyncClient,
    token: str | None,
    owner: str,
    repo: str,
    exclude_pr: int,
    n: int,
) -> list[dict]:
    pulls = await _gh(
        client,
        token,
        f"/repos/{owner}/{repo}/pulls",
        state="open",
        sort="updated",
        direction="desc",
        per_page=n + 5,
    )
    return [p for p in pulls if p["number"] != exclude_pr][:n]


async def _fetch_greptile_score(
    client: httpx.AsyncClient,
    token: str | None,
    owner: str,
    repo: str,
    pr_number: int,
) -> int | None:
    """Latest Greptile confidence score (1-5), or None. Best-effort."""
    try:
        reviews, comments = await asyncio.gather(
            _gh_list(
                client, token, f"/repos/{owner}/{repo}/pulls/{pr_number}/reviews"
            ),
            _gh_list(
                client,
                token,
                f"/repos/{owner}/{repo}/issues/{pr_number}/comments",
            ),
        )
    except httpx.HTTPStatusError:
        return None

    candidates: list[tuple[str, str]] = []
    for r in reviews or []:
        login = (r.get("user") or {}).get("login") or ""
        if _GREPTILE_LOGIN_RE.search(login):
            candidates.append((r.get("submitted_at") or "", r.get("body") or ""))
    for c in comments or []:
        login = (c.get("user") or {}).get("login") or ""
        if _GREPTILE_LOGIN_RE.search(login):
            candidates.append((c.get("created_at") or "", c.get("body") or ""))

    candidates.sort(reverse=True)
    for _, body in candidates:
        m = _GREPTILE_SCORE_RE.search(body) or _GREPTILE_SCORE_FALLBACK_RE.search(
            body
        )
        if m:
            return int(m.group(1))
    return None


async def _fetch_veria_findings(
    client: httpx.AsyncClient,
    token: str | None,
    owner: str,
    repo: str,
    pr_number: int,
) -> dict | None:
    """Veria AI security findings on a PR.

    Returns:
        {
            "ran": bool,                 # has Veria commented on this PR at all
            "high_count": int,           # # of High|Critical inline findings
            "all_findings": [            # one per inline review comment
                {"severity": str, "html_url": str},
                ...
            ],
        }
        or None on fetch error.

    Veria posts in two shapes:
      1. Issue comments — overall summary per run; headline severity is the
         RUN's risk, not a per-finding signal. Used here only to detect
         whether Veria has run at all.
      2. PR review comments — one per finding, body starts with
         "**<Severity>: <Title>**". Source of truth for high_count.

    TODO: filter out findings whose review thread is resolved. The REST
    /pulls/{n}/comments endpoint does not expose thread.is_resolved; needs
    the GraphQL pullRequest.reviewThreads(first:N){nodes{isResolved}} query.
    """
    try:
        issue_comments, review_comments = await asyncio.gather(
            _gh_list(
                client,
                token,
                f"/repos/{owner}/{repo}/issues/{pr_number}/comments",
            ),
            _gh_list(
                client,
                token,
                f"/repos/{owner}/{repo}/pulls/{pr_number}/comments",
            ),
        )
    except httpx.HTTPStatusError:
        return None

    def _is_veria(item: dict) -> bool:
        login = (item.get("user") or {}).get("login") or ""
        return bool(_VERIA_LOGIN_RE.match(login))

    veria_issue_comments = [c for c in issue_comments or [] if _is_veria(c)]
    veria_review_comments = [c for c in review_comments or [] if _is_veria(c)]

    ran = bool(veria_issue_comments or veria_review_comments)

    findings: list[dict] = []
    for c in veria_review_comments:
        body = c.get("body") or ""
        m = _VERIA_FINDING_RE.search(body)
        if not m:
            continue
        findings.append(
            {
                "severity": m.group("severity").capitalize(),
                "html_url": c.get("html_url") or "",
            }
        )

    high_count = sum(
        1 for f in findings if f["severity"].lower() in _VERIA_HIGH_SEVERITIES
    )

    return {
        "ran": ran,
        "high_count": high_count,
        "all_findings": findings,
    }


async def _noop_none() -> None:
    return None


# --------------------------------------------------------------------------- #
# Orchestration                                                                #
# --------------------------------------------------------------------------- #


async def gather(
    owner: str,
    repo: str,
    pr_number: int,
    *,
    github_token: str | None,
    circleci_token: str | None,
) -> dict:
    async with httpx.AsyncClient() as client:
        pr, diff_files, other_prs, greptile_score, veria = await asyncio.gather(
            _gh(client, github_token, f"/repos/{owner}/{repo}/pulls/{pr_number}"),
            _fetch_diff(client, github_token, owner, repo, pr_number),
            _fetch_other_open_prs(
                client,
                github_token,
                owner,
                repo,
                pr_number,
                OTHER_PRS_SAMPLE_SIZE,
            ),
            _fetch_greptile_score(client, github_token, owner, repo, pr_number),
            _fetch_veria_findings(client, github_token, owner, repo, pr_number),
        )
        head_sha = pr["head"]["sha"]

        own_checks_task = _all_checks(client, github_token, owner, repo, head_sha)
        other_checks_tasks = [
            _all_checks(client, github_token, owner, repo, p["head"]["sha"])
            for p in other_prs
        ]
        own_checks, *other_checks = await asyncio.gather(
            own_checks_task, *other_checks_tasks
        )

        passing: list[str] = []
        in_progress: list[str] = []
        failing_runs: list[dict] = []
        for r in own_checks:
            concl = r.get("conclusion")
            if concl in ("success", "neutral", "skipped"):
                passing.append(r["name"])
            elif concl in ("failure", "timed_out", "cancelled"):
                failing_runs.append(r)
            else:
                in_progress.append(r["name"])

        if failing_runs:
            annotations_per, circleci_logs_per = await asyncio.gather(
                asyncio.gather(
                    *[
                        _fetch_annotations(
                            client, github_token, owner, repo, r.get("id")
                        )
                        for r in failing_runs
                    ]
                ),
                asyncio.gather(
                    *[
                        (
                            _fetch_circleci_failure_log(
                                client, circleci_token, r.get("html_url")
                            )
                            if circleci_token
                            else _noop_none()
                        )
                        for r in failing_runs
                    ]
                ),
            )
        else:
            annotations_per = []
            circleci_logs_per = []

        failure_contexts: list[dict] = []
        for r, ann_list, cci_log in zip(
            failing_runs, annotations_per, circleci_logs_per
        ):
            name = r["name"]
            output = r.get("output") or {}
            text = output.get("text") or ""
            if len(text) > MAX_LOG_CHARS:
                text = text[:MAX_LOG_CHARS] + "\n...[truncated]"
            if cci_log:
                text = (
                    f"{text}\n\n--- CircleCI raw log tail ---\n{cci_log}"
                    if text
                    else f"--- CircleCI raw log tail ---\n{cci_log}"
                )
            other_status: list[dict] = []
            for p, p_checks in zip(other_prs, other_checks):
                match = next((c for c in p_checks if c["name"] == name), None)
                other_status.append(
                    {
                        "pr_number": p["number"],
                        "pr_title": p.get("title", ""),
                        "found": match is not None,
                        "conclusion": (match or {}).get("conclusion"),
                    }
                )
            failure_contexts.append(
                {
                    "check_name": name,
                    "conclusion": r.get("conclusion"),
                    "summary": output.get("summary"),
                    "failure_excerpt": text or None,
                    "annotations": ann_list,
                    "html_url": r.get("html_url"),
                    "other_prs": other_status,
                }
            )

        return {
            "owner": owner,
            "repo": repo,
            "pr_number": pr_number,
            "pr_title": pr.get("title", ""),
            "pr_author": (pr.get("user") or {}).get("login") or "",
            "head_sha": head_sha,
            "passing_checks": passing,
            "in_progress_checks": in_progress,
            "failing_check_contexts": failure_contexts,
            "diff_files": diff_files,
            "other_pr_numbers": [p["number"] for p in other_prs],
            "greptile_score": greptile_score,
            "has_circleci_checks": _has_circleci_checks(own_checks),
            "veria": veria,
        }


def main() -> None:
    ap = argparse.ArgumentParser(
        description=(
            "Print a JSON triage report for a single GitHub PR. Consumed by "
            "the litellm-pr-reviewer SKILL.md."
        )
    )
    ap.add_argument(
        "pr",
        help=(
            "PR reference. Either a full URL "
            "(https://github.com/owner/repo/pull/N) or owner/repo#N."
        ),
    )
    args = ap.parse_args()

    try:
        owner, repo, pr_number = parse_pr_url(args.pr)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(2)

    gh_token = os.environ.get("GITHUB_TOKEN") or None
    cci_token = os.environ.get("CIRCLECI_TOKEN") or None
    if not gh_token:
        print(
            "warning: GITHUB_TOKEN not set; using unauthenticated GitHub API "
            "(60 req/hr limit; expect 403 on busy repos).",
            file=sys.stderr,
        )

    try:
        report = asyncio.run(
            gather(
                owner,
                repo,
                pr_number,
                github_token=gh_token,
                circleci_token=cci_token,
            )
        )
    except httpx.HTTPStatusError as exc:
        print(
            f"error: GitHub returned {exc.response.status_code} for "
            f"{exc.request.url}",
            file=sys.stderr,
        )
        sys.exit(1)

    json.dump(report, sys.stdout, indent=2)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
