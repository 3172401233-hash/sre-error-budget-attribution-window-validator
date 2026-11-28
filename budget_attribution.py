"""Offline error-budget attribution validator.

Consumes requests, failures, dependencies, retries, SLO windows,
service ownership and shared root causes; computes unique failure
attribution and per-service budget consumption, plus duplicate
counts and undeterminable parts. Python 3.8.2 compatible.
"""

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple


@dataclass(frozen=True)
class Request:
    request_id: str
    service: str
    timestamp: float
    dependency: Optional[str] = None


@dataclass(frozen=True)
class Failure:
    failure_id: str
    request_id: str
    service: str
    timestamp: float
    dependency: Optional[str] = None
    root_cause_id: Optional[str] = None
    retry_of: Optional[str] = None  # points to another Failure.failure_id


@dataclass(frozen=True)
class SLOWindow:
    service: str
    start: float  # inclusive
    end: float    # exclusive
    slo_target: float  # e.g. 0.999


@dataclass(frozen=True)
class ServiceOwnership:
    service: str
    owner: str


@dataclass(frozen=True)
class SharedRootCause:
    root_cause_id: str
    dependency: str
    services: Tuple[str, ...]


@dataclass
class ServiceWindowResult:
    service: str
    owner: Optional[str]
    window_start: float
    window_end: float
    total_requests: int = 0
    unique_failures: int = 0
    budget_allowed: int = 0
    budget_consumed: int = 0
    budget_remaining: int = 0
    budget_exhausted: bool = False
    failure_ids: List[str] = field(default_factory=list)


@dataclass
class AttributionReport:
    results: List[ServiceWindowResult]
    duplicate_count: int            # collapsed duplicates (retry amplification + shared root cause)
    retry_duplicates: int           # collapsed by retry-chain dedup
    shared_duplicates: int          # cross-service double counting via shared root cause
    undetermined: List[dict]        # failures that cannot be attributed
    shared_root_causes: List[dict]  # shared root cause group details


def _resolve_retry_root(failure, by_id):
    """Follow the retry_of chain to the root failure.

    Returns (root_failure, broken); broken=True when the chain is
    cyclic or references a missing failure.
    """
    seen = set()
    current = failure
    while current.retry_of is not None:
        if current.failure_id in seen:
            return None, True
        seen.add(current.failure_id)
        parent = by_id.get(current.retry_of)
        if parent is None:
            return None, True
        current = parent
    return current, False


def _window_for(windows, service, timestamp):
    for w in windows:
        if w.service == service and w.start <= timestamp < w.end:
            return w
    return None


def attribute_budget(requests, failures, windows, ownership, shared_root_causes):
    owners = {o.service: o.owner for o in ownership}
    shared_by_id = {s.root_cause_id: s for s in shared_root_causes}
    request_by_id = {r.request_id: r for r in requests}
    failure_by_id = {f.failure_id: f for f in failures}

    retry_duplicates = 0
    undetermined = []

    # 1) Retry dedup: resolve every failure to its retry-chain root;
    #    each root counts once.
    canonical = {}
    for f in failures:
        root, broken = _resolve_retry_root(f, failure_by_id)
        if broken:
            undetermined.append({
                "failure_id": f.failure_id,
                "service": f.service,
                "reason": "retry_chain_broken_or_cyclic",
            })
            continue
        if root.failure_id != f.failure_id:
            retry_duplicates += 1
        canonical.setdefault(root.failure_id, root)

    # 2) Attribute to service + window; missing request or no window
    #    coverage -> undetermined.
    attributed = defaultdict(list)  # (service, start, end) -> [Failure]
    for root in canonical.values():
        req = request_by_id.get(root.request_id)
        if req is None:
            undetermined.append({
                "failure_id": root.failure_id,
                "service": root.service,
                "reason": "missing_request",
                "request_id": root.request_id,
            })
            continue
        if req.service != root.service:
            undetermined.append({
                "failure_id": root.failure_id,
                "service": root.service,
                "reason": "request_service_mismatch",
                "request_id": root.request_id,
            })
            continue
        window = _window_for(windows, root.service, root.timestamp)
        if window is None:
            undetermined.append({
                "failure_id": root.failure_id,
                "service": root.service,
                "reason": "outside_all_windows",
                "timestamp": root.timestamp,
            })
            continue
        attributed[(root.service, window.start, window.end)].append(root)

    # 3) Shared root cause: unique failures sharing a root_cause_id
    #    across services; each service consumes budget once, extra
    #    counts are shared duplicates.
    shared_groups = defaultdict(list)
    for flist in attributed.values():
        for f in flist:
            if f.root_cause_id and f.root_cause_id in shared_by_id:
                shared_groups[f.root_cause_id].append(f)

    shared_duplicates = 0
    shared_detail = []
    for rc_id, group in sorted(shared_groups.items()):
        services_hit = sorted({f.service for f in group})
        extra = len(group) - len(services_hit)
        if len(services_hit) > 1:
            # same physical failure counted by multiple services
            extra += len(services_hit) - 1
        shared_duplicates += extra
        rc = shared_by_id[rc_id]
        shared_detail.append({
            "root_cause_id": rc_id,
            "dependency": rc.dependency,
            "services": services_hit,
            "unique_failures": len(group),
            "duplicate_count": extra,
        })

    # 4) Per-service per-window budget consumption.
    results = []
    for w in sorted(windows, key=lambda x: (x.service, x.start)):
        key = (w.service, w.start, w.end)
        flist = attributed.get(key, [])
        total_requests = sum(
            1 for r in requests
            if r.service == w.service and w.start <= r.timestamp < w.end
        )
        allowed = int(total_requests * (1.0 - w.slo_target))
        consumed = len(flist)
        results.append(ServiceWindowResult(
            service=w.service,
            owner=owners.get(w.service),
            window_start=w.start,
            window_end=w.end,
            total_requests=total_requests,
            unique_failures=consumed,
            budget_allowed=allowed,
            budget_consumed=consumed,
            budget_remaining=allowed - consumed,
            budget_exhausted=consumed > allowed,
            failure_ids=[f.failure_id for f in flist],
        ))

    return AttributionReport(
        results=results,
        duplicate_count=retry_duplicates + shared_duplicates,
        retry_duplicates=retry_duplicates,
        shared_duplicates=shared_duplicates,
        undetermined=undetermined,
        shared_root_causes=shared_detail,
    )
