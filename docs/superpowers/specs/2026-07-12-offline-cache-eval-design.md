# Offline Cache Eval Design

## Goal

Allow a live model evaluation to run from already-cached Git fixture mirrors when
remote Git access is unavailable. The mode must not alter the default online
evaluation behavior.

## Configuration

`EVAL_OFFLINE_WORKSPACE_CACHE=false` remains the default. When set to `true`,
the runner uses only `eval/outputs/workspace_cache` and never executes a remote
update, fetch, or clone for Git-backed fixtures.

## Workspace behavior

For each Git fixture, the runner computes the existing deterministic cache key.
If a valid mirror exists and contains `checkout_sha`, it clones locally from that
mirror and checks out the requested commit. If the mirror is absent or the commit
is absent, workspace preparation fails with a clear `offline cache miss` error.
The normal Eval result captures that failure per fixture, preserving the rest of
the run and its diagnostics.

## Safety and compatibility

The offline flag is opt-in. It does not write to Git global configuration, does
not weaken TLS validation, and does not mutate fixture data. Local cache cloning
uses the existing temporary workspace lifecycle. The cached mirror is treated as
read-only input.

## Verification

Tests cover: offline cache hit does not call a remote update; cache miss reports
an actionable error; online mode keeps remote-update behavior. A live single
sample Eval runs only the fixtures whose cached mirrors contain the required
commit, then emits the standard report and diagnostics.
