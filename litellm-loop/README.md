# litellm-loop

> Drive a `BerriAI/litellm` PR through repeated rounds of `litellm-bot` review until it returns a clean 5/5 READY verdict.

The contributor-side analog of [`/greploop`](https://github.com/greptileai/greptile-skills/tree/main/greptile/greploop): point your local agent at this skill, give it a PR URL, and it will ask the bot for a review, fix every blocker the card and drilldown name, push, and re-ask — up to a configurable iteration cap.

## What it does

Given a PR URL (or auto-detected from the current branch), the skill:

1. Posts the PR to a running [`litellm-bot`](https://github.com/BerriAI/internal-pr-review-agent) instance — either via Slack `@-mention` (webhook) or its `/chat/api` HTTP endpoint.
2. Parses the merge-confidence card (`*Merge Confidence: N/5*  emoji VERDICT`) and the threaded drilldown for blocking findings: merge conflicts, PR-related CI failures, high/medium-risk pattern findings, scope drift vs the linked issue, unresolved prior signals, and karpathy-stage `safe_for_high_rps_gateway` verdicts.
3. Walks blockers in **rubric-weight order** so each fix moves the score the most: conflicts (weight 5) → PR-related failures (2) → high-risk pattern (2) → scope drift (2) → medium-risk pattern (1) → unresolved priors → karpathy.
4. Commits, pushes, waits for CI to start, and re-asks the bot.
5. Stops when the bot returns 5/5 READY with no blockers, or when the iteration cap is hit, or when two iterations in a row produce identical drilldowns (no progress).

## Install

### As a Claude Code plugin

Once published to a plugin marketplace (e.g. via the [LiteLLM Skills Gateway](https://docs.litellm.ai/docs/skills_gateway)):

```text
/plugin marketplace add <your-marketplace>
/plugin install litellm-loop
```

### Manually as a project skill

```bash
mkdir -p .claude/skills
git clone --depth 1 https://github.com/BerriAI/pr-review-agent-skills /tmp/pras
cp -R /tmp/pras/litellm-loop .claude/skills/
```

No Python dependencies — the skill is pure shell + your agent's editor tools.

## Use

```text
/litellm-loop https://github.com/BerriAI/litellm/pull/26500
```

Or just describe what you want — Claude/Cursor will load the skill automatically when you ask it to "drive my PR through litellm-bot until it passes" or "loop PR `<url>` to READY".

If you omit the PR ref, the skill detects it from `gh pr view` on the current branch.

## Required environment

| Variable | Required | Purpose |
|---|---|---|
| `GITHUB_TOKEN` | yes | PAT with `public_repo` scope (`repo` for private). Used for `git push` and direct GitHub reads when the bot doesn't surface a detail. |
| `LITELLM_BOT_URL` | one transport | Base URL of a `litellm-bot` instance exposing `/chat/api` (e.g. `https://litellm-bot.fly.dev`). Required for `http` transport. |
| `LITELLM_BOT_AUTH_COOKIE` | with `LITELLM_BOT_URL` | Session cookie value if the bot has `AUTH_ENABLED=True`. Skip otherwise. |
| `SLACK_WEBHOOK_URL` | one transport | Incoming webhook scoped to a channel `litellm-bot` is in. Required for `slack` transport. |
| `SLACK_BOT_USER_TOKEN` | with `SLACK_WEBHOOK_URL` | `xoxp-…` user token with `channels:history` so the skill can read the bot's reply back. |

You need **at least one** of `LITELLM_BOT_URL` or `SLACK_WEBHOOK_URL`. If both are set, the skill prefers `http` (deterministic, parseable, no Slack rate-limit concerns).

## How the loop decides "done"

The bot's verdict is the **only source of truth**. The skill never fabricates a 5/5 to short-circuit. Specifically, it stops when:

- `score == 5` AND `verdict == READY` AND zero blocking entries in the drilldown, **or**
- iteration cap hit (default 5 push iterations — wait cycles don't count), **or**
- two consecutive **post-fix** iterations produce identical drilldown bullets (you're not making progress, or the bot has flagged something un-fixable from inside the diff like wide-fanout maintainability risk).

### Pending states are not stop conditions, but they're not push conditions either

Two distinct things can be "pending", and the loop waits instead of pushing in both cases (pushing resets `head_sha` and restarts the wait):

| Pending signal | Where to spot it | Loop behavior |
|---|---|---|
| **CI checks still running** | `_Still running_` section in drilldown is non-empty; verdict is `WAITING` | Re-ask every 60s, cap at 15 min/iteration. |
| **Greptile hasn't reviewed yet** | Card prose contains `Greptile pending` / `Greptile has not reviewed this PR yet`; the bot docks 1 from the score so verdict is usually `BLOCKED` even on otherwise-clean PRs | Re-ask every 120s, cap at 15 min/iteration. After the first 15 min, optionally post `@greptile review` on the PR (one shot only — never spam), then wait one more cycle. |

If the only remaining penalty after fixes is `Greptile pending`, the loop will sit in the wait state rather than push — a new commit just delays Greptile further. If the wait caps out, the loop stops with a "still pending after 15 min" report and lets the user decide.

## What it intentionally does **not** fix

| Drilldown section | Why we skip |
|---|---|
| `_Unrelated failures_` | Bot already classified these as infra/cross-PR. Pushing your branch won't fix them. |
| `_Policy / meta failures_` | DCO, source-branch, CLA. Out-of-scope unless the user asks. The bot zero-penalizes these so the loop can finish READY with one still red. |
| `_Tech debt (FYI, not blocking)_` | Explicitly non-blocking by name in the bot's own card. |

If you want the loop to handle policy failures too, say so when invoking it — the skill will surface them and the agent can decide.

## Layout

```text
litellm-loop/
├── SKILL.md                   ← agent instructions (the prompt)
├── README.md                  ← this file
├── commands/
│   └── litellm-loop.md        ← /litellm-loop <pr-url>
└── agents/
    └── litellm-loop.md        ← sub-agent definition for delegation
```

## Origin

Same shape as [`/greploop`](https://github.com/greptileai/greptile-skills/tree/main/greptile/greploop) (review → fix → push → re-review until 5/5), but rewired for `litellm-bot`: the score is `Merge Confidence: N/5`, the verdict is `READY / BLOCKED / WAITING`, and the rubric covers CI failures + pattern conformance + karpathy-style production risk in addition to Greptile's confidence number. The triage and pattern data layers it consumes live in this same repo under `litellm-pr-reviewer/` and `litellm-pattern-conformance-reviewer/`.

## License

MIT.
