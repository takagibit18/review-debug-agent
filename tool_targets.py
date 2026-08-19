import json, glob, os

def get_tool_targets(run_id, label):
    el = f"eval/outputs/event_logs/golden_deepset-ai_haystack_pr12257_reverse_{run_id}.jsonl"
    print(f"\n=== {label} tool_call targets (rid={run_id}) ===")
    if not os.path.exists(el):
        print("NO EVENT LOG"); return
    for line in open(el, encoding="utf-8"):
        d = json.loads(line)
        if d.get("event_type") != "tool_call": continue
        p = d.get("payload", {})
        name = p.get("tool_name") or p.get("name") or "?"
        args = p.get("arguments") or p.get("args") or p.get("input") or {}
        if isinstance(args, str):
            try: args = json.loads(args)
            except: pass
        print(f"  {name}: {json.dumps(args, ensure_ascii=False, default=str)[:250]}")

# B2 s1, B2 s2, A s1, A s2
get_tool_targets("f10e36a8-85ca-4748-99dc-b96952ad4633", "B2 s1")
get_tool_targets("90eaef7f-c488-4356-80ea-eed7d50bad68", "B2 s2")
get_tool_targets("7c091434-bc77-46e6-a496-74242e226223", "A s1")
get_tool_targets("008278ce-f08f-466e-89dc-672e8a0b1752", "A s2")
