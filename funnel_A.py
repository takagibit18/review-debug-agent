import json

el = "eval/outputs/event_logs/golden_deepset-ai_haystack_pr12257_reverse_7c091434-bc77-46e6-a496-74242e226223.jsonl"
print("=== A s1 event log: finding/verifier/draft/submit funnel ===")
keywords = ["finding", "verif", "draft", "submit", "consolidat", "repair", "reject", "match", "funnel"]
for line in open(el, encoding="utf-8"):
    d = json.loads(line)
    t = d.get("event_type", "?")
    blob = json.dumps(d, ensure_ascii=False, default=str).lower()
    if any(k in blob for k in keywords):
        print(f"\n--- [{t}] ---")
        print(json.dumps(d.get("payload", d), ensure_ascii=False, default=str)[:1100])
