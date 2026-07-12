# Reproducible Golden Eval Design

## Goal

Make Golden fixture restoration work in the current Windows sandbox without
changing global Git configuration or disabling TLS validation.

## Design

The Eval runner accepts `EVAL_GIT_SSL_BACKEND` with `system` as the default and
`openssl` as the opt-in compatible backend. Every Eval Git subprocess receives
the configured backend through Git's per-command `-c` option. Git commands also
trust only their current working directory and explicit local cache source
directories, never a global wildcard.

Before refreshing a cache remote, the runner checks whether the fixture's exact
checkout SHA already exists. A cache hit is cloned locally with no network call;
only a miss performs a remote update/fetch.

## Acceptance

`git -c http.sslBackend=openssl` reaches GitHub, cache hits do not issue remote
updates, and a Golden run emits a report after all selected fixtures complete.
