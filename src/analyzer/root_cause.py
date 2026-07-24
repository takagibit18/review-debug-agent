"""Conservative root-cause blocking, clustering, and independent merge gate."""

from __future__ import annotations

import hashlib
import re
from collections import defaultdict
from enum import Enum
from typing import Any, Iterable

from pydantic import BaseModel, Field

from src.analyzer.context_planner import CandidateContextManifest
from src.analyzer.diff_lines import changed_new_lines_by_file
from src.analyzer.finding_schema import (
    CounterfactualResult,
    EvidenceRole,
    EvidenceProvenance,
    RelatedLocation,
    RepairIntent,
    SourceAnchor,
    normalize_semantic_token,
)
from src.analyzer.location import normalize_location
from src.analyzer.output_formatter import ReviewIssue, ReviewReport, Severity


class CausalityRelation(str, Enum):
    SAME_CAUSAL_MECHANISM = "SAME_CAUSAL_MECHANISM"
    VIOLATES_SAME_INVARIANT = "VIOLATES_SAME_INVARIANT"
    SAME_REPAIR_UNIT = "SAME_REPAIR_UNIT"
    SYMPTOM_OF = "SYMPTOM_OF"
    TRIGGER_OF = "TRIGGER_OF"
    IMPACT_OF = "IMPACT_OF"
    RELATED_BUT_INDEPENDENT = "RELATED_BUT_INDEPENDENT"
    UNCERTAIN = "UNCERTAIN"


class FindingCausalityEdge(BaseModel):
    source: str
    target: str
    kind: CausalityRelation
    rationale: str
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)


class FindingCausalityGraph(BaseModel):
    nodes: dict[str, ReviewIssue] = Field(default_factory=dict)
    edges: list[FindingCausalityEdge] = Field(default_factory=list)

    def add_edge(self, edge: FindingCausalityEdge) -> None:
        key = (edge.source, edge.target, edge.kind)
        if not any((item.source, item.target, item.kind) == key for item in self.edges):
            self.edges.append(edge)


class BlockingSignal(BaseModel):
    left: str
    right: str
    kind: str
    value: str


class FindingBlock(BaseModel):
    block_id: str
    finding_ids: list[str]
    signals: list[BlockingSignal] = Field(default_factory=list)


class BlockingResult(BaseModel):
    blocks: list[FindingBlock] = Field(default_factory=list)
    signal_count: int = Field(default=0, ge=0)
    blocked_finding_count: int = Field(default=0, ge=0)
    average_block_size: float = Field(default=0.0, ge=0.0)


class RootCauseProposal(BaseModel):
    root_cause_id: str
    member_findings: list[str]
    causal_mechanism: str
    violated_invariant: str
    minimal_repair: RepairIntent
    absorbed_roles: dict[str, str] = Field(default_factory=dict)
    counterfactual_result: CounterfactualResult
    primary_member: str
    allowed_context_manifest_ids: list[str] = Field(default_factory=list)


class ConsolidationVerification(BaseModel):
    root_cause_id: str
    accepted: bool
    reasons: list[str] = Field(default_factory=list)


class MergeRejection(BaseModel):
    member_findings: list[str]
    reasons: list[str]


class ConsolidationMetrics(BaseModel):
    input_verified_findings: int = Field(default=0, ge=0)
    block_count: int = Field(default=0, ge=0)
    average_block_size: float = Field(default=0.0, ge=0.0)
    proposal_count: int = Field(default=0, ge=0)
    accepted_cluster_count: int = Field(default=0, ge=0)
    rejected_cluster_count: int = Field(default=0, ge=0)
    merged_member_count: int = Field(default=0, ge=0)
    final_root_cause_count: int = Field(default=0, ge=0)
    finding_inflation_ratio: float = Field(default=0.0, ge=0.0)


class ConsolidationResult(BaseModel):
    report: ReviewReport
    blocking: BlockingResult
    causality_graph: FindingCausalityGraph
    proposals: list[RootCauseProposal] = Field(default_factory=list)
    verifications: list[ConsolidationVerification] = Field(default_factory=list)
    rejections: list[MergeRejection] = Field(default_factory=list)
    metrics: ConsolidationMetrics = Field(default_factory=ConsolidationMetrics)


