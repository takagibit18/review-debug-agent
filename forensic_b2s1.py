import json, glob, os

def audit(run_id, label):
    el = f"eval/outputs/event_logs/golden_deepset-ai_haystack_pr12257_reverse_{run_id}.jsonl"
    print(f"\n{'#'*78}\n# {label} (rid={run_id})\n{'#'*78}")
    if not os.path.exists(el):
        print("NO EVENT LOG"); return
    manifests = []
    anchors = None
    graph_tel = None
    tool_calls = []
    ctx_telemetry = []
    for line in open(el, encoding="utf-8"):
        d = json.loads(line)
        t = d.get("event_type", "?")
        p = d.get("payload", {})
        if not isinstance(p, dict): p = {}
        if t == "context_manifest_created":
            manifests.append(p)
        elif t == "changed_anchors_extracted":
            anchors = p
        elif t in ("relation_graph_built", "index_lifecycle"):
            graph_tel = p
        elif t == "tool_call":
            tool_calls.append(p)
        elif t == "context_telemetry":
            ctx_telemetry.append(p)

    print(f"\n=== changed anchors ===")
    print(json.dumps(anchors, ensure_ascii=False, default=str)[:1500] if anchors else "(none)")

    print(f"\n=== graph telemetry (relation_graph_built / index_lifecycle) ===")
    if graph_tel:
        for k in ["node_count","edge_count","changed_anchor_count","manifest_count","manifest_token_cost","context_token_cost","included_graph_path_count","discarded_graph_path_count","parsed_file_count","build_latency_seconds","resolver_mode"]:
            if k in graph_tel: print(f"  {k}: {graph_tel[k]}")
    else:
        print("(none)")

    print(f"\n=== context_manifest_created count: {len(manifests)} ===")
    # Check for Gold spans in manifests
    gold_keywords = ["_dates_are_equal", "_prepare_ordering_comparison", "_ensure_both_dates_naive_or_aware", "filters.py"]
    for i, m in enumerate(manifests):
        blob = json.dumps(m, ensure_ascii=False, default=str)
        is_gold = any(k in blob for k in gold_keywords)
        marker = " *** GOLD ***" if is_gold else ""
        f = m.get("file") or m.get("path") or "?"
        sym = m.get("symbol_id") or m.get("symbol") or "?"
        ln = m.get("line") or m.get("line_range") or m.get("span") or "?"
        role = m.get("role") or m.get("retrieval_source") or "?"
        print(f"  [{i:2d}] file={f} sym={sym} line={ln} role/source={role}{marker}")
        if is_gold:
            print(f"        FULL: {json.dumps(m, ensure_ascii=False, default=str)[:500]}")

    print(f"\n=== Reviewer tool calls ({len(tool_calls)}) ===")
    for tc in tool_calls:
        name = tc.get("tool_name") or tc.get("name") or "?"
        args = tc.get("arguments") or tc.get("args") or {}
        if isinstance(args, str):
            try: args = json.loads(args)
            except: pass
        print(f"  {name}: {json.dumps(args, ensure_ascii=False, default=str)[:200]}")

    print(f"\n=== context_telemetry iterations ({len(ctx_telemetry)}) ===")
    for ct in ctx_telemetry:
        print(f"  iter={ct.get('iteration')} est_prompt_tokens={ct.get('estimated_prompt_tokens')} msg_count={ct.get('message_count')}")
        shapes = ct.get("message_shapes", [])
        for s in shapes:
            comp = s.get("component","?")
            print(f"    [{s.get('index')}] {s.get('role')} chars={s.get('chars')} est_tok={s.get('estimated_tokens')} comp={comp}")

audit("f10e36a8-85ca-4748-99dc-b96952ad4633", "B2-graph-hybrid-warm s1 (#12257)")
