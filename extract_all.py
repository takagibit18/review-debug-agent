import json, glob, os

# Load all measured runs with their run_ids
runs = {}
for line in open("eval/outputs/graph-ab-qwen-2x2-haystack/checkpoint.jsonl", encoding="utf-8"):
    d = json.loads(line)
    if d.get("status") != "measured":
        continue
    fx = d.get("fixture_id")
    var = d.get("variant_id")
    smp = d.get("sample_index")
    rr = d.get("run_record", {})
    rid = rr.get("run_id")
    r = rr.get("result", {})
    pm = r.get("process_metrics", {})
    st = r.get("stage_timings", {})
    runs[(fx, var, smp)] = {
        "run_id": rid,
        "expected_count": r.get("expected_count"),
        "actual_count": r.get("actual_count"),
        "matched_count": r.get("matched_count"),
        "false_positive": r.get("false_positive_count"),
        "final_finding_count": r.get("final_finding_count"),
        "latency": r.get("latency_seconds"),
        "total_tokens": r.get("total_tokens"),
        "review_iterations": pm.get("review_iterations"),
        "tool_calls": pm.get("tool_call_count"),
        "read_file": pm.get("read_file_calls"),
        "grep": pm.get("grep_calls"),
        "model_raw_issue_count": pm.get("model_raw_issue_count"),
        "draft_findings_created": pm.get("draft_findings_created"),
        "finish_reasons": r.get("finish_reasons"),
        "submit_review_seen": r.get("submit_review_seen_any"),
        "graph_cache_mode": r.get("graph_cache_mode"),
        "manifest_count": rr.get("contract", {}).get("actual_manifest_count"),
    }

# Map run_id -> event log path
elogs = {}
for f in glob.glob("eval/outputs/event_logs/golden_deepset-ai_haystack_pr*.jsonl"):
    rid = os.path.basename(f).split("_reverse_")[1].replace(".jsonl", "")
    elogs[rid] = f

# Map run_id -> journal path
journals = {}
for f in glob.glob("eval/outputs/graph-ab-qwen-2x2-haystack/run_journals/*.jsonl"):
    base = os.path.basename(f)
    # extract run_id (last UUID segment)
    parts = base.split("_")
    rid = parts[-1].replace("_journal.jsonl", "")
    journals[rid] = f

def get_funnel(rid):
    f = elogs.get(rid)
    if not f:
        return None
    out = {}
    for line in open(f, encoding="utf-8"):
        d = json.loads(line)
        if d.get("event_type") == "finding_verification_completed":
            p = d.get("payload", {})
            out["raw_verdicts"] = p.get("raw_verdicts", [])
            out["det_checked"] = p.get("deterministic_evidence_checked_count")
            out["det_passed"] = p.get("deterministic_evidence_passed_count")
            out["det_rejected"] = p.get("deterministic_evidence_rejected_count")
            out["accepted_count"] = p.get("accepted_count")
            out["det_rejection_details"] = p.get("deterministic_rejection_details", [])
        if d.get("event_type") == "finding_funnel_completed":
            p = d.get("payload", {})
            out["final_risk_finding_count"] = p.get("final_risk_finding_count")
            out["final_effective_issue_count"] = p.get("final_effective_issue_count")
            out["det_rejected_funnel"] = p.get("deterministic_rejected_count")
    return out

def get_submit_issues(rid):
    f = journals.get(rid)
    if not f:
        return []
    issues = []
    for line in open(f, encoding="utf-8"):
        d = json.loads(line)
        tc = (d.get("payload", {}) or {}).get("tool_calls", [])
        for call in tc:
            fn = call.get("function", {})
            if "submit" in str(fn.get("name", "")):
                args = fn.get("arguments", "{}")
                if isinstance(args, str):
                    args = json.loads(args)
                for iss in args.get("issues", []):
                    issues.append({
                        "severity": iss.get("severity"),
                        "location": iss.get("location"),
                        "evidence_head": str(iss.get("evidence", ""))[:120],
                    })
    return issues

# Order: fixture, sample, then A before B2
order = []
for fx in ["golden_deepset-ai_haystack_pr12257_reverse", "golden_deepset-ai_haystack_pr12162_reverse"]:
    for smp in [1, 2]:
        for var in ["A-agent-search", "B2-graph-hybrid-warm"]:
            order.append((fx, var, smp))

for k in order:
    if k not in runs:
        continue
    r = runs[k]
    fn = get_funnel(r["run_id"]) or {}
    rv = fn.get("raw_verdicts", [])
    sem_ok = sum(1 for v in rv if v.get("status") == "accepted")
    si = get_submit_issues(r["run_id"])
    fxshort = k[0].replace("golden_deepset-ai_haystack_", "").replace("_reverse", "")
    print(f"--- {fxshort} {k[1]} s{k[2]} (rid={r['run_id'][:8]}) ---")
    print(f"  expected={r['expected_count']} actual={r['actual_count']} matched={r['matched_count']} final_finding={r['final_finding_count']} raw_issues={r['model_raw_issue_count']}")
    print(f"  semantic_accepted={sem_ok}/{len(rv)} det_checked={fn.get('det_checked')} det_rejected={fn.get('det_rejected')} accepted={fn.get('accepted_count')} final_risk={fn.get('final_risk_finding_count')} final_eff={fn.get('final_effective_issue_count')}")
    for rd in fn.get("det_rejection_details", []):
        msg = str(rd.get("message", ""))[:70]
        print(f"    REJECT {rd.get('finding_id')} rule={rd.get('rule')} role={rd.get('evidence_role')} field={rd.get('field')} msg={msg}")
    for i, iss in enumerate(si):
        print(f"  submit[{i}] sev={iss['severity']} loc={iss['location']}")
    print(f"  latency={r['latency']:.1f}s tokens={r['total_tokens']} iter={r['review_iterations']} tools={r['tool_calls']} read={r['read_file']} grep={r['grep']} finish={r['finish_reasons']} manifest={r['manifest_count']}")
