# MergeWarden Marketing Page

This directory contains a standalone static product page for MergeWarden. It is
intentionally separate from the Python runtime, CLI, FastAPI app, and GitHub
Actions workflow code.

## Preview

Open `index.html` directly in a browser.

## Deployment

The page has no build step. It can be deployed as a static site through GitHub
Pages, Vercel, Netlify, or any static file host by serving this directory.

## Product Boundary

The page presents MergeWarden as an advisory AI PR gatekeeper. It should not
claim to replace CI, approve merges, or act as a hard merge gate.
