rule: pre-push

# Review before a branch's first push

With this plugin's git hook in a repo, the first push of a branch other than main or master asks on a terminal whether a review ran. Your shell has none, so run a tiger-style review of the branch's whole diff, fix every P0 finding, then push with `REVIEW_DONE=1 git push ...`. Never set `REVIEW_DONE=1` without that review, and never use `--no-verify`: the hook can't check, so it's on you.

On a feature branch, judge changes against tiger-style as you go, and say plainly where one doesn't match.
