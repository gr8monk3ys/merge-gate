---
status: accepted
date: 2026-08-28
---

# Merge exactly the judged head; never arm auto-merge

The gate merges a green PR immediately with `--match-head-commit <judged sha>` and disables any auto-merge it finds, instead of enabling GitHub auto-merge. An arm is standing permission to merge future content on a judgement about past content: on 2026-08-17 Dependabot force-pushed a major bump into an armed PR and it landed 130 seconds later, past the version rule. With a head-matched merge GitHub refuses server-side if the head moved, at the price of one sweep of latency. See DECISIONS.md section 14.
