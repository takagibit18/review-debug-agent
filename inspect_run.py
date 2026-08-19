import json, sys
f = "eval/outputs/event_logs/golden_deepset-ai_haystack_pr12257_reverse_7c091434-bc77-46e6-a496-74242e226223.jsonl"
auth = 0
tokens = 0
model = None
finish_reasons = []
errors = []
for line in open(f, encoding="utf-8"):
    d = json.loads(line)
    p = d.get("payload", {})
    if not isinstance(p, dict):
        continue
    if p.get("model"):
        model = p["model"]
    et = str(p.get("error_type", ""))
    if "AuthenticationError" in et or p.get("code") == "auth_failed":
        auth += 1
        errors.append(p.get("message", "")[:200])
    if p.get("tokens", 0) > 0:
        tokens += p.get("tokens", 0)
    if p.get("model_finish_reason"):
        finish_reasons.append(p["model_finish_reason"])
print("event_log:", f.split("/")[-1])
print("model:", model)
print("auth_errors:", auth)
print("total_tokens:", tokens)
print("finish_reasons:", finish_reasons)
if errors:
    print("first_error:", errors[0])
print("--- checkpoint ---")
for line in open("eval/outputs/graph-ab-qwen-2x2-haystack/checkpoint.jsonl", encoding="utf-8"):
    d = json.loads(line)
    print("  ", d.get("fixture_id"), d.get("variant_id"), "s" + str(d.get("sample_index")), "status=" + str(d.get("status")), "valid=" + str(d.get("valid")))
