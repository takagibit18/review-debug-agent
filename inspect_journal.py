import json, sys

def inspect(journal_path, label):
    print(f"\n{'='*72}\n{label}\n{'='*72}")
    print("journal:", journal_path.split("run_journals")[-1])
    types = {}
    events = []
    for line in open(journal_path, encoding="utf-8"):
        try:
            d = json.loads(line)
        except Exception:
            continue
        events.append(d)
        t = d.get("event_type") or d.get("type") or d.get("kind") or "?"
        types[t] = types.get(t, 0) + 1
    print("total events:", len(events))
    print("event types:", types)

    # Print finding/verifier/submit-related events in full (truncated)
    keywords = ["finding", "verif", "submit", "draft", "consolidat", "repair", "reject", "issue"]
    for d in events:
        blob = json.dumps(d, ensure_ascii=False, default=str).lower()
        if any(k in blob for k in keywords):
            t = d.get("event_type") or d.get("type") or d.get("kind") or "?"
            print(f"\n--- [{t}] ---")
            print(json.dumps(d, ensure_ascii=False, default=str)[:1400])

inspect("eval/outputs/graph-ab-qwen-2x2-haystack/run_journals/golden_deepset-ai_haystack_pr12257_reverse_A-agent-search_7c091434-bc77-46e6-a496-74242e226223_journal.jsonl", "A-agent-search s1 (#12257)")