class FindingBlocker:
    """Deterministically block by repair/state signals, never file/text alone."""

    def __init__(self, *, max_block_size: int = 16, small_batch_size: int = 4) -> None:
        self.max_block_size = max(2, max_block_size)
        self.small_batch_size = max(1, small_batch_size)

    def build_blocks(self, findings: list[ReviewIssue]) -> BlockingResult:
        ids = [_finding_id(item) for item in findings]
        if not ids:
            return BlockingResult()
        signals: list[BlockingSignal] = []
        adjacency: dict[str, set[str]] = {finding_id: set() for finding_id in ids}
        for left_index, left in enumerate(findings):
            for right in findings[left_index + 1 :]:
                pair_signals = self._signals(left, right)
                signals.extend(pair_signals)
                if pair_signals:
                    left_id, right_id = _finding_id(left), _finding_id(right)
                    adjacency[left_id].add(right_id)
                    adjacency[right_id].add(left_id)

        components: list[list[str]] = []
        if len(findings) <= self.small_batch_size:
            components = [ids]
            if len(ids) > 1 and not signals:
                for left_id, right_id in zip(ids, ids[1:]):
                    signals.append(
                        BlockingSignal(
                            left=left_id,
                            right=right_id,
                            kind="small_batch",
                            value="bounded_single_block",
                        )
                    )
        else:
            remaining = set(ids)
            while remaining:
                seed = min(remaining)
                queue = [seed]
                component: list[str] = []
                remaining.remove(seed)
                while queue:
                    current = queue.pop(0)
                    component.append(current)
                    for neighbor in sorted(adjacency[current]):
                        if neighbor in remaining:
                            remaining.remove(neighbor)
                            queue.append(neighbor)
                components.append(component)

        signal_by_component: list[FindingBlock] = []
        for component_index, component in enumerate(components, start=1):
            for chunk_index in range(0, len(component), self.max_block_size):
                chunk = component[chunk_index : chunk_index + self.max_block_size]
                chunk_set = set(chunk)
                signal_by_component.append(
                    FindingBlock(
                        block_id=f"B-{component_index:03d}-{chunk_index // self.max_block_size + 1:02d}",
                        finding_ids=chunk,
                        signals=[
                            signal
                            for signal in signals
                            if signal.left in chunk_set and signal.right in chunk_set
                        ],
                    )
                )
        sizes = [len(block.finding_ids) for block in signal_by_component]
        return BlockingResult(
            blocks=signal_by_component,
            signal_count=len(signals),
            blocked_finding_count=len(ids),
            average_block_size=sum(sizes) / len(sizes) if sizes else 0.0,
        )

    def _signals(self, left: ReviewIssue, right: ReviewIssue) -> list[BlockingSignal]:
        left_id, right_id = _finding_id(left), _finding_id(right)
        output: list[BlockingSignal] = []
        left_symbol = left.primary_anchor.symbol_id if left.primary_anchor else ""
        right_symbol = right.primary_anchor.symbol_id if right.primary_anchor else ""
        if left_symbol and left_symbol == right_symbol:
            output.append(
                BlockingSignal(
                    left=left_id,
                    right=right_id,
                    kind="qualified_symbol",
                    value=left_symbol,
                )
            )

        left_targets = set(left.repair_intent.normalized_targets())
        right_targets = set(right.repair_intent.normalized_targets())
        overlap = left_targets & right_targets
        if overlap:
            output.append(
                BlockingSignal(
                    left=left_id,
                    right=right_id,
                    kind="repair_target_overlap",
                    value=",".join(sorted(overlap)),
                )
            )
        invariant = _invariant_category(left.violated_invariant)
        if (
            invariant != "unknown"
            and invariant == _invariant_category(right.violated_invariant)
            and _repair_scope_overlap(left.repair_intent, right.repair_intent)
        ):
            output.append(
                BlockingSignal(
                    left=left_id,
                    right=right_id,
                    kind="invariant_and_repair_scope",
                    value=invariant,
                )
            )
        evidence_overlap = _evidence_keys(left.cause_evidence) & (
            _evidence_keys(right.trigger_evidence)
            | _evidence_keys(right.impact_evidence)
        )
        reverse_overlap = _evidence_keys(right.cause_evidence) & (
            _evidence_keys(left.trigger_evidence) | _evidence_keys(left.impact_evidence)
        )
        if evidence_overlap or reverse_overlap:
            output.append(
                BlockingSignal(
                    left=left_id,
                    right=right_id,
                    kind="cause_role_evidence_overlap",
                    value=",".join(sorted(evidence_overlap | reverse_overlap)),
                )
            )
        if overlap and _same_class_scope(left_symbol, right_symbol):
            output.append(
                BlockingSignal(
                    left=left_id,
                    right=right_id,
                    kind="same_class_shared_state",
                    value=",".join(sorted(overlap)),
                )
            )
        return output


