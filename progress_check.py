import json, glob, os

print("=== ALL checkpoint records (3rd run) ===")
runs = []
for line in open("eval/outputs/graph-ab-qwen-2x2-haystack/checkpoint.jsonl", encoding="utf-8"):
    d = json.loads(line)
    runs.append(d)
    print("  ", d.get("fixture_id", "?")[-30:],
          d.get("variant_id", "?"),
          "s" + str(d.get("sample_index")),
          "status=" + str(d.get("status")),
          "valid=" + str(d.get("valid")),
          "attempt=" + str(d.get("attempt")))
print("total:", len(runs))
by_status = {}
for r in runs:
    by_status[r.get("status")] = by_status.get(r.get("status"), 0) + 1
print("by_status:", by_status)

print("\n=== NEW event logs (mtime > 18:46, 3rd run only) ===")
import datetime
all_logs = glob.glob("eval/outputs/event_logs/*.jsonl")
new_logs = []
for f in all_logs:
    mt = os.path.getmtime(f)
    if mt > 1891230360:  # approx 18:46 epoch
        new_logs.append((f, mt))
new_logs.sort(key=lambda x: x[1])
print("new event log count:", len(new_logs))
for f, mt in new_logs:
    tname = os.path.basename(f)
    fixture = "pr12257" if "12257" in tname else ("pr12162" if "12162" in tname else "?")
    auth = 0
    tokens = 0
    model = None
    finish = []
    for line in open(f, encoding="utf-8"):
        try:
            d = json.loads(line)
        except Exception:
            continue
        p = d.get("payload", {})
        if not isinstance(p, dict):
            continue
        if p.get("model"):
            model = p["model"]
        et = str(p.get("error_type", ""))
        if "AuthenticationError" in et or p.get("code") == "auth_failed":
            auth += 1
        if p.get("tokens", 0) > 0:
            tokens += p.get("tokens", 0)
        if p.get("model_finish_reason"):
            finish.append(p["model_finish_reason"])
    mtime_str = datetime.datetime.fromtimestamp(mt).strftime("%H:%M:%S")
    print(f"  {mtime_str} {fixture} model={model} auth={auth} tokens={tokens} finish={finish}")
