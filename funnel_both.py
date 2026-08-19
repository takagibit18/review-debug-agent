import json

def funnel(run_id, label):
    el = f"eval/outputs/event_logs/golden_deepset-ai_haystack_pr12257_reverse_{run_id}.jsonl"
    print(f"\n{'='*70}\n{label} ({run_id})\n{'='*70}")
    for line in open(el, encoding="utf-8"):
        d = json.loads(line)
        if d.get("event_type") != "finding_verification_completed":
            continue
        p = d.get("payload", {})
        print("raw_verdicts:", json.dumps(p.get("raw_verdicts"), ensure_ascii=False))
        print("deterministic_evidence_checked:", p.get("deterministic_evidence_checked_count"))
        print("deterministic_evidence_passed:", p.get("deterministic_evidence_passed_count"))
        print("deterministic_evidence_rejected:", p.get("deterministic_evidence_rejected_count"))
        print("accepted_count:", p.get("accepted_count"))
        print("verifier_accepted_count:", p.get("verifier_accepted_count"))
        drd = p.get("deterministic_rejection_details", [])
        print(f"deterministic_rejection_details ({len(drd)}):")
        for r in drd:
            print("  ", json.dumps(r, ensure_ascii=False, default=str)[:600])
    # also finding_funnel_completed
    for line in open(el, encoding="utf-8"):
        d = json.loads(line)
        if d.get("event_type") == "finding_funnel_completed":
            p = d.get("payload", {})
            print("\nfinding_funnel_completed:")
            print("  submitted_finding_count:", p.get("submitted_finding_count"))
            print("  deterministic_rejected_count:", p.get("deterministic_rejected_count"))
            print("  final_risk_finding_count:", p.get("final_risk_finding_count"))
            print("  final_effective_issue_count:", p.get("final_effective_issue_count"))

funnel("7c091434-bc77-46e6-a496-74242e226223", "A-agent-search s1")
funnel("f10e36a8-85ca-4748-99dc-b96952ad4633", "B2-graph-hybrid-warm s1")