class ConsolidationVerifier:
    """Independent fail-closed gate for one proposed cluster."""

    def verify(
        self,
        proposal: RootCauseProposal,
        members: list[ReviewIssue],
        merged: ReviewIssue,
        *,
        diff_text: str = "",
        manifests: dict[str, CandidateContextManifest] | None = None,
    ) -> ConsolidationVerification:
        reasons: list[str] = []
        if len(members) < 2:
            reasons.append("cluster_has_fewer_than_two_members")
        if proposal.counterfactual_result != "yes":
            reasons.append("counterfactual_not_yes")
        if (
            not proposal.causal_mechanism.strip()
            or _mechanism_category(proposal.causal_mechanism) == "unknown"
        ):
            reasons.append("causal_mechanism_not_concrete")
        if (
            not proposal.violated_invariant.strip()
            or _invariant_category(proposal.violated_invariant) == "unknown"
        ):
            reasons.append("violated_invariant_not_concrete")
        if not _repair_complete(proposal.minimal_repair):
            reasons.append("minimal_repair_incomplete")

        for left_index, left in enumerate(members):
            for right in members[left_index + 1 :]:
                compatible, pair_reasons = _merge_compatible(left, right)
                if compatible != "yes":
                    reasons.append(
                        f"member_pair_not_compatible:{_finding_id(left)}:{_finding_id(right)}:{compatible}"
                    )
                if "repair" not in pair_reasons:
                    reasons.append("minimal_repair_does_not_cover_all_members")
        member_mechanisms = {
            _mechanism_category(item.causal_mechanism) for item in members
        }
        if _mechanism_category(proposal.causal_mechanism) not in member_mechanisms:
            reasons.append("proposal_introduces_unverified_mechanism")
        member_invariants = {
            _invariant_category(item.violated_invariant) for item in members
        }
        if _invariant_category(proposal.violated_invariant) not in member_invariants:
            reasons.append("proposal_introduces_unverified_invariant")
        if not _repair_covers(
            proposal.minimal_repair, [item.repair_intent for item in members]
        ):
            reasons.append("minimal_repair_does_not_cover_all_members")

        member_evidence = {
            _evidence_identity(evidence)
            for member in members
            for evidence in member.all_evidence()
        }
        merged_evidence = {
            _evidence_identity(evidence) for evidence in merged.all_evidence()
        }
        if not member_evidence.issubset(merged_evidence):
            reasons.append("merged_finding_lost_member_evidence")
        member_locations = {
            location.location
            for member in members
            for location in _issue_locations(member)
        }
        merged_locations = {location.location for location in _issue_locations(merged)}
        if not member_locations.issubset(merged_locations):
            reasons.append("merged_finding_lost_related_locations")
        if any(item.trigger and item.trigger not in merged.trigger for item in members):
            reasons.append("merged_finding_lost_trigger")
        if any(item.impact and item.impact not in merged.impact for item in members):
            reasons.append("merged_finding_lost_impact")

        changed = changed_new_lines_by_file(diff_text)
        primary = merged.primary_anchor
        if primary is None:
            reasons.append("primary_anchor_missing")
        elif changed and primary.line not in changed.get(primary.file, set()):
            reasons.append("primary_anchor_not_changed_line")

        allowed_contexts = set(proposal.allowed_context_manifest_ids)
        for evidence in merged.all_evidence():
            if (
                evidence.context_manifest_id
                and evidence.context_manifest_id not in allowed_contexts
            ):
                reasons.append("evidence_outside_member_context_union")
            if evidence.edge_kind and (
                evidence.evidence_eligibility != "strong"
                or (evidence.edge_confidence or 0.0) < 0.65
            ):
                reasons.append("low_confidence_edge_used_as_merge_evidence")
            if manifests is not None and evidence.context_manifest_id:
                manifest = manifests.get(evidence.context_manifest_id)
                if (
                    manifest is None
                    or evidence.line is None
                    or not manifest.contains_location(
                        evidence.file, evidence.line, evidence.end_line
                    )
                ):
                    reasons.append("evidence_not_in_manifest")
        return ConsolidationVerification(
            root_cause_id=proposal.root_cause_id,
            accepted=not reasons,
            reasons=sorted(set(reasons)),
        )


