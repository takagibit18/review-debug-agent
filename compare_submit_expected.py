import json

# 1. Full submit_review arguments from A s1 journal
jp = "eval/outputs/graph-ab-qwen-2x2-haystack/run_journals/golden_deepset-ai_haystack_pr12257_reverse_A-agent-search_7c091434-bc77-46e6-a496-74242e226223_journal.jsonl"
print("=== A s1 model submit_review content ===")
for line in open(jp, encoding="utf-8"):
    d = json.loads(line)
    tc = (d.get("payload", {}) or {}).get("tool_calls", [])
    for call in tc:
        fn = call.get("function", {})
        if "submit" in str(fn.get("name", "")):
            args = json.loads(fn["arguments"]) if isinstance(fn.get("arguments"), str) else fn.get("arguments", {})
            print("tool:", fn.get("name"))
            print("summary:", args.get("summary", "")[:300])
            issues = args.get("issues", [])
            print("issues count:", len(issues))
            for i, iss in enumerate(issues):
                print(f"  issue[{i}]: severity={iss.get('severity')} location={iss.get('location')}")
                print(f"    evidence[:200]: {str(iss.get('evidence',''))[:200]}")

# 2. Expected issue from fixture
print("\n=== fixture #12257 expected ===")
fx = json.load(open("eval/fixtures/golden_deepset-ai_haystack_pr12257_reverse.json", encoding="utf-8"))
exp = fx.get("expected", {})
print("expected issues:", json.dumps(exp.get("issues", []), ensure_ascii=False, indent=2, default=str)[:1500])
