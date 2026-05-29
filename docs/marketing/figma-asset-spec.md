# MergeWarden Marketing Figma Asset Spec

Figma source: https://www.figma.com/design/rgAm606tvflaRqlkhsuSK7

## Direction

The redesign uses a restrained blue-white product system with GitHub-native
scenes. It avoids abstract AI illustration and uses product evidence as the
visual language: pull request diff, neutral check, changed-line comments, run
summary, and publish artifacts.

## Asset Set

- `hero-pr-review-scene.svg`: first-viewport product scene with PR diff,
  neutral advisory check, two changed-line comment cards, and explicit CI
  authority boundary.
- `workflow-evidence-path.svg`: middle-page workflow from PR diff to
  `changed_lines.json`, `review_response`, `run_summary`, and GitHub publish.
- `proof-artifact-strip.svg`: proof strip for changed-line eligibility, run
  summary artifact, fingerprint lifecycle, and neutral check.

## Implementation Notes

- Keep text and controls code-native in HTML/CSS. SVG text is only product-scene
  labeling and should remain short, English, and non-critical for translation.
- Use the language switch for visible page copy, nav, CTA, FAQ, meta text, and
  image alt text.
- Preserve the advisory boundary in every future visual iteration: MergeWarden
  supplies evidence and suggestions, while CI, branch protection, and human
  reviewers keep merge authority.