class RootCauseConsolidator:
    """Cluster verified findings by mechanism + invariant + minimal repair."""

    def __init__(
        self,
        *,
        max_block_size: int = 16,
        conservative_mode: bool = True,
        extra_retrieval_enabled: bool = False,
        verifier: ConsolidationVerifier | None = None,
    ) -> None:
        self.blocker = FindingBlocker(
            max_block_size=max_block_size,
            small_batch_size=4 if conservative_mode else max_block_size,
        )
        self.conservative_mode = conservative_mode
        self.extra_retrieval_enabled = extra_retrieval_enabled
        self.verifier = verifier or ConsolidationVerifier()

    def consolidate(
        self,
        report: ReviewReport,
        *,
        diff_text: str = "",
        manifests: Iterable[CandidateContextManifest | dict[str, Any]] = (),
    ) -> ConsolidationResult:
        risk = [
            issue
            for issue in report.issues
            if issue.severity in {Severity.CRITICAL, Severity.WARNING}
        ]
        passthrough = [
            issue
            for issue in report.issues
            if issue.severity not in {Severity.CRITICAL, Severity.WARNING}
        ]
        by_id = {_finding_id(issue): issue for issue in risk}
        manifest_values = list(manifests)
        manifest_map = _manifest_map(
            manifest_values,
            allow_extensions=self.extra_retrieval_enabled,
        )
        verifier_manifests = manifest_map if manifest_values else None
        blocking = self.blocker.build_blocks(risk)
        causality = FindingCausalityGraph(nodes=by_id)
        proposals: list[RootCauseProposal] = []
        consumed: set[str] = set()

        for block in blocking.blocks:
            block_findings = [
                by_id[item] for item in block.finding_ids if item in by_id
            ]
            clusters = self._cluster_block(block_findings, causality)
            for cluster in clusters:
                if len(cluster) < 2:
                    continue
                proposal = self._proposal(cluster)
                proposals.append(proposal)

        output_risk: list[ReviewIssue] = []
        verifications: list[ConsolidationVerification] = []
        rejections: list[MergeRejection] = []
        for proposal in proposals:
            members = [by_id[item] for item in proposal.member_findings]
            merged = _merge_members(proposal, members)
            verdict = self.verifier.verify(
                proposal,
                members,
                merged,
                diff_text=diff_text,
                manifests=verifier_manifests,
            )
            verifications.append(verdict)
            if verdict.accepted:
                output_risk.append(merged)
                consumed.update(proposal.member_findings)
                self._add_role_edges(causality, proposal)
            else:
                rejections.append(
                    MergeRejection(
                        member_findings=proposal.member_findings,
                        reasons=verdict.reasons,
                    )
                )

        for issue in risk:
            finding_id = _finding_id(issue)
            if finding_id in consumed:
                continue
            rejection_reasons = [
                reason
                for rejection in rejections
                if finding_id in rejection.member_findings
                for reason in rejection.reasons
            ]
            output_risk.append(
                issue.model_copy(
                    deep=True,
                    update={
                        "root_cause_id": _root_cause_id([finding_id]),
                        "member_findings": [finding_id],
                        "merge_rejection_reasons": sorted(set(rejection_reasons)),
                    },
                )
            )
        output_risk.sort(
            key=lambda issue: (
                _severity_rank(issue.severity),
                issue.location,
                _finding_id(issue),
            )
        )
        final_issues = [*output_risk, *passthrough]
        final_root_causes = len(output_risk)
        metrics = ConsolidationMetrics(
            input_verified_findings=len(risk),
            block_count=len(blocking.blocks),
            average_block_size=blocking.average_block_size,
            proposal_count=len(proposals),
            accepted_cluster_count=sum(item.accepted for item in verifications),
            rejected_cluster_count=sum(not item.accepted for item in verifications),
            merged_member_count=len(consumed),
            final_root_cause_count=final_root_causes,
            finding_inflation_ratio=(
                len(risk) / final_root_causes if final_root_causes else 0.0
            ),
        )
        return ConsolidationResult(
            report=ReviewReport(
                summary=report.summary,
                issues=final_issues,
                schema_version=report.schema_version,
            ),
            blocking=blocking,
            causality_graph=causality,
            proposals=proposals,
            verifications=verifications,
            rejections=rejections,
            metrics=metrics,
        )

    def _cluster_block(
        self,
        findings: list[ReviewIssue],
        causality: FindingCausalityGraph,
    ) -> list[list[ReviewIssue]]:
        clusters: list[list[ReviewIssue]] = []
        for finding in sorted(findings, key=_finding_id):
            compatible_clusters: list[tuple[int, list[ReviewIssue]]] = []
            for cluster in clusters:
                comparisons = [_merge_compatible(finding, member) for member in cluster]
                if all(result == "yes" for result, _ in comparisons):
                    compatible_clusters.append((len(cluster), cluster))
                for member, (result, criteria) in zip(cluster, comparisons):
                    self._record_pair_edges(
                        causality, finding, member, result, criteria
                    )
            if compatible_clusters:
                # Complete-link admission avoids A-B/B-C transitive over-merge.
                target = max(compatible_clusters, key=lambda item: item[0])[1]
                target.append(finding)
            else:
                clusters.append([finding])
        return clusters

    @staticmethod
    def _proposal(cluster: list[ReviewIssue]) -> RootCauseProposal:
        member_ids = sorted(_finding_id(item) for item in cluster)
        primary = _select_primary_member(cluster)
        allowed_contexts = sorted(
            {
                context_id
                for item in cluster
                for context_id in _issue_context_ids(item)
                if context_id
            }
        )
        return RootCauseProposal(
            root_cause_id=_root_cause_id(member_ids),
            member_findings=member_ids,
            causal_mechanism=primary.causal_mechanism,
            violated_invariant=primary.violated_invariant,
            minimal_repair=primary.repair_intent.model_copy(deep=True),
            absorbed_roles={
                _finding_id(item): _absorbed_role(item, primary)
                for item in cluster
                if _finding_id(item) != _finding_id(primary)
            },
            counterfactual_result="yes",
            primary_member=_finding_id(primary),
            allowed_context_manifest_ids=allowed_contexts,
        )

    @staticmethod
    def _record_pair_edges(
        graph: FindingCausalityGraph,
        left: ReviewIssue,
        right: ReviewIssue,
        result: CounterfactualResult,
        criteria: set[str],
    ) -> None:
        left_id, right_id = _finding_id(left), _finding_id(right)
        if "mechanism" in criteria:
            graph.add_edge(
                FindingCausalityEdge(
                    source=left_id,
                    target=right_id,
                    kind=CausalityRelation.SAME_CAUSAL_MECHANISM,
                    rationale="normalized causal mechanism matches",
                )
            )
        if "invariant" in criteria:
            graph.add_edge(
                FindingCausalityEdge(
                    source=left_id,
                    target=right_id,
                    kind=CausalityRelation.VIOLATES_SAME_INVARIANT,
                    rationale="specific invariant category matches",
                )
            )
        if result == "yes":
            graph.add_edge(
                FindingCausalityEdge(
                    source=left_id,
                    target=right_id,
                    kind=CausalityRelation.SAME_REPAIR_UNIT,
                    rationale="one minimal repair removes both findings",
                )
            )
        elif result == "no":
            graph.add_edge(
                FindingCausalityEdge(
                    source=left_id,
                    target=right_id,
                    kind=CausalityRelation.RELATED_BUT_INDEPENDENT,
                    rationale="different mechanism, invariant, or repair unit",
                )
            )
        else:
            graph.add_edge(
                FindingCausalityEdge(
                    source=left_id,
                    target=right_id,
                    kind=CausalityRelation.UNCERTAIN,
                    rationale="structured merge criteria are incomplete",
                )
            )

    @staticmethod
    def _add_role_edges(
        graph: FindingCausalityGraph, proposal: RootCauseProposal
    ) -> None:
        role_kinds = {
            "trigger": CausalityRelation.TRIGGER_OF,
            "impact": CausalityRelation.IMPACT_OF,
            "symptom": CausalityRelation.SYMPTOM_OF,
        }
        for finding_id, role in proposal.absorbed_roles.items():
            graph.add_edge(
                FindingCausalityEdge(
                    source=finding_id,
                    target=proposal.primary_member,
                    kind=role_kinds.get(role, CausalityRelation.SYMPTOM_OF),
                    rationale=f"absorbed as {role} after cluster-level verification",
                )
            )


