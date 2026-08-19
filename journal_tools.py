import json, glob, os

def journal_tools(run_id, label):
    pat = "eval/outputs/graph-ab-qwen-2x2-haystack/run_journals/*" + run_id + "*.jsonl"
    fs = glob.glob(pat)
    if not fs:
        print("\n=== " + label + ": NO JOURNAL ==="); return
    f = fs[0]
    print("\n=== " + label + " (rid=" + run_id + ") journal tool calls ===")
    for line in open(f, encoding="utf-8"):
        d = json.loads(line)
        tc = (d.get("payload", {}) or {}).get("tool_calls", [])
        for call in tc:
            fn = call.get("function", {})
            name = fn.get("name", "?")
            if name == "submit_review": continue
            args = fn.get("arguments", "{}")
            if isinstance(args, str):
                try: args = json.loads(args)
                except: pass
            print("  " + name + ": " + json.dumps(args, ensure_ascii=False, default=str)[:300])

for rid, lbl in [
    ("f10e36a8-85ca-4748-99dc-b96952ad4633", "B2 s1"),
    ("90eaef7f-c488-4356-80ea-eed7d50bad68", "B2 s2"),
    ("7c091434-bc77-46e6-a496-74242e226223", "A s1"),
    ("008278ce-f08f-466e-89dc-672e8a0b1752", "A s2"),
]:
    journal_tools(rid, lbl)
