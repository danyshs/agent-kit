rule: pre-push

# Review before a branch's first push

With this module's git hook installed in a repo, the first push of any branch other than main or master asks for confirmation that a review ran. It can't check that one really did, so that part is on you.

- On a feature branch, judge your changes against tiger-style as you go, if the tiger-style plugin is installed: safety first, then performance, then developer experience; assertions, a limit on everything, functions of 70 lines or fewer (140 at most), minimal abstractions, explicit control flow, units in names, and comments that say why. Say plainly where a change doesn't match, including structural choices such as new packages or new abstractions.
- The hook asks for "yes" on a terminal, and your shell has none. So: run a tiger-style review of the branch's whole diff, fix every P0 finding, then push with `REVIEW_DONE=1 git push ...`.
- Never set `REVIEW_DONE=1` without having run the review, and never use `--no-verify`.
- This is a duty to assess, not a licence to over-engineer.
