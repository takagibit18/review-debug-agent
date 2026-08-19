import json

runs = {}
for line in open("eval/outputs/graph-ab-qwen-2x2-haystack/checkpoint.jsonl", encoding="utf-8"):
    d = json.loads(line)
    if d.get("status") != "measured":
        continue
    key = (d.get("fixture_id"), d.get("variant_id"), d.get("sample_index"))
    runs[key] = d

A = runs.get(("golden_deepset-ai_haystack_pr12257_reverse", "A-agent-search", 1))
B = runs.get(("golden_deepset-ai_haystack_pr12257_reverse", "B2-graph-hybrid-warm", 1))

def extract(d, label):
    if not d:
        print(f"\n{label}: NOT FOUND")
        return None
    rr = d.get("run_record", {})
    r = rr.get("result", {})
    pm = r.get("process_metrics", {})
    sm = r.get("structural_metrics", {})
    st = r.get("stage_timings", {})
    priming = rr.get("lifecycle", {}).get("priming")
    return {
        "label": label,
        "model": pm.get("model"),
        "latency_agent_s": r.get("latency_seconds"),
        "reviewer_latency_s": pm.get("reviewer_latency_seconds"),
        "total_tokens": r.get("total_tokens"),
        "review_iterations": pm.get("review_iterations"),
        "tool_calls": pm.get("tool_call_count"),
        "read_file": pm.get("read_file_calls"),
        "grep": pm.get("grep_calls"),
        "symbol_lookup": pm.get("symbol_lookup_calls"),
        "draft_findings": pm.get("draft_findings_created"),
        "model_raw_issue_count": pm.get("model_raw_issue_count"),
        "expected_count": r.get("expected_count"),
        "actual_count": r.get("actual_count"),
        "matched_count": r.get("matched_count"),
        "false_positive": r.get("false_positive_count"),
        "matched_root_cause": r.get("matched_root_cause_count"),
        "repair_unit_matched": r.get("repair_unit_matched_count"),
        "final_finding_count": r.get("final_finding_count"),
        "budget_exhausted": r.get("budget_exhausted"),
        "finish_reasons": r.get("finish_reasons"),
        "submit_review_seen": r.get("submit_review_seen_any"),
        "graph_cache_mode": r.get("graph_cache_mode"),
        "manifest_count": rr.get("contract", {}).get("actual_manifest_count"),
        "priming_latency_s": priming.get("latency_seconds") if priming else None,
        "priming_node_count": priming.get("telemetry", {}).get("node_count") if priming else None,
        "priming_edge_count": priming.get("telemetry", {}).get("edge_count") if priming else None,
        "struct_cross_file_exp": sm.get("direct_cross_file_expected_count"),
        "struct_cross_file_match": sm.get("direct_cross_file_matched_count"),
        "struct_graph_obs_exp": sm.get("graph_observable_expected_count"),
        "struct_graph_obs_match": sm.get("graph_observable_matched_count"),
        "agent_run_s": st.get("agent_run_seconds"),
    }

a = extract(A, "A-agent-search s1 (#12257)")
b = extract(B, "B2-graph-hybrid-warm s1 (#12257)")

import json as _j
print(_j.dumps({"A": a, "B": b}, ensure_ascii=False, indent=2, default=str))
