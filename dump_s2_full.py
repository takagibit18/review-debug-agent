import json

def dump_submit(journal_path, label):
    print(f"\n{'#'*78}\n# {label}\n{'#'*78}")
    print("journal:", journal_path.split("run_journals")[-1])
    found = False
    for line in open(journal_path, encoding="utf-8"):
        d = json.loads(line)
        tc = (d.get("payload", {}) or {}).get("tool_calls", [])
        for call in tc:
            fn = call.get("function", {})
            if "submit" not in str(fn.get("name", "")):
                continue
            found = True
            args = fn.get("arguments", "{}")
            if isinstance(args, str):
                args = json.loads(args)
            print("\n=== submit_review tool:", fn.get("name"), "===")
            print("\n--- summary ---")
            print(args.get("summary", ""))
            print("\n--- issues (FULL, all fields) ---")
            for i, iss in enumerate(args.get("issues", [])):
                print(f"\n  issue[{i}] keys: {sorted(iss.keys())}")
                for k, v in iss.items():
                    print(f"\n  issue[{i}].{k}:")
                    print(f"    {json.dumps(v, ensure_ascii=False, indent=4) if not isinstance(v, str) else v}")
    if not found:
        print("(no submit_review call found in journal)")

# Also dump any record_draft_finding calls
def dump_drafts(journal_path, label):
    print(f"\n=== {label}: record_draft_finding calls (if any) ===")
    found = False
    for line in open(journal_path, encoding="utf-8"):
        d = json.loads(line)
        tc = (d.get("payload", {}) or {}).get("tool_calls", [])
        for call in tc:
            fn = call.get("function", {})
            if "draft" in str(fn.get("name", "")).lower() or "record_finding" in str(fn.get("name", "")):
                found = True
                args = fn.get("arguments", "{}")
                if isinstance(args, str):
                    args = json.loads(args)
                print(f"\n  draft tool: {fn.get('name')}")
                print(f"  args: {json.dumps(args, ensure_ascii=False, indent=2)[:2000]}")
    if not found:
        print("  (none)")

dump_submit("eval/outputs/graph-ab-qwen-2x2-haystack/run_journals/golden_deepset-ai_haystack_pr12162_reverse_A-agent-search_6b62c5c0-c1cf-4265-97bb-76aac24b770c_journal.jsonl", "A-agent-search s2 (#12162, rid=6b62c5c0)")
dump_drafts("eval/outputs/graph-ab-qwen-2x2-haystack/run_journals/golden_deepset-ai_haystack_pr12162_reverse_A-agent-search_6b62c5c0-c1cf-4265-97bb-76aac24b770c_journal.jsonl", "A s2")
