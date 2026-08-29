# MergeWarden Marketing Site

This is the deployable static marketing surface for MergeWarden. It is separate
from the Python runtime, CLI, FastAPI app, and GitHub Actions implementation.

## Preview

```powershell
python -m http.server 4174 -d E:\PycharmProjects\Debug\marketing
```

Open `http://localhost:4174`.

## Deployment

Use `marketing` as the Vercel project root. No build command is required; the
directory is served as a static site.

## Product Boundary

The page presents MergeWarden as an advisory AI PR gatekeeper. It provides
neutral soft checks, structured evidence, changed-line comments, and run
summary artifacts. It must not claim to replace CI, auto-approve pull requests,
or take over branch protection.

## Design Source

Figma concept: https://www.figma.com/design/rgAm606tvflaRqlkhsuSK7

Asset details are documented in `../docs/marketing/figma-asset-spec.md`.
