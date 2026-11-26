# Building the evidence file

The evidence file (`ACCOMPLISHMENTS.md` by default) is the friend's own record of what they did. Every number, date, name, language and link on the resume must trace to a confirmed fact in it. The friend's prose stays as they wrote it; each fact is a fenced block under the entry it belongs to.

## 1. Sources

Ask which sources you may read: old resumes, their repos, a LinkedIn export, transcripts, emails, anything else on the machine. Read only what they approve, and nothing private they didn't name.

## 2. Draft

Read `removed-questions.md` first if it exists. Never re-ask a removed question in other words, and let each `why:` shape what you ask instead: "numbers are under NDA" means ask for a relative or public measure, "I wasn't the lead on that" means ask what they owned.

Draft one fact per claim a resume could make, every field proposed, `status: draft`, and a `questions` line for anything you guessed. One result per fact: "cut build time 40%" and "led the migration" are two facts.

```fact
id: acme-build-time
entry: acme-backend
text: >
  Cut CI build time from 25 to 15 minutes by caching dependencies.
is_measure: yes
paid: yes
shipped: yes
dates: 2024-03 to 2024-06
source: old resume v3; their message 2026-11-20
confidential: no
status: draft
languages:
  - Python
links:
  - https://github.com/acme/ci/pull/12
questions:
  - Is 25 to 15 minutes measured, or a guess? A guess is written "about".
```

- `entry` is the resume entry's slug (`\rkEntry`); a fact with no resume entry yet still gets one (`other-<name>`).
- `is_measure: yes` needs a number in `text`.
- `confidential: yes` for anything an employer would not want public, and then `terms` must list what identifies it: client names, internal project names, distinctive phrases. Those terms are checked against every PDF.
- `dates` is `YYYY-MM to YYYY-MM` or `YYYY-MM to present`.

Write the blocks into the file as you go, and run `evidence.py lint <evidence>` after each edit.

## 3. Interview

About 30 minutes. Go fact by fact, the ones behind claims on the current resume first, filling each message up to the friend's batch size: steps 1-4 for one fact are several questions, and a batch can span several facts. Number each question and name its fact, so a reply like "3: yes, 40% from the CI dashboard" maps back without guessing.

1. Read the fact back in plain words. Ask if it's right; correct it with their words.
2. Ask for the number behind every claim of size, speed, money or scale, and how they know it. No number is fine: drop `is_measure` to `no`, never invent one.
3. For each fact from a private source, ask what would identify it, and list those as `terms`.
4. Ask each open question in `questions`; remove it once answered.
5. When every field is one they agreed to, set `status: confirmed`.

A question from steps 1-3 that the friend answers `SKIP` goes into the fact's `questions` list, so the next pass finds it through `evidence.py todo`.

The friend can stop at any point. Drafts and open questions stay in the file, and the next `/resume-setup` starts from `evidence.py todo`.

## 4. Validate

`evidence.py validate <evidence>` until it passes. It also refuses a save that loses a fact without `evidence.py delete <id>`, so a fact is never dropped by accident. Only confirmed facts back a claim on the page; a claim resting on a draft fails the check as "unconfirmed".
