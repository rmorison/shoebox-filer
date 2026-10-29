---
title: An upgrade across an env-file layout change stops at the commit before it
date: 2026-10-28
category: best-practices
module: deploy
problem_type: best_practice
component: infrastructure
severity: medium
applies_when:
  - A release changes which files the production compose file needs before a container can start, such as a new required env file
  - A one-time step on the server has to run before that release, under the old layout
  - A server can be more than one release behind when it is upgraded
  - Writing or reviewing an upgrade section in docs/user/server-deployment.md
symptoms:
  - "An upgrade step says to run an earlier section \"on the current checkout first\", but the server's checkout doesn't have that section yet"
  - "After git pull, docker compose config and up fail with env file ... not found before touching a container"
root_cause: missing_workflow_step
resolution_type: documentation_update
related_components:
  - documentation
  - development_workflow
tags: [upgrade, env-file, docker-compose, runbook, git-log-pickaxe, first-parent, intermediate-commit]
---

# An upgrade across an env-file layout change stops at the commit before it

## Context

Two releases in a row changed how the server is configured. The first added separate database logins: a one-time switch on the server, run with the server deployment guide's section on separate logins. The second moved the owner credentials into a new `.env.db`, and its compose file needs that file: `deploy/docker-compose.yml` names `env_file: ../.env.db` for `db`, and `config` and `up` fail without it. The split's section said to run the logins section "on the current checkout first, before `git pull`", because the logins switch recreates the scheduler, and the new compose file can't do that until `.env.db` exists.

The first production upgrade across both releases came from a checkout older than either one. "The current checkout" had neither the logins section nor its migration, and pulling to main brought in a compose file that wouldn't start. Before choosing a route, the upgrade first established which commit the server's checkout was on (session history). It then ran in two stages: check out the logins change's merge commit, run the logins section there, pull to main, and run the split. Both stages passed, but the guide gave neither the stop nor a way to find it.

## Guidance

**Treat the commit just before a layout change as a required stop.** When a release changes what the compose file needs before it can start (a new env file, a renamed one, a required bind mount), any one-time step that must run under the old layout has to run at the last commit before the change. A server further behind goes to that commit first, finishes the step there, and only then moves on. The upgrade route depends on where the server's checkout is, so check that first.

**Give the reader a command that finds the stop, not a hash and not "the current checkout".** Find the first main commit that brought in a string only the new layout has, then take its first parent:

```bash
git fetch
git checkout "$(git log --first-parent --format=%H -S'../.env.db' origin/main -- deploy/docker-compose.yml | tail -1)^1"
```

- `-S'<string>'` (git's pickaxe) lists commits that change how many times the string appears in the file. `tail -1` keeps the oldest, the one that brought the string in.
- `--first-parent` keeps the walk on main's own commits, so the result is a merge on main and `^1` is main as it was just before that merge. Without it, the result can be a commit inside the feature branch, and its `^1` is wherever that branch started. In this repository's history both forms happen to land on the same commit, because the split's branch started from the logins merge. A branch started earlier would not.
- A hash pasted into the guide is just as exact, but the repository's docs don't cite commits, and a hash hides how it was found.

To check whether a server has already passed the stop, run `git merge-base --is-ancestor <stop> HEAD` on its checkout. Exit 0 means it is at or past the stop.

**Check the command against history before publishing it.** At the commit it finds, the file must not name the new layout yet, and the one-time step's section must exist:

```bash
c="$(git log --first-parent --format=%H -S'../.env.db' origin/main -- deploy/docker-compose.yml | tail -1)^1"
git show "$c":deploy/docker-compose.yml | grep -c 'env.db'               # 0
git show "$c":docs/user/server-deployment.md | grep -c '^### 8.4'        # 1
```

**Write the one-time step so it runs at the stop.** Its commands can't assume the new layout, because the stop's compose file reads only the old one. Tell the reader to follow the step from the default branch's copy of the guide, not the copy in the older checkout, which predates any fix to the step. Then return to the default branch (`git checkout main`) before the `git pull` that brings in the next release. A detached HEAD can't pull.

## Why This Matters

"On the current checkout" quietly assumes the server is exactly one release behind. A production server skips releases whenever upgrades are rare, and a server held back on purpose (here, with the schedule off until a backfill finished) skips more. When the precondition can't be met, the reader has to work out the stop from the history in the middle of a production change. That is slow, and getting it wrong leaves a compose file that won't start, or a one-time step skipped altogether.

## When to Apply

- A release adds, renames or newly requires an env file, volume or bind mount that a service needs to start.
- An upgrade section in the guide says to run another section "first" or "before `git pull`".
- Reviewing a release whose upgrade notes assume the previous release is already deployed.

## Examples

Before, the precondition in the split's section:

> On a server still on the owner login, run 8.4 on the current checkout first, **before** `git pull` brings in this release.

After, the same paragraph, followed by:

> If the checkout predates 8.4 as well, it has neither 8.4's steps nor its migration. Go first to the last commit whose compose file doesn't need `.env.db`, which has both, and run 8.4 there.

Then the checkout command above, a `build`, "run 8.4 from this guide as main has it, not from the copy in the older checkout", and `git checkout main` before going on with the split.

## Related

- `docs/user/server-deployment.md`, sections 8.4 (separate database logins), 8.5 (splitting the owner credentials out of `.env`) and 8.6 (with the scheduler stopped)
- `deploy/docker-compose.yml`, the `env_file` keys of `db` and `scheduler`