def _merge_compatible(
    left: ReviewIssue, right: ReviewIssue
) -> tuple[CounterfactualResult, set[str]]:
    if not left.is_structured_hypothesis or not right.is_structured_hypothesis:
        return "uncertain", set()
    if not _repair_complete(left.repair_intent) or not _repair_complete(
        right.repair_intent
    ):
        return "uncertain", set()
    criteria: set[str] = set()
    if _same_mechanism(left, right):
        criteria.add("mechanism")
    if _same_invariant(left, right):
        criteria.add("invariant")
    if _same_repair(left.repair_intent, right.repair_intent):
        criteria.add("repair")
    if criteria == {"mechanism", "invariant", "repair"}:
        return "yes", criteria
    if (
        any(
            value
            for value in (
                left.causal_mechanism.strip(),
                right.causal_mechanism.strip(),
                left.violated_invariant.strip(),
                right.violated_invariant.strip(),
            )
        )
        and "repair" not in criteria
    ):
        return "no", criteria
    return "uncertain" if len(criteria) >= 2 else "no", criteria


def _same_mechanism(left: ReviewIssue, right: ReviewIssue) -> bool:
    left_category = _mechanism_category(left.causal_mechanism)
    right_category = _mechanism_category(right.causal_mechanism)
    if left_category != "unknown" and left_category == right_category:
        return True
    if (
        normalize_semantic_token(left.causal_mechanism)
        == normalize_semantic_token(right.causal_mechanism)
        and left.causal_mechanism.strip()
    ):
        return True
    return bool(
        _evidence_keys(left.cause_evidence) & _evidence_keys(right.cause_evidence)
        and _same_repair(left.repair_intent, right.repair_intent)
    )


