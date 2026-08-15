"""Prompt templates and message builders for inference."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from src.analyzer.context_builder import ContextBuilder
from src.analyzer.context_compressor import ContextCompressor
from src.analyzer.context_priority import (
    SUMMARY_LABEL_PREFIX,
    assemble_debug_payload,
    assemble_review_payload,
    build_debug_context_parts,
    build_review_context_parts,
)
from src.analyzer.context_state import ContextState
from src.analyzer.schemas import DebugRequest, ReviewRequest
from src.models.schemas import Message

if TYPE_CHECKING:
    from src.models.client import ModelClient

REVIEW_SEVERITY_CALIBRATION_GUIDANCE = (
    "Severity measures the impact of the behavior; confidence measures how certain the "
    "evidence makes you. Do not downgrade a current correctness, compatibility, or "
    "user-visible regression to info merely because its trigger or affected population is "
    "narrow. Do not inflate confidence merely to cross a policy threshold. "
    "Independently compare the pre-change fallback and compatibility contract against the "
    "new behavior. Author tests and PR intent demonstrate intended behavior, but do not "
    "prove that existing callers retain their prior contract. "
    "Before calling a concern future-only, trace the operands currently compared, wrapper "
    "unwrapping, and keying/grouping call paths, then look for a concrete current counterexample. "
    "Examples: removing a compatibility fallback or comparing a wrapped value directly to "
    "its wrapper can be a warning when current callers get an incorrect result; a pure "
    "optimization with unchanged results is info; a vague risk that depends only on a future "
    "caller or hypothetical extension is not a risk finding. "
)

SYSTEM_PROMPT_REVIEW = (
    "You are a senior code reviewer. Analyze the provided diff/files and return structured, "
    "actionable findings. The final answer must be submitted via the submit_review tool. "
    "Use only these severity values: critical, warning, info, style. "
    "Each issue is a structured finding hypothesis using schema_version 2.0. Include the legacy "
    "severity, location, evidence, suggestion, and confidence fields plus finding_id, primary_anchor, "
    "related_locations, observed_behavior, causal_mechanism, violated_invariant, repair_intent, "
    "trigger, impact, cause_evidence, contract_evidence, trigger_evidence, impact_evidence, and "
    "context_manifest_id. Never invent or emit root_cause_id; only the later consolidator assigns it. "
    "Location must be canonical: path[:line[-end_line]], using repo-relative forward-slash paths. "
    "Do not use free-form natural language for location. Location and primary_anchor identify the "
    "clearest display site and may point to unchanged code when that is where the symptom occurs; "
    "they must agree. At least one cause_evidence entry for every warning or critical finding must "
    "cite a concrete changed diff line or range and explain how this PR introduces, triggers, or "
    "changes the displayed problem. "
    "Concrete bugs, regressions, compatibility breaks, incorrect results, data loss, or "
    "user-visible behavior changes must use warning or critical, never info or style. "
    "The summary must not mention bugs, regressions, breaking changes, compatibility risks, "
    "or user-visible behavior change unless there is a corresponding issue in issues[] "
    "with concrete location and evidence. "
    "For concrete changed-code bugs, regressions, compatibility breaks, or user-visible behavior changes, "
    "use confidence >= 0.85; reserve lower confidence for speculative or non-blocking concerns. "
    "Do not label something critical unless the diff itself clearly supports it. "
    "Check for silent behavior changes: fallback paths, coercion, or exception handling "
    "can make inputs that previously failed, errored, or were rejected quietly succeed. "
    "Pay special attention to cross-type comparison, precision semantics, boundary values, "
    "and error exposure semantics. Tests can show intent, but if users receive changed "
    "behavior without a user-visible warning or explicit documentation, report at least "
    "a warning with concrete changed-line evidence. "
    "First enumerate concrete abnormal observations. Then decide whether each observation is a "
    "trigger, symptom, or impact of the same causal mechanism. Within this candidate call, output "
    "one hypothesis per independent minimal repair unit. A local pre-merge is allowed only when the "
    "observations share the causal mechanism, violated invariant, and repair action+targets+boundary. "
    "If a downstream symptom, cache/keying effect, or behavioral consequence comes from the same "
    "defect, preserve it as role-specific evidence or a related location in that hypothesis. "
    "When uncertain, keep hypotheses separate. Never merge merely because observations are in the "
    "same module, function, call chain, graph community, or use similar wording. "
    "Do not promote consequences of a hypothetical fix into a separate issue; keep them "
    "inside the suggestion unless the current diff already creates that independent risk. "
    "Evidence may cite only code present in the supplied candidate_context_manifests or later successful "
    "tool results. For each role, identify the repository-relative file/span and state what that code "
    "proves. Candidate identity, retrieval source, manifest id, and context hash are system-owned and "
    "will be bound from the runtime context; do not invent provenance metadata. "
    "A graph edge is navigation context, not proof of runtime identity; exploratory/low-confidence edges "
    "cannot alone support a warning. When you need a changed hunk plus surrounding source, prefer get_changed_context. "
    "When you need symbol definitions, references, field initialization, or constructor "
    "assignment evidence, use find_symbol_context. "
    "As soon as a suspected issue has a concrete file and claim, call "
    "record_draft_finding before further exploration. This is a mandatory draft checkpoint: "
    "once a concrete file-level suspicion exists, your next assistant action must include "
    "record_draft_finding; do not continue private analysis of that suspicion before recording it. "
    "Keep that draft minimal: optional "
    "line/symbol plus the claim only. A draft is an investigation hypothesis, not a final "
    "finding; continue gathering evidence and still finish with submit_review. You may "
    "record a draft and request a read/search tool in the same response. "
    "Before submitting warning or critical findings, if tool rounds remain, call "
    "validate_review_draft on candidate findings; treat its result as policy feedback, "
    "not as a replacement for submit_review. "
    "When paths are uncertain, use list_dir first before glob/grep/read_file. "
    "After any Directory/File not found error, validate parent directory first and avoid blind retries. "
    + REVIEW_SEVERITY_CALIBRATION_GUIDANCE
)

_MANIFEST_SCHEMA_REQUIREMENT = "and context_manifest_id. Never invent or emit root_cause_id; only the later consolidator assigns it. "
_GRAPH_EVIDENCE_REQUIREMENT = (
    "Evidence may cite only code present in the supplied candidate_context_manifests or later successful "
    "tool results. For each role, identify the repository-relative file/span and state what that code "
    "proves. Candidate identity, retrieval source, manifest id, and context hash are system-owned and "
    "will be bound from the runtime context; do not invent provenance metadata. "
    "A graph edge is navigation context, not proof of runtime identity; exploratory/low-confidence edges "
    "cannot alone support a warning. "
)

# The common policy is byte-for-byte identical for every A/B variant.  Only the
# strategy-specific suffix below changes.
COMMON_REVIEW_PROMPT = SYSTEM_PROMPT_REVIEW.replace(
    _MANIFEST_SCHEMA_REQUIREMENT,
    "and optional context_manifest_id. Never invent or emit root_cause_id; only the later consolidator assigns it. ",
).replace(_GRAPH_EVIDENCE_REQUIREMENT, "")
AGENT_SEARCH_POLICY = (
    "Context policy: agent_search. No graph or candidate context manifest exists for this run. "
    "Evidence may come from the supplied diff or successful read-only tool calls. "
    "For each evidence role, record the repository-relative file, line/span, and a concrete statement. "
    "The runtime binds candidate_id and retrieval_source; leave manifest-only fields empty and never "
    "invent graph provenance, context_manifest_id, or context_hash. If evidence is insufficient, "
    "continue a targeted read/grep/symbol "
    "search when a tool round remains, otherwise do not submit that finding. "
)
GRAPH_CONTEXT_POLICY = (
    "Context policy: graph_hybrid. Candidate context manifests are first-pass navigation context, not a "
    "complete world model. A graph edge indicates only its named structural relation and is not runtime fact. "
    "Cite the exact visible file/span and what it proves; the runtime binds its real diff, read, symbol, "
    "or manifest provenance, including canonical context_manifest_id and context_hash. Successful "
    "read-only tool results outside a "
    "manifest remain valid independent tool provenance. Low-confidence or exploratory "
    "graph edges cannot alone support warning or critical findings. "
)


def review_prompt_parts(context_mode: str) -> tuple[str, str]:
    """Return the invariant prompt core and the selected context policy."""

    policy = (
        AGENT_SEARCH_POLICY if context_mode == "agent_search" else GRAPH_CONTEXT_POLICY
    )
    return COMMON_REVIEW_PROMPT, policy


def review_system_prompt(context_mode: str) -> str:
    common, policy = review_prompt_parts(context_mode)
    return common + policy


SYSTEM_PROMPT_DEBUG = (
    "You are a senior debugging assistant. Produce structured hypotheses and steps. "
    "When paths are uncertain, use list_dir first before glob/grep/read_file. "
    "After any Directory/File not found error, validate parent directory first and avoid blind retries. "
    "Use canonical step locations: path[:line[-end_line]] whenever location is provided."
)

USER_PREFIX_REVIEW = (
    "Review the payload and call submit_review exactly once with final JSON. "
    "Prioritize concrete bugs/regressions over optimization advice. "
    "Concrete bugs, regressions, compatibility breaks, incorrect results, data loss, or "
    "user-visible behavior changes must use warning or critical, never info or style. "
    "If evidence cannot point to a specific diff snippet, lower the severity instead of forcing a bug claim. "
    "If you mention a bug, regression, compatibility break, or user-visible behavior change in the summary, "
    "also include the same finding as an issue in issues[]. "
    "If that issue is backed by a concrete changed line or hunk, use confidence >= 0.85. "
    "If the payload shows truncated files/context, prioritize path exploration and targeted reads. "
    "Use get_changed_context before broad reads when you need changed hunk context, "
    "find_symbol_context before blind grep when you need symbol relationships, and "
    "record_draft_finding as soon as a concrete file-level suspicion is identified. Once such "
    "a suspicion exists, the next assistant action must include that draft call before further "
    "private analysis of the suspicion. "
    "validate_review_draft before final warning/critical submit_review when another tool round is available. "
    "Do not return plain-text-only final answers. "
    + REVIEW_SEVERITY_CALIBRATION_GUIDANCE
    + "\n"
)
USER_PREFIX_DEBUG = (
    "Return tool calls if needed, then submit_debug with final JSON. "
    "If path is unknown, call list_dir first.\n"
)

FINALIZE_REVIEW_NOTICE = (
    "FINAL CALL — this is your last opportunity to respond. You MUST call submit_review "
    "as your FIRST and ONLY action. Do NOT output any reasoning, analysis, or prose text "
    "before the tool call — go directly to submit_review with the best conclusions you can "
    "derive from the known draft findings and accumulated tool feedback. Do NOT request "
    "any additional tools. The submit_review function arguments must directly contain "
    "top-level summary and issues fields; do not wrap them inside an arguments object. "
    "Treat drafts as hypotheses and submit only those supported by "
    "the retained evidence. "
    "If the accumulated evidence only supports speculative, info/style/design, or "
    "non-blocking suggestions, submit issues: [] with an honest summary. "
    "Concrete bugs, regressions, compatibility breaks, incorrect results, data loss, or "
    "user-visible behavior changes must use warning or critical, never info or style. "
    "The summary must not mention bugs, regressions, breaking changes, compatibility risks, "
    "or user-visible behavior change unless there is a corresponding issue in issues[]. "
    "For concrete changed-code bugs, regressions, compatibility breaks, or user-visible behavior changes, "
    "use confidence >= 0.85; reserve lower confidence for speculative or non-blocking concerns. "
    "Before submitting an empty review, re-check whether any fallback path, coercion, or exception handling "
    "creates a silent behavior change in cross-type comparison, precision, boundary-value, "
    "or error exposure semantics. If users receive changed behavior without a user-visible warning "
    "or explicit documentation, report at least a warning with concrete changed-line evidence. "
    "Before submitting multiple issues, merge findings that share the same independent root cause; "
    "a downstream symptom or cache/keying effect should stay in the same issue. "
    "Do not promote a hypothetical fix side effect into its own warning unless the current diff "
    "already creates that separate risk. "
    "If uncertain, return whatever partial findings are supported by what was already read; "
    "an empty issues list is acceptable with an honest summary. "
    + REVIEW_SEVERITY_CALIBRATION_GUIDANCE
)
FINALIZE_DEBUG_NOTICE = (
    "FINAL CALL — this is your last opportunity to respond. You MUST call submit_debug "
    "as your FIRST and ONLY action. Do NOT output any reasoning, analysis, or prose text "
    "before the tool call — go directly to submit_debug with the best hypotheses and steps "
    "you can derive from the accumulated tool feedback. Do NOT request any additional tools."
)


def build_review_messages(
    request: ReviewRequest,
    context: ContextState,
    diff: str,
    file_contents: dict[str, str],
    *,
    prompt_token_budget: int | None = None,
    context_builder: ContextBuilder | None = None,
    project_structure: str | None = None,
    telemetry_sink: dict[str, Any] | None = None,
) -> list[Message]:
    """Build review-mode messages with optional priority truncation of payload parts."""
    cb = context_builder or ContextBuilder()
    all_parts = build_review_context_parts(
        request, context, diff, file_contents, project_structure
    )
    if prompt_token_budget is not None:
        selected = cb.truncate_context(all_parts, prompt_token_budget)
    else:
        selected = all_parts
    payload = assemble_review_payload(request, context, all_parts, selected)
    _populate_context_telemetry(telemetry_sink, cb, all_parts, selected)
    _populate_planner_telemetry(telemetry_sink, context, selected)
    return [
        Message(role="system", content=review_system_prompt(context.context_mode)),
        Message(
            role="user",
            content=USER_PREFIX_REVIEW + json.dumps(payload, ensure_ascii=True),
        ),
    ]


async def build_review_messages_async(
    request: ReviewRequest,
    context: ContextState,
    diff: str,
    file_contents: dict[str, str],
    *,
    prompt_token_budget: int | None = None,
    context_builder: ContextBuilder | None = None,
    project_structure: str | None = None,
    compressor_model_client: ModelClient | None = None,
    summary_enabled: bool = False,
    summary_max_tokens_per_part: int = 1000,
    summary_model_name: str = "",
    telemetry_sink: dict[str, Any] | None = None,
) -> list[Message]:
    """Build review-mode messages with optional second-layer summary compaction."""
    cb = context_builder or ContextBuilder()
    all_parts = build_review_context_parts(
        request, context, diff, file_contents, project_structure
    )
    if prompt_token_budget is None:
        selected = all_parts
    elif summary_enabled and compressor_model_client is not None:
        selected, _ = await cb.truncate_with_summary(
            all_parts,
            prompt_token_budget,
            compressor=ContextCompressor(compressor_model_client),
            model_name=summary_model_name,
            max_summary_tokens=summary_max_tokens_per_part,
        )
    else:
        selected = cb.truncate_context(all_parts, prompt_token_budget)
    payload = assemble_review_payload(request, context, all_parts, selected)
    _populate_context_telemetry(telemetry_sink, cb, all_parts, selected)
    _populate_planner_telemetry(telemetry_sink, context, selected)
    return [
        Message(role="system", content=review_system_prompt(context.context_mode)),
        Message(
            role="user",
            content=USER_PREFIX_REVIEW + json.dumps(payload, ensure_ascii=True),
        ),
    ]


def build_debug_messages(
    request: DebugRequest,
    context: ContextState,
    error_log: str,
    file_contents: dict[str, str],
    *,
    prompt_token_budget: int | None = None,
    context_builder: ContextBuilder | None = None,
    project_structure: str | None = None,
    telemetry_sink: dict[str, Any] | None = None,
) -> list[Message]:
    """Build debug-mode messages with optional priority truncation of payload parts."""
    cb = context_builder or ContextBuilder()
    all_parts = build_debug_context_parts(
        request, context, error_log, file_contents, project_structure
    )
    if prompt_token_budget is not None:
        selected = cb.truncate_context(all_parts, prompt_token_budget)
    else:
        selected = all_parts
    payload = assemble_debug_payload(request, context, all_parts, selected)
    _populate_context_telemetry(telemetry_sink, cb, all_parts, selected)
    return [
        Message(role="system", content=SYSTEM_PROMPT_DEBUG),
        Message(
            role="user",
            content=USER_PREFIX_DEBUG + json.dumps(payload, ensure_ascii=True),
        ),
    ]


async def build_debug_messages_async(
    request: DebugRequest,
    context: ContextState,
    error_log: str,
    file_contents: dict[str, str],
    *,
    prompt_token_budget: int | None = None,
    context_builder: ContextBuilder | None = None,
    project_structure: str | None = None,
    compressor_model_client: ModelClient | None = None,
    summary_enabled: bool = False,
    summary_max_tokens_per_part: int = 1000,
    summary_model_name: str = "",
    telemetry_sink: dict[str, Any] | None = None,
) -> list[Message]:
    """Build debug-mode messages with optional second-layer summary compaction."""
    cb = context_builder or ContextBuilder()
    all_parts = build_debug_context_parts(
        request, context, error_log, file_contents, project_structure
    )
    if prompt_token_budget is None:
        selected = all_parts
    elif summary_enabled and compressor_model_client is not None:
        selected, _ = await cb.truncate_with_summary(
            all_parts,
            prompt_token_budget,
            compressor=ContextCompressor(compressor_model_client),
            model_name=summary_model_name,
            max_summary_tokens=summary_max_tokens_per_part,
        )
    else:
        selected = cb.truncate_context(all_parts, prompt_token_budget)
    payload = assemble_debug_payload(request, context, all_parts, selected)
    _populate_context_telemetry(telemetry_sink, cb, all_parts, selected)
    return [
        Message(role="system", content=SYSTEM_PROMPT_DEBUG),
        Message(
            role="user",
            content=USER_PREFIX_DEBUG + json.dumps(payload, ensure_ascii=True),
        ),
    ]


def _populate_context_telemetry(
    sink: dict[str, Any] | None,
    builder: ContextBuilder,
    all_parts: list[Any],
    selected: list[Any],
) -> None:
    if sink is None:
        return
    selected_labels = {part.label for part in selected}
    available_labels = {part.label for part in all_parts}
    dropped_count = len(
        [
            label
            for label in available_labels
            if label not in selected_labels
            and f"{SUMMARY_LABEL_PREFIX}{label}" not in selected_labels
        ]
    )
    sink.clear()
    sink.update(
        {
            "available": _measure_context_parts(builder, all_parts),
            "selected": _measure_context_parts(builder, selected),
            "selected_file_complete_lines": {
                str(part.label)[5:]: str(part.content).count("\n")
                for part in selected
                if str(part.label).startswith("file:")
            },
            "dropped_part_count": dropped_count,
            "summarized_part_count": len(
                [
                    part
                    for part in selected
                    if str(part.label).startswith(SUMMARY_LABEL_PREFIX)
                ]
            ),
        }
    )


def _populate_planner_telemetry(
    sink: dict[str, Any] | None,
    context: ContextState,
    selected: list[Any],
) -> None:
    if sink is None:
        return
    manifests = context.candidate_context_manifests
    sink["candidate_context_manifest_count"] = len(manifests)
    selected_manifests = [
        item for item in selected if str(item.label).startswith("manifest:")
    ]
    selected_manifest_paths = [
        item for item in selected if str(item.label).startswith("manifest_path:")
    ]
    sink["candidate_context_manifest_selected_count"] = len(selected_manifests)
    sink["candidate_context_graph_path_selected_count"] = len(selected_manifest_paths)
    sink["candidate_context_prompt_token_cost"] = sum(
        int(item.token_count or 0)
        for item in [*selected_manifests, *selected_manifest_paths]
    )
    sink["candidate_context_token_cost"] = sum(
        int(item.get("token_cost", 0) or 0) for item in manifests
    )
    sink["candidate_context_graph_path_count"] = sum(
        len(item.get("included_graph_paths", []))
        for item in manifests
        if isinstance(item.get("included_graph_paths", []), list)
    )
    sink["candidate_context_discarded_path_count"] = sum(
        len(item.get("discarded_paths", []))
        + len(item.get("excluded_low_confidence_paths", []))
        for item in manifests
        if isinstance(item.get("discarded_paths", []), list)
        and isinstance(item.get("excluded_low_confidence_paths", []), list)
    )


def _measure_context_parts(
    builder: ContextBuilder,
    parts: list[Any],
) -> dict[str, Any]:
    by_kind: dict[str, dict[str, int | float]] = {}
    total_chars = 0
    total_tokens = 0
    for part in parts:
        chars = len(part.content)
        tokens = int(part.token_count or builder.estimate_tokens(part.content))
        kind = _context_part_kind(str(part.label))
        bucket = by_kind.setdefault(kind, {"parts": 0, "chars": 0, "tokens": 0})
        bucket["parts"] = int(bucket["parts"]) + 1
        bucket["chars"] = int(bucket["chars"]) + chars
        bucket["tokens"] = int(bucket["tokens"]) + tokens
        total_chars += chars
        total_tokens += tokens
    for bucket in by_kind.values():
        bucket["char_ratio"] = (
            round(int(bucket["chars"]) / total_chars, 4) if total_chars else 0.0
        )
        bucket["token_ratio"] = (
            round(int(bucket["tokens"]) / total_tokens, 4) if total_tokens else 0.0
        )
    return {
        "parts": len(parts),
        "chars": total_chars,
        "tokens": total_tokens,
        "by_kind": by_kind,
    }


def _context_part_kind(label: str) -> str:
    label = label.removeprefix(SUMMARY_LABEL_PREFIX)
    if label.startswith("diff_hunk_"):
        return "diff_hunk"
    if label.startswith("file:"):
        return "file"
    if label.startswith("manifest_path:"):
        return "manifest_path"
    if label.startswith("manifest:"):
        return "manifest"
    if label in {"meta", "structure", "error_log"}:
        return label
    return "other"
