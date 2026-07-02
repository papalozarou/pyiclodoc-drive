# Local project rules

These rules apply only to this repository. They extend `AGENTS.md`.

## Review notes

- MUST: Treat review notes as local-only scratch or review material.
- MUST NOT: Move review notes into tracked files unless explicitly requested.
- MUST NOT: Suggest tracking, committing, or force-adding review-note files such
  as `CODE_REVIEW.md`.

## Context notes

- MUST: Record every major decision in `.context/` as a timestamped Markdown
  file.
- MUST: Treat a major decision as any choice that changes implementation
  direction, root-cause understanding, scope boundary, rollback or revert
  strategy, validation strategy, or branch strategy.
- MUST: Use timestamped filenames in this format:
  - `YYYY-MM-DDThh-mm-ss+hh-mm-short-title.md`
- MUST: Write each context note in plain, practical UK English.
- MUST: Structure every context note using these sections in this order:
  - title
  - recorded timestamp
  - decision
  - why
  - consequence
  - course correction
- MUST: In the `course correction` section, state where the work went wrong,
  what had to change, and why the corrected direction is different.
- MUST: Add relative Markdown links to related context notes where they help
  explain the decision trail.
- MUST: Update an existing context note when a later decision materially
  changes the meaning, limits, or outcome of the earlier one.
- MUST: Keep context notes concise, factual, and specific. Do not pad them
  with narrative filler.
- MUST NOT: Treat `.context/` notes as a substitute for code comments, tests,
  commit messages, or operational documentation.
- MUST: Create a new context note before or during a major decision, not
  long after the fact.
- MUST: Prefer one note per major decision boundary rather than mixing
  unrelated decisions into one file.