def _same_invariant(left: ReviewIssue, right: ReviewIssue) -> bool:
    left_category = _invariant_category(left.violated_invariant)
    right_category = _invariant_category(right.violated_invariant)
    if left_category != "unknown" and left_category == right_category:
        return True
    return bool(
        left.violated_invariant.strip()
        and normalize_semantic_token(left.violated_invariant)
        == normalize_semantic_token(right.violated_invariant)
    )


def _same_repair(left: RepairIntent, right: RepairIntent) -> bool:
    if not _repair_complete(left) or not _repair_complete(right):
        return False
    if _action_category(left.action) != _action_category(right.action):
        return False
    left_targets, right_targets = (
        set(left.normalized_targets()),
        set(right.normalized_targets()),
    )
    if left_targets != right_targets:
        return False
    return _boundary_category(left.boundary) == _boundary_category(right.boundary)


def _repair_complete(repair: RepairIntent) -> bool:
    return bool(repair.action.strip() and repair.targets and repair.boundary.strip())


def _repair_scope_overlap(left: RepairIntent, right: RepairIntent) -> bool:
    return bool(
        set(left.normalized_targets()) & set(right.normalized_targets())
        or (
            left.boundary.strip()
            and _boundary_category(left.boundary) == _boundary_category(right.boundary)
        )
    )


def _repair_covers(proposal: RepairIntent, repairs: list[RepairIntent]) -> bool:
    return all(_same_repair(proposal, repair) for repair in repairs)


def _mechanism_category(text: str) -> str:
    value = normalize_semantic_token(text)
    words = set(re.findall(r"[a-z0-9]+", value))
    if {"hash", "equality"} & words or ({"hash", "eq"} <= words):
        return "equality_hash_contract"
    if "cache" in words and words & {
        "identity",
        "key",
        "model",
        "language",
        "stale",
        "reuse",
    }:
        return "cache_identity"
    if words & {"download", "network"} and words & {"sync", "synchronous", "blocking"}:
        return "synchronous_network"
    if "default" in words and words & {"language", "value", "locale"}:
        return "default_value_change"
    if words & {"authorization", "permission", "auth"}:
        return "authorization"
    return value if len(words) >= 4 else "unknown"


def _invariant_category(text: str) -> str:
    value = normalize_semantic_token(text)
    words = set(re.findall(r"[a-z0-9]+", value))
    if "hash" in words and words & {"equal", "equality", "equivalent", "eq"}:
        return "equality_hash_contract"
    if "cache" in words and words & {
        "identity",
        "key",
        "model",
        "language",
        "configuration",
    }:
        return "cache_identity_matches_configuration"
    if "default" in words and words & {"language", "value", "compatibility"}:
        return "default_value_contract"
    if words & {"network", "download"} and words & {
        "async",
        "synchronous",
        "blocking",
        "nonblocking",
    }:
        return "nonblocking_io_contract"
    if words & {"authorization", "permission", "access"}:
        return "authorization_contract"
    return value if len(words) >= 4 else "unknown"


