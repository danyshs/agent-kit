rule: fork-pr-check

# Forks: a bare `gh` lookup is not proof that a PR is absent

In a clone with two remotes (your fork and the upstream repo), a bare `gh pr view` or `gh pr checks` can look in the wrong repo and show nothing. Before you say a PR is absent or has no review comments, read the `fork-pr-check` skill and run the check it names.
