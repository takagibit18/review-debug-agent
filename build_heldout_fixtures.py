"""Construct 3 held-out P0 fixtures + update manifest + schema validation."""
import json
import sys
from pathlib import Path

REPO = Path("E:/PycharmProjects/MergeWarden-recovered")
TMP = Path("E:/tmp-heldout-p0")
FIXTURES = REPO / "eval" / "fixtures"

# ---- P0-1: pydantic-ai #6205 reverse ----
p0_1_diff = (TMP / "p0-1.diff").read_text(encoding="utf-8")
p0_1 = {
    "id": "golden_pydantic_pydantic-ai_pr6205_reverse",
    "type": "review",
    "source": {
        "repo_full_name": "pydantic/pydantic-ai",
        "pr_number": 6205,
        "url": "https://github.com/pydantic/pydantic-ai/pull/6205",
        "merge_commit_sha": "e918b831d0b79fad76f70f2bce3bc4a7712f623d",
        "title": "Reverse fixture: FileUrl.force_download is lost across UI adapter round-trips",
    },
    "input": {
        "diff_text": p0_1_diff,
        "files": {},
        "error_log": None,
        "workspace": {
            "kind": "git",
            "repo_url": "https://github.com/pydantic/pydantic-ai.git",
            "base_sha": "6041f2d124122cdbe3f8835c862a7864c5a4941e",
            "head_sha": "d7e399521c1d1a7f617af88dfa8ef0a213382761",
            "checkout_sha": "d7e399521c1d1a7f617af88dfa8ef0a213382761",
            "diff_base_sha": "6041f2d124122cdbe3f8835c862a7864c5a4941e",
            "apply_fixture_diff": False,
            "review_scope": "legacy",
        },
    },
    "expected": {
        "issues": [
            {
                "severity": "warning",
                "location_pattern": "pydantic_ai_slim/pydantic_ai/ui/vercel_ai/_adapter.py",
                "path": "pydantic_ai_slim/pydantic_ai/ui/vercel_ai/_adapter.py",
                "line": 1010,
                "end_line": 1010,
                "category": "serialization",
                "description": "Vercel AI and typed AG-UI multimodal adapter dump paths omit FileUrl.force_download from their transport metadata, and load paths reconstruct FileUrl instances without that value. Non-default force_download modes (True or 'allow-local') are lost across dump_messages -> load_messages round-trips, falling back to the URL constructor default of False. Both parallel adapter paths violate the same round-trip invariant.",
                "root_cause_id": "fileurl-force-download-roundtrip-loss",
                "repair_unit": "persist and restore non-default FileUrl.force_download through the Vercel AI and typed AG-UI adapter metadata carriers so dump/load round-trips preserve the original FileUrl behavior",
                "mechanism_pattern": "parallel UI adapter dump paths omit force_download from their transport metadata and load paths reconstruct FileUrl instances without that value, causing non-default modes to fall back to False",
                "invariant_pattern": "dump/load round-trips must preserve behavior-bearing FileUrl metadata, including force_download, across every adapter representation that has a metadata carrier",
                "affected_paths": [
                    "pydantic_ai_slim/pydantic_ai/ui/vercel_ai/_adapter.py",
                    "pydantic_ai_slim/pydantic_ai/ui/ag_ui/_multimodal.py",
                ],
                "structural_scope": "direct_cross_file",
                "graph_observable": True,
            }
        ],
        "min_issues": 1,
        "max_issues": 1,
        "is_empty_annotation": False,
    },
    "metadata": {
        "suite": "golden",
        "tags": [
            "golden", "github", "review", "positive-sample", "should-detect",
            "held-out", "cross-file", "parallel-path", "round-trip",
            "serialization", "graph-sensitive", "pydantic-ai",
        ],
        "difficulty": "hard",
        "annotated_by": "manual",
        "reviewed": True,
    },
}

# ---- P0-2: FastAPI #15077 reverse ----
p0_2_diff = (TMP / "p0-2.diff").read_text(encoding="utf-8")
p0_2 = {
    "id": "golden_fastapi_fastapi_pr15077_reverse",
    "type": "review",
    "source": {
        "repo_full_name": "fastapi/fastapi",
        "pr_number": 15077,
        "url": "https://github.com/fastapi/fastapi/pull/15077",
        "merge_commit_sha": "ad03e117c0010a563067740c97cb7ab011cb5174",
        "title": "Reverse fixture: include_router drops derived stream item type",
    },
    "input": {
        "diff_text": p0_2_diff,
        "files": {},
        "error_log": None,
        "workspace": {
            "kind": "git",
            "repo_url": "https://github.com/fastapi/fastapi.git",
            "base_sha": "bdcff30ca261b51a364d8aa55f5c6bf071cce1fa",
            "head_sha": "98b12fe56f97107e71a20fc1cf334ccfa590efb5",
            "checkout_sha": "98b12fe56f97107e71a20fc1cf334ccfa590efb5",
            "diff_base_sha": "bdcff30ca261b51a364d8aa55f5c6bf071cce1fa",
            "apply_fixture_diff": False,
            "review_scope": "legacy",
        },
    },
    "expected": {
        "issues": [
            {
                "severity": "warning",
                "location_pattern": "fastapi/routing.py",
                "path": "fastapi/routing.py",
                "line": 988,
                "end_line": 988,
                "category": "logic",
                "description": "When include_router recreates an API route, the already-derived stream_item_type is not forwarded to the new route. The recreated route resets stream_item_type to None and can no longer rederive it from the endpoint annotation because the response model state has already changed from the initial DefaultPlaceholder. Runtime streaming continues to work, but the OpenAPI schema loses typed stream item information: SSE itemSchema loses contentSchema/contentMediaType and JSONL itemSchema degrades to empty or missing.",
                "root_cause_id": "include-router-drops-stream-item-type",
                "repair_unit": "propagate the already-derived stream_item_type when include_router recreates an API route instead of resetting that derived route metadata",
                "mechanism_pattern": "include_router recreates a route without forwarding its precomputed stream_item_type; the recreated route resets it to None and can no longer rederive it from the endpoint annotation because the response model state has already changed",
                "invariant_pattern": "route cloning and router inclusion must preserve derived route metadata required for downstream runtime/schema behavior",
                "affected_paths": ["fastapi/routing.py"],
                "structural_scope": "multi_hop",
                "graph_observable": True,
            }
        ],
        "min_issues": 1,
        "max_issues": 1,
        "is_empty_annotation": False,
    },
    "metadata": {
        "suite": "golden",
        "tags": [
            "golden", "github", "review", "positive-sample", "should-detect",
            "held-out", "multi-hop", "state-propagation", "openapi",
            "streaming", "graph-sensitive", "fastapi",
        ],
        "difficulty": "hard",
        "annotated_by": "manual",
        "reviewed": True,
    },
}

