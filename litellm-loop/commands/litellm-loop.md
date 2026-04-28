---
name: litellm-loop
description: Drive a BerriAI/litellm PR through repeated rounds of litellm-bot review until it returns 5/5 READY. Pass the PR URL or owner/repo#N as the argument; if omitted, the PR is detected from the current branch.
argument-hint: [pr-url-or-ref]
---

Drive the litellm PR `$ARGUMENTS` to a clean `5/5 READY` verdict using the `litellm-loop` skill. Follow the skill's loop verbatim: detect the PR (use `gh pr view` if `$ARGUMENTS` is empty), pick the transport (`http` if `LITELLM_BOT_URL` is set, else `slack` via `SLACK_WEBHOOK_URL`), then on each iteration: post to the bot, parse the `*Merge Confidence: N/5*` card and threaded drilldown, fix only what the drilldown blocks on (in rubric-weight order: conflicts → PR-related CI → high-risk pattern → scope drift → medium-risk pattern → priors → karpathy), commit, push, and re-ask.

Stop when the bot returns 5/5 READY with no blocking drilldown entries, or after 5 iterations, or when two consecutive drilldowns are identical (no progress). Never fabricate a verdict — the bot's reply is the only source of truth.

If neither `LITELLM_BOT_URL` nor `SLACK_WEBHOOK_URL` is set, tell the user and stop.
