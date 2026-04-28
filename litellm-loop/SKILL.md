---
name: litellm-loop
description: Iteratively fix a BerriAI/litellm PR until the litellm-bot reviewer returns a 5/5 READY verdict. Posts the PR to the bot (via Slack @-mention or its /chat/api endpoint), parses the merge-confidence card, fixes every blocker the card and drilldown call out, pushes, and re-asks. Use when a contributor wants their PR fully greenlit by the bot before requesting a human review. Triggers on "loop my PR through litellm-bot", "/litellm-loop", "make this PR pass review", or "drive PR <url> to READY".
allowed-tools: Bash, Read, Grep, Glob, Edit, Write
---

You drive a single `BerriAI/litellm` pull request through repeated rounds of [`litellm-bot`](https://github.com/BerriAI/internal-pr-review-agent) review until the bot returns **5/5 READY** with no remaining blockers — or you hit the iteration cap.

This is the contributor-side analog of `/greploop`: same shape (review → fix → push → re-review), but the reviewer is `litellm-bot`, the score is **N/5 Merge Confidence**, and the verdict label is **READY / BLOCKED / WAITING**.

## Inputs

- **PR URL or ref** (optional): full `https://github.com/BerriAI/litellm/pull/<N>` URL or short `BerriAI/litellm#<N>`. If omitted, detect from the current branch via `gh pr view`.
- **Bot transport** (optional, auto-detected): one of
  - `slack` — `@litellm-bot <PR_URL>` in a channel the bot is in. Default when `SLACK_WEBHOOK_URL` is set.
  - `http` — `POST {LITELLM_BOT_URL}/chat/api`. Default when `LITELLM_BOT_URL` is set.

## Required environment

| Var | Required | Purpose |
|---|---|---|
| `GITHUB_TOKEN` | yes | PAT with `public_repo` (or `repo` for private). Used to push and to read PR/check state directly when the bot doesn't surface a detail. |
| `LITELLM_BOT_URL` | one of | Base URL of a `litellm-bot` instance exposing `/chat/api` (e.g. `https://litellm-bot.fly.dev`). Required for `http` transport. |
| `LITELLM_BOT_API_KEY` | with `LITELLM_BOT_URL` | Bearer token that the bot accepts on `/chat/api`. The bot operator mints these and configures them via `BOT_API_KEYS` (CSV) on the bot side. Skip if the bot has no auth configured. |
| `SLACK_WEBHOOK_URL` | one of | Incoming webhook scoped to a channel the bot is in. Required for `slack` transport. |
| `SLACK_BOT_USER_TOKEN` | with `SLACK_WEBHOOK_URL` | `xoxp-…` user token with `channels:history` so this skill can read the bot's reply back. |

If neither `LITELLM_BOT_URL` nor `SLACK_WEBHOOK_URL` is set, tell the user and stop. Do not try to invoke the bot's underlying skill yourself — the rubric, scoring, and karpathy-check stages are server-side and not reproducible from the SKILL alone.

## Hard rules (apply throughout)

- **Do not edit the rubric.** The score and verdict come verbatim from the bot's reply. Never fabricate a 5/5 to short-circuit the loop.
- **Only fix what the card or drilldown names.** Do not chase Greptile-style nits the bot didn't surface — the bot has its own rubric and ignores noise on purpose. If the card says READY but a CI check is red, the failure name will appear in `failing_line` or `unrelated_failures`; treat anything not named there as out of scope for this loop.
- **Never `git push --force` to `main`.** Force-push is fine on the contributor's PR branch when fixups need squashing, but warn before doing it on any branch the user didn't explicitly call out.
- **Stop at the first WAITING with non-stale checks.** WAITING means CI is still running; pushing again restarts the wait. Wait for the run to complete before re-asking unless the user asks you to spin.

## Step 0: detect platform and identity

```bash
# PR identity — explicit arg wins, otherwise current branch.
PR_REF="${ARGUMENTS:-$(gh pr view --json url -q .url 2>/dev/null)}"
if [ -z "$PR_REF" ]; then
  echo "no PR ref given and `gh pr view` found none for this branch" >&2
  exit 1
fi

# Normalize to owner/repo and number.
PR_URL=$(echo "$PR_REF" | grep -oE 'https://github.com/[^/]+/[^/]+/pull/[0-9]+' || true)
if [ -z "$PR_URL" ]; then
  # Short ref form: BerriAI/litellm#123
  OWNER_REPO=$(echo "$PR_REF" | cut -d'#' -f1)
  PR_NUM=$(echo "$PR_REF" | cut -d'#' -f2)
  PR_URL="https://github.com/${OWNER_REPO}/pull/${PR_NUM}"
fi

# Pick transport. http wins when both are set (deterministic, parseable).
if [ -n "$LITELLM_BOT_URL" ]; then
  TRANSPORT="http"
elif [ -n "$SLACK_WEBHOOK_URL" ]; then
  TRANSPORT="slack"
else
  echo "set LITELLM_BOT_URL or SLACK_WEBHOOK_URL" >&2
  exit 1
fi
```

Switch to the PR branch with `gh pr checkout <N>` if you're not already on it. The loop edits files locally, so being on the right branch is non-negotiable.

## Step 1: the loop

Repeat the cycle below. **Max 5 iterations.** Each iteration ends with either an exit condition met or a fresh push that triggers the next iteration.

### A. Ask the bot for a review

#### A.1 — `http` transport (preferred)

```bash
THREAD_ID=$(uuidgen | tr 'A-Z' 'a-z' | tr -d '-')
RESP=$(curl -s "${LITELLM_BOT_URL}/chat/api" \
  -H 'content-type: application/json' \
  ${LITELLM_BOT_API_KEY:+-H "Authorization: Bearer ${LITELLM_BOT_API_KEY}"} \
  -d "$(jq -nc --arg msg "Triage this PR: $PR_URL" --arg tid "$THREAD_ID" \
        '{message: $msg, thread_id: $tid}')")
CARD=$(echo "$RESP" | jq -r .output)
```

The card body is a single Slack-mrkdwn string. Save it for parsing in step B.

If the bot also exposes `/chat/api/threads/<id>` (it does in the dev UI), fetch the **drilldown** by re-posting `Show drilldown` on the same `thread_id` — that's where per-failure rationale, pattern findings, and prior-signal reconciliation live. The card alone may say `BLOCKED` without telling you which file to touch.

```bash
DRILLDOWN_RESP=$(curl -s "${LITELLM_BOT_URL}/chat/api" \
  -H 'content-type: application/json' \
  ${LITELLM_BOT_API_KEY:+-H "Authorization: Bearer ${LITELLM_BOT_API_KEY}"} \
  -d "$(jq -nc --arg msg "Show drilldown" --arg tid "$THREAD_ID" \
        '{message: $msg, thread_id: $tid}')")
DRILLDOWN=$(echo "$DRILLDOWN_RESP" | jq -r .output)
```

If the bot returns `401`, your `LITELLM_BOT_API_KEY` is unset or wrong — fix it and retry. Don't fall back to unauthenticated requests; that path is only valid against a wide-open bot (`BOT_API_KEYS` unset on the bot side, intended for local dev only).

#### A.2 — `slack` transport (fallback)

```bash
# Post the mention via webhook; the bot replies in-thread.
TS=$(date +%s)
curl -s -X POST "$SLACK_WEBHOOK_URL" \
  -H 'content-type: application/json' \
  -d "$(jq -nc --arg text "<@litellm-bot> Triage this PR: $PR_URL (loop:$TS)" \
        '{text: $text}')"
```

Then poll the channel via Slack Web API until a message from the bot's user ID lands containing the substring `*Merge Confidence:`:

```bash
# CHANNEL_ID and BOT_USER_ID resolved once via conversations.list / users.list.
for i in {1..60}; do
  CARD=$(curl -s "https://slack.com/api/conversations.history?channel=${CHANNEL_ID}&oldest=${TS}&limit=20" \
    -H "Authorization: Bearer $SLACK_BOT_USER_TOKEN" \
    | jq -r ".messages[] | select(.user==\"${BOT_USER_ID}\") | select(.text | contains(\"Merge Confidence:\")) | .text" \
    | head -1)
  [ -n "$CARD" ] && break
  sleep 10
done
```

Drilldown lives in the same thread under `replies` — fetch via `conversations.replies?ts=<parent_ts>` and pick the message whose text starts with `*Drill-down*`.

### B. Parse the verdict

The card is a known shape (locked in `app.render_card`):

```
*Triage Summary*
<one-paragraph summary>

_<size line>_  ← optional

*Merge Confidence: N/5*  <emoji> <VERDICT>
⚠️ N check(s) failing: foo, bar  ← optional, only when failing_line is non-empty
<one-line verdict explanation>

<longer justification prose>
```

Extract:

```bash
SCORE=$(echo "$CARD" | grep -oE '\*Merge Confidence: [0-5]/5\*' | grep -oE '[0-5]' | head -1)
VERDICT=$(echo "$CARD" | grep -oE '(READY|BLOCKED|WAITING)' | head -1)
FAILING_LINE=$(echo "$CARD" | grep -E '^⚠️' | head -1)  # may be empty
```

From the drilldown, extract structured findings. Each section header is italicized (`_Header_`) and bullets start with `  • `. The relevant sections for the loop are:

- `_Merge state_` — only present when conflicts. Always blocking.
- `_PR-related failures_` — every bullet is a CI check the bot blames on this diff. **Always blocking.**
- `_Pattern findings_` — bullets formatted as `[severity risk=X] ` `path` ` — rationale (source: …, citation: …)`. Severity `blocker` or `risk=high` is blocking; `risk=medium` is a partial dock; `nit` is informational.
- `_Scope drift vs linked issue_` — one bullet citing the issue field that disagrees with the diff. Blocking.
- `_Prior signals (reviewer + Greptile reconciliation)_` — bullets prefixed with `⚠️` are unresolved at severity `[blocker]` / `[concern]`; those are blocking. Bullets prefixed with `✓` were already reconciled — ignore.
- `_Karpathy senior-eng review_` — only present when fused verdict was otherwise READY. `safe_for_high_rps_gateway: no` is blocking; `conditional` is a partial dock.

Sections you should **not** act on:

- `_Unrelated failures_` — flagged as infra/cross-PR by the bot. Pushing won't fix them.
- `_Policy / meta failures_` — DCO, source-branch, CLA. Tell the user verbatim what the rationale says (it's already specific) and stop unless the user asked you to handle policy too. These never dock score so the loop can finish READY with one of these still red.
- `_Tech debt (FYI, not blocking)_` — explicitly non-blocking by name.
- `_Still running_` — see "Pending state taxonomy" below; wait, don't act.

### B.5. Pending state taxonomy (CI vs Greptile)

Two distinct things can be "pending", and the loop must treat them differently — pushing a new commit makes the wait *longer*, not shorter:

| Pending signal | Where it shows up | What `score` looks like | What `verdict` looks like | Loop action |
|---|---|---|---|---|
| **CI checks still running** | `_Still running_` section in drilldown is non-empty; verdict one-liner says `N check(s) still running: foo, bar` | provisional, may be 5/5 anyway | always `WAITING` (overrides score) | **wait, don't push.** Pushing resets `head_sha` and restarts every check. |
| **Greptile hasn't reviewed yet** | Justification prose contains `Greptile pending` or `Greptile has not reviewed this PR yet`; `greptile_score` is null in the underlying data | docked 1 (capped at 4/5) | usually `BLOCKED` (because score < 5) | **wait, don't push** unless other blockers also exist. Greptile lands on its own schedule once the PR is non-draft and CI is green; pushing doesn't speed it up. You *can* nudge it by leaving an `@greptile review` comment on the PR if it has been > 10 min and the score still hasn't landed. |
| **Both** | Both signals present | provisional + docked 1 | `WAITING` | wait. CI must finish before Greptile starts on most setups. |

How to detect each from the parsed card + drilldown:

```bash
# CI still running:
CI_RUNNING=$(echo "$DRILLDOWN" | awk '/^_Still running_$/,/^_/' | grep -c '^  • ' || echo 0)
# Greptile pending:
GREPTILE_PENDING=$(echo "$CARD" | grep -ciE 'greptile (pending|has not reviewed)' || echo 0)
```

Polling cadence when waiting: re-ask the bot every 60s for CI (checks usually finish in 5–15 min on this repo), every 120s for Greptile (typical landing is 2–10 min after CI goes green). Cap the wait at **15 min total per iteration**; if pending persists past that, stop and tell the user — something is stuck server-side and the loop can't unstick it.

### C. Check exit conditions

Stop the loop and report when **any** are true:

- `SCORE == 5` AND `VERDICT == READY` AND there are zero entries in `_PR-related failures_` / `_Pattern findings_ [blocker|risk=high]` / `_Scope drift_` / `_Prior signals_ ⚠️` / `_Karpathy_ no`.
- `VERDICT == WAITING` AND no other fixable items in the drilldown — enter the wait loop above. Resume normal iteration when CI finishes; stop with a "still waiting after 15 min" report if it doesn't.
- `VERDICT == BLOCKED` AND the **only** remaining penalty is `Greptile pending` — enter the wait loop. Don't push, don't re-trigger; just re-ask. If Greptile hasn't landed after 15 min, optionally post `@greptile review` on the PR (one shot only — never spam) and wait one more cycle, then stop.
- Iteration count == 5 (push iterations only — wait cycles don't count toward this).
- Two consecutive **post-fix** drilldown bullets are identical — you're not making progress, surface the remaining issues and stop.

### D. Fix the named blockers

Walk the blocking findings in this order (matches the bot's own rubric weights, so the score moves the most per fix):

1. **Merge conflicts** (weight 5 — single-handedly forces score to 0).
   ```bash
   git fetch origin main
   git rebase origin/main  # or merge if the repo prefers
   # resolve with the editor; do NOT auto-resolve in favor of HEAD or theirs.
   ```

2. **PR-related CI failures** (weight 2 each). The drilldown rationale tells you what failed. For CircleCI failures, the bot already pulled the failure log tail — read it inline in the drilldown and reproduce the failure locally before pushing.

3. **Pattern findings with `risk=high` or `severity=blocker`** (weight 2). The bullet cites a `path` and a `(source: docs|code, citation: …)`. Read both:
   - `source: docs` → open the doc at the citation, then change the diff file to match.
   - `source: code` → open the sibling file at the citation, then change the diff file to match its pattern.

4. **Scope drift** (weight 2). The bullet quotes the issue field (title or body). Either narrow the diff to the issue's scope (preferred — split out the extra concerns to a follow-up PR), or update the issue description to cover the broader scope. Don't claim the issue covers something it doesn't.

5. **Pattern findings with `risk=medium`** (weight 1). Same fix shape as `risk=high`, lower priority.

6. **Unresolved prior signals at `[blocker]` / `[concern]`** (weight 2 / 1). The bullet quotes the original reviewer or Greptile excerpt. Address the underlying issue OR explicitly resolve the GitHub thread with a justification — the bot will reclassify a thread with a concrete dismissal reason as `disagreed` instead of `agreed`, which drops the dock.

7. **Karpathy `safe_for_high_rps_gateway: no` / `conditional`** — the `merge_gate.what_would_make_yes` field tells you the smallest change to flip the verdict. Apply it.

For each fix, run the relevant local check before committing (`uv run pytest path/to/test.py`, `uv run ruff check`, etc.) so you don't push a broken commit and waste a CI cycle.

### E. Commit and push

```bash
git add -A
git commit -m "address litellm-bot review feedback (litellm-loop iteration N)"
git push
```

Wait briefly for CI to start, then go back to step A:

```bash
sleep 10  # give GitHub time to register the push and start checks
```

The bot reads `head_sha`, so a fresh push automatically resets the score window — the next review will be against the new commit.

## Step 2: report

After exiting the loop, summarize:

| Field             | Value                                  |
|-------------------|----------------------------------------|
| PR                | `<PR_URL>`                             |
| Iterations        | N                                      |
| Final score       | X/5                                    |
| Final verdict     | READY / BLOCKED / WAITING              |
| Fixed             | bulleted list of what you changed      |
| Remaining         | bulleted list of what's still flagged  |
| Notes             | any policy/meta or unrelated failures  |

If you stopped early (max iterations, no progress, or stuck WAITING), say so explicitly and link the last bot reply so the user can read the card themselves.

## Output format

Steady state:

```
litellm-loop complete.
  PR:          https://github.com/BerriAI/litellm/pull/26500
  Iterations:  2
  Score:       5/5
  Verdict:     ✅ READY
  Fixed:
    - rebased onto origin/main (resolved conflicts in litellm/proxy/auth.py)
    - added missing `await` in litellm/llms/anthropic/chat/transformation.py
                       (PR-related: pytest tests/llms/test_anthropic.py)
  Remaining:   none
  Notes:       1 unrelated CircleCI failure (`build_and_test 3.13`) — also
               red on PRs #26385 and #26011, infra-wide noise.
```

Stuck:

```
litellm-loop stopped after 5 iterations.
  PR:          https://github.com/BerriAI/litellm/pull/26451
  Score:       3/5
  Verdict:     ❌ BLOCKED
  Fixed:       3 items (see commits a1b2c3d…f4e5d6c)
  Remaining:
    - merge conflicts: branch keeps re-conflicting on litellm/router.py
      (someone else is actively pushing to main; pause and rebase manually)
    - pattern finding [blocker risk=high] `litellm/proxy/auth/user_api_key_auth.py`
      — guard added at sink, not source (source: code,
        citation: litellm/proxy/auth/handle_jwt.py:142)
  Last bot reply: <link to thread>
```

Stuck waiting:

```
litellm-loop paused — pending signals haven't cleared after 15 min.
  PR:          https://github.com/BerriAI/litellm/pull/26500
  Iterations:  1 push, 8 wait cycles
  Score:       4/5 (provisional — Greptile pending docks 1)
  Verdict:     ❌ BLOCKED
  Fixed:       1 item (see commit a1b2c3d)
  Remaining:
    - Greptile has not reviewed this PR yet
      (posted `@greptile review` once at +15min, no response by +30min)
  Notes:       1 unrelated CircleCI failure (`build_and_test 3.13`) — also
               red on PRs #26385 and #26011, infra-wide noise.
  Next step:   wait for Greptile to land on its own schedule, or ping
               @greptile-bot in the PR. Re-run /litellm-loop once it scores.
```