# ---- P0-3: pytest #14829 forward clean control ----
p0_3_diff = (TMP / "p0-3.diff").read_text(encoding="utf-8")
p0_3 = {
    "id": "golden_pytest-dev_pytest_pr14829",
    "type": "review",
    "source": {
        "repo_full_name": "pytest-dev/pytest",
        "pr_number": 14829,
        "url": "https://github.com/pytest-dev/pytest/pull/14829",
        "merge_commit_sha": "e739014f38d284f1ce57745cdd4e13392fb56165",
        "title": "Fix duplicated exception chain output when an exception group has a cause",
    },
    "input": {
        "diff_text": p0_3_diff,
        "files": {},
        "error_log": None,
        "workspace": {
            "kind": "git",
            "repo_url": "https://github.com/pytest-dev/pytest.git",
            "base_sha": "68308aa288e00ff84880572ed9b3590f6cd7d470",
            "head_sha": "d07e8eff479f5b37749fc406462d1fdff8b7e17a",
            "checkout_sha": "d07e8eff479f5b37749fc406462d1fdff8b7e17a",
            "diff_base_sha": "68308aa288e00ff84880572ed9b3590f6cd7d470",
            "apply_fixture_diff": False,
            "review_scope": "legacy",
        },
    },
    "expected": {
        "issues": [],
        "min_issues": 0,
        "max_issues": 0,
        "is_empty_annotation": True,
    },
    "metadata": {
        "suite": "golden",
        "tags": [
            "golden", "github", "review", "negative-sample", "clean-control",
            "pytest", "exception-group", "traceback", "held-out",
        ],
        "difficulty": "medium",
        "annotated_by": "manual",
        "reviewed": True,
    },
}

# ---- Write fixtures ----
fixtures = [
    ("golden_pydantic_pydantic-ai_pr6205_reverse", p0_1),
    ("golden_fastapi_fastapi_pr15077_reverse", p0_2),
    ("golden_pytest-dev_pytest_pr14829", p0_3),
]
for fid, data in fixtures:
    path = FIXTURES / f"{fid}.json"
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"WRITTEN: {path} ({path.stat().st_size} bytes)")

# ---- Update manifest ----
manifest_path = FIXTURES / "manifest.json"
manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
new_entries = [
    {
        "fixture_id": "golden_pydantic_pydantic-ai_pr6205_reverse",
        "suite": "golden",
        "fixture_type": "review",
        "repo_full_name": "pydantic/pydantic-ai",
        "pr_number": 6205,
        "path": "eval/fixtures/golden_pydantic_pydantic-ai_pr6205_reverse.json",
        "reviewed": True,
    },
    {
        "fixture_id": "golden_fastapi_fastapi_pr15077_reverse",
        "suite": "golden",
        "fixture_type": "review",
        "repo_full_name": "fastapi/fastapi",
        "pr_number": 15077,
        "path": "eval/fixtures/golden_fastapi_fastapi_pr15077_reverse.json",
        "reviewed": True,
    },
    {
        "fixture_id": "golden_pytest-dev_pytest_pr14829",
        "suite": "golden",
        "fixture_type": "review",
        "repo_full_name": "pytest-dev/pytest",
        "pr_number": 14829,
        "path": "eval/fixtures/golden_pytest-dev_pytest_pr14829.json",
        "reviewed": True,
    },
]
existing_ids = {e["fixture_id"] for e in manifest["entries"]}
for entry in new_entries:
    if entry["fixture_id"] not in existing_ids:
        manifest["entries"].append(entry)
manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
print(f"MANIFEST UPDATED: {len(manifest['entries'])} entries total")

# ---- Schema validation ----
sys.path.insert(0, str(REPO))
from eval.schemas import Fixture
for fid, _ in fixtures:
    path = FIXTURES / f"{fid}.json"
    fx = Fixture.model_validate_json(path.read_text(encoding="utf-8"))
    exp_count = len(fx.expected.issues)
    print(f"SCHEMA PASS: {fid} | expected_issues={exp_count} | type={fx.type}")
    if exp_count > 0:
        iss = fx.expected.issues[0]
        print(f"  structural_scope={iss.structural_scope} graph_observable={iss.graph_observable}")
        print(f"  path={iss.path} line={iss.line}")

print("\nALL DONE")
