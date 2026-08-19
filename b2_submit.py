import json

jp = "eval/outputs/graph-ab-qwen-2x2-haystack/run_journals/golden_deepset-ai_haystack_pr12257_reverse_B2-graph-hybrid-warm_f10e36a8-85ca-4748-99dc-b96952ad4633_journal.jsonl"
print("=== B2 s1 submit_review content ===")
for line in open(jp, encoding="utf-8"):
    d = json.loads(line)
    tc = (d.get("payload", {}) or {}).get("tool_calls", [])
    for call in tc:
        fn = call.get("function", {})
        if "submit" in str(fn.get("name", "")):
            args = json.loads(fn["arguments"]) if isinstance(fn.get("arguments"), str) else fn.get("arguments", {})
            print("tool:", fn.get("name"))
            print("summary:", args.get("summary", "")[:400])
            for i, iss in enumerate(args.get("issues", [])):
                print(f"  issue[{i}] severity={iss.get('severity')} location={iss.get('location')}")
                print(f"    evidence[:250]: {str(iss.get('evidence',''))[:250]}")