def _action_category(text: str) -> str:
    value = normalize_semantic_token(text)
    words = set(re.findall(r"[a-z0-9]+", value))
    if words & {"include", "key", "rekey"} and words & {
        "cache",
        "identity",
        "configuration",
        "model",
        "language",
    }:
        return "change_cache_identity"
    if words & {"invalidate", "clear", "refresh", "rebuild"} and "cache" in words:
        return "invalidate_cache"
    if words & {"align", "implement", "update", "fix"} and words & {
        "hash",
        "equality",
        "eq",
    }:
        return "align_equality_hash"
    return value


def _boundary_category(text: str) -> str:
    value = normalize_semantic_token(text)
    words = set(re.findall(r"[a-z0-9]+", value))
    for category, markers in (
        ("single_class", {"class"}),
        ("single_method", {"method", "function"}),
        ("cache_lifecycle", {"cache", "lifecycle"}),
        ("equality_pair", {"eq", "hash", "equality"}),
    ):
        if words & markers:
            return category
    return value


def _merge_members(
    proposal: RootCauseProposal, members: list[ReviewIssue]
) -> ReviewIssue:
    primary = next(
        (item for item in members if _finding_id(item) == proposal.primary_member),
        members[0],
    )
    anchors = [location for member in members for location in _issue_locations(member)]
    primary_anchor = primary.primary_anchor or anchors[0]
    related: list[RelatedLocation] = []
    seen_locations = {primary_anchor.location}
    for member in members:
        role = proposal.absorbed_roles.get(_finding_id(member), "related")
        location_role: EvidenceRole
        if role == "trigger":
            location_role = "trigger"
        elif role == "impact":
            location_role = "impact"
        else:
            location_role = "related"
        for location in _issue_locations(member):
            if location.location in seen_locations:
                continue
            seen_locations.add(location.location)
            related.append(
                RelatedLocation(
                    file=location.file,
                    line=location.line,
                    end_line=location.end_line,
                    symbol_id=location.symbol_id,
                    role=location_role,
                    description=f"{role} location from {_finding_id(member)}",
                )
            )
    evidence = _unique_evidence(
        evidence for member in members for evidence in member.all_evidence()
    )
    by_role: dict[str, list[EvidenceProvenance]] = defaultdict(list)
    for item in evidence:
        original_role = _evidence_role(item, members)
        by_role[original_role].append(item)
    observed = _join_unique(item.observed_behavior for item in members)
    trigger = _join_unique(item.trigger for item in members)
    impact = _join_unique(item.impact for item in members)
    legacy_evidence = _join_unique(item.evidence for item in members)
    severity = min((item.severity for item in members), key=_severity_rank)
    return ReviewIssue(
        severity=severity,
        location=primary_anchor.location,
        evidence=legacy_evidence,
        suggestion=primary.suggestion,
        confidence=min(item.confidence for item in members),
        candidate_id=primary.candidate_id,
        schema_version="2.0",
        finding_id=proposal.primary_member,
        root_cause_id=proposal.root_cause_id,
        primary_anchor=primary_anchor,
        related_locations=related,
        observed_behavior=observed,
        causal_mechanism=proposal.causal_mechanism,
        violated_invariant=proposal.violated_invariant,
        repair_intent=proposal.minimal_repair,
        trigger=trigger,
        impact=impact,
        cause_evidence=by_role["cause"],
        contract_evidence=by_role["contract"],
        trigger_evidence=by_role["trigger"],
        impact_evidence=by_role["impact"],
        context_manifest_id=primary.context_manifest_id,
        member_findings=proposal.member_findings,
        absorbed_roles=proposal.absorbed_roles,
        counterfactual_result="yes",
    )


def _select_primary_member(members: list[ReviewIssue]) -> ReviewIssue:
    return sorted(
        members,
        key=lambda item: (
            0 if item.cause_evidence else 1,
            0 if item.primary_anchor and item.primary_anchor.symbol_id else 1,
            -len(item.causal_mechanism),
            _finding_id(item),
        ),
    )[0]


