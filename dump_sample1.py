import json

runs = {}
for line in open("eval/outputs/graph-ab-qwen-2x2-haystack/checkpoint.jsonl", encoding="utf-8"):
    d = json.loads(line)
    if d.get("status") != "measured":
        continue
    key = (d.get("variant_id"), d.get("sample_index"))
    runs[key] = d

def show(key, label):
    d = runs.get(key)
    if not d:
        print(f"  {label}: NOT FOUND")
        return
    rr = d.get("run_record", {})
    result = rr.get("result", {}) if isinstance(rr, dict) else {}
    metrics = rr.get("metrics", {}) if isinstance(rr, dict) else {}
    print(f"\n=== {label} ===")
    print("  top-level run_record keys:", sorted(rr.keys()) if isinstance(rr, dict) else type(rr))
    # Dump everything compactly
    print(json.dumps(rr, ensure_ascii=False, indent=2, default=str)[:4000])

# Sample 1 pair on fixture #12257
show(("A-agent-search", 1), "A-agent-search s1 (#12257)")
show(("B2-graph-hybrid-warm", 1), "B2-graph-hybrid-warm s1 (#12257)")
