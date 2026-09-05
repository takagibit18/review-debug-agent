# Review Skill retrieval-v1 fixture bank

This bank is isolated from the production JSONL files and is intentionally small. Its five active skills map to reviewed Golden PR mechanisms:

- `skill-wrapper-equality`: pytest PR 9350 changes wrapper equality and hashing.
- `skill-offline-fallback`: SpeechRecognition PR 657 replaces a local default with network discovery.
- `skill-provider-fallback`: OpenClaw PR 37717 changes omitted-provider-field fallback behavior.
- `skill-runtime-region`: Nethermind PR 5381 adds a NoGC region to an Engine API handler.
- `skill-derived-state`: FastAPI reverse PR 15077 drops derived streaming schema state during route recreation.

Pydantic PR 12568 is a reviewed clean control with no expected skills. Candidate and deprecated records deliberately contain common triggers to prove lifecycle filtering.