def _absorbed_role(item: ReviewIssue, primary: ReviewIssue) -> str:
    if item.trigger and not primary.trigger:
        return "trigger"
    if item.impact and not primary.impact:
        return "impact"
    if item.trigger_evidence and not item.cause_evidence:
        return "trigger"
    if item.impact_evidence and not item.cause_evidence:
        return "impact"
    return "symptom"


def _finding_id(issue: ReviewIssue) -> str:
    if issue.finding_id.strip():
        return issue.finding_id.strip()
    if issue.candidate_id.strip():
        return issue.candidate_id.strip()
    raw = "|".join((issue.location, issue.evidence, issue.suggestion))
    return "F-" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]


def _root_cause_id(member_ids: list[str]) -> str:
    raw = "|".join(sorted(member_ids))
    return "RC-" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:10].upper()


def _issue_locations(issue: ReviewIssue) -> list[SourceAnchor]:
    output: list[SourceAnchor] = []
    if issue.primary_anchor is not None:
        output.append(issue.primary_anchor)
    else:
        parsed = normalize_location(issue.location)
        if parsed.valid and parsed.path and parsed.line is not None:
            output.append(
                SourceAnchor(
                    file=parsed.path,
                    line=parsed.line,
                    end_line=parsed.end_line,
                )
            )
    output.extend(issue.related_locations)
    return output


def _issue_context_ids(issue: ReviewIssue) -> set[str]:
    return {
        value
        for value in [
            issue.context_manifest_id,
            *[evidence.context_manifest_id for evidence in issue.all_evidence()],
        ]
        if value
    }


def _manifest_map(
    manifests: Iterable[CandidateContextManifest | dict[str, Any]],
    *,
    allow_extensions: bool = False,
) -> dict[str, CandidateContextManifest]:
    output: dict[str, CandidateContextManifest] = {}
    for value in manifests:
        try:
            manifest = (
                value
                if isinstance(value, CandidateContextManifest)
                else CandidateContextManifest.model_validate(value)
            )
        except Exception:  # noqa: BLE001
            continue
        if not allow_extensions and (
            manifest.parent_manifest_ids or manifest.retrieval_provenance
        ):
            continue
        output[manifest.candidate_id] = manifest
    return output


def _evidence_keys(items: list[EvidenceProvenance]) -> set[str]:
    output: set[str] = set()
    for item in items:
        if item.context_hash:
            output.add(item.context_hash)
        elif item.file and item.line:
            output.add(f"{item.file}:{item.line}-{item.end_line or item.line}")
    return output


def _evidence_identity(item: EvidenceProvenance) -> tuple[Any, ...]:
    return (
        item.context_manifest_id,
        item.file,
        item.line,
        item.end_line,
        item.context_hash,
        item.edge_kind,
        item.statement,
    )


def _unique_evidence(
    values: Iterable[EvidenceProvenance],
) -> list[EvidenceProvenance]:
    output: list[EvidenceProvenance] = []
    seen: set[tuple[Any, ...]] = set()
    for value in values:
        key = _evidence_identity(value)
        if key in seen:
            continue
        seen.add(key)
        output.append(value.model_copy(deep=True))
    return output


def _evidence_role(evidence: EvidenceProvenance, members: list[ReviewIssue]) -> str:
    identity = _evidence_identity(evidence)
    for member in members:
        for role, values in (
            ("cause", member.cause_evidence),
            ("contract", member.contract_evidence),
            ("trigger", member.trigger_evidence),
            ("impact", member.impact_evidence),
        ):
            if any(_evidence_identity(item) == identity for item in values):
                return role
    return "cause"


def _same_class_scope(left_symbol: str, right_symbol: str) -> bool:
    if not left_symbol or not right_symbol:
        return False
    try:
        left_scope = left_symbol.split("|")[2].split(".")[:-1]
        right_scope = right_symbol.split("|")[2].split(".")[:-1]
    except IndexError:
        return False
    return bool(left_scope and left_scope == right_scope)


def _join_unique(values: Iterable[str]) -> str:
    output: list[str] = []
    for value in values:
        cleaned = value.strip()
        if cleaned and cleaned not in output:
            output.append(cleaned)
    return " | ".join(output)


def _severity_rank(severity: Severity) -> int:
    return {
        Severity.CRITICAL: 0,
        Severity.WARNING: 1,
        Severity.INFO: 2,
        Severity.STYLE: 3,
    }[severity]
