import json, glob, os

print("=== sample-1 runs on #12257 (measured) ===")
for line in open("eval/outputs/graph-ab-qwen-2x2-haystack/checkpoint.jsonl", encoding="utf-8"):
    d = json.loads(line)
    if d.get("status") != "measured":
        continue
    if d.get("fixture_id") != "golden_deepset-ai_haystack_pr12257_reverse":
        continue
    if d.get("sample_index") != 1:
        continue
    rr = d.get("run_record", {})
    print(f"\nvariant={d.get('variant_id')} run_id={rr.get('run_id')}")
    print("  journal:", rr.get("lifecycle", {}).get("run_journal_path"))
    print("  result.expected_count:", rr.get("result", {}).get("expected_count"))
    print("  result.actual_count:", rr.get("result", {}).get("actual_count"))
    print("  result.matched_count:", rr.get("result", {}).get("matched_count"))
    print("  process.model_raw_issue_count:", rr.get("result", {}).get("process_metrics", {}).get("model_raw_issue_count"))
    print("  process.draft_findings_created:", rr.get("result", {}).get("process_metrics", {}).get("draft_findings_created"))

# also map run_id -> event log path
print("\n=== event log files for these run_ids ===")
journals = glob.glob("eval/outputs/graph-ab-qwen-2x2-haystack/run_journals/golden_deepset-ai_haystack_pr12257_reverse_*_journal.jsonl")
for j in sorted(journals):
    print("  ", os.path.basename(j))
elogs = glob.glob("eval/outputs/event_logs/golden_deepset-ai_haystack_pr12257_reverse_*.jsonl")
print("event logs (by mtime, newest 6):")
for e in sorted(elogs, key=os.path.getmtime)[-6:]:
    print("  ", os.path.basename(e), "mtime", int(os.path.getmtime(e)))
