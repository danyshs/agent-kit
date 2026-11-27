---
name: fork-pr-check
description: How to check for a pull request or its review comments with gh in a fork clone, where a bare gh pr view can look in the wrong repo. Read before saying a PR does not exist or has no comments.
---

# Forks: a bare `gh` lookup is not proof that a PR is absent

In a clone with two remotes, your fork and the original (upstream) repo, `gh` can't always tell which repo is "the" repo. A bare `gh pr view` or `gh pr checks` then looks in the wrong place and shows nothing, which looks exactly like "no PR exists". It once made an agent report there was no open PR when one was sitting upstream.

- On first use of `gh` in a fork clone, pin the upstream once: `gh repo set-default <upstream-owner>/<repo>`. Check with `gh repo set-default --view`. "No default remote repository has been set" is the condition that causes the wrong lookup.
- Before saying a PR is absent, or has no review comments, confirm with a query that names the repo: `gh pr status`, `gh pr view <n> --repo <upstream>`, `gh api repos/<upstream>/pulls/<n>/comments`, or `gh pr view <n> --repo <upstream> --json reviews`.
- Treat "no open PR" and "no review comments" as unverified until such a query says so. An empty bare result is not evidence.
- Don't blame git transport without proof. An SSH host alias affects git, not `gh`'s API login, and GitHub repo names ignore case. The usual cause is two remotes and no `gh` default.
