"""Connected components on auto-match edges, plus guards against the transitive closure
problem (PROJECT_CONSTITUTION.md #13.1-13.2): A~B and B~C doesn't imply A~C. Chained weak
links can produce over-merged monster clusters -- the most dangerous failure mode (P13:
false merges are the expensive error). A flagged cluster is never silently merged.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

PairKey = tuple[str, str]


class _UnionFind:
    def __init__(self) -> None:
        self._parent: dict[str, str] = {}

    def find(self, x: str) -> str:
        self._parent.setdefault(x, x)
        root = x
        while self._parent[root] != root:
            root = self._parent[root]
        while self._parent[x] != root:
            self._parent[x], x = root, self._parent[x]
        return root

    def union(self, a: str, b: str) -> None:
        root_a, root_b = self.find(a), self.find(b)
        if root_a != root_b:
            self._parent[root_a] = root_b


@dataclass(frozen=True)
class Cluster:
    members: tuple[str, ...]
    scored_pairs: int
    possible_pairs: int
    confidence: float
    flagged: bool
    flag_reasons: tuple[str, ...]


def build_clusters(
    edges: list[PairKey], *, max_cluster_size: int, min_cluster_density: float
) -> list[Cluster]:
    """`edges` must be distinct auto-match pairs (record_key_a, record_key_b). Density guard:
    cluster_confidence = scored_pairs / possible_pairs -- the fraction of a cluster's
    complete graph that's actually backed by an auto-match edge. A cluster held together by
    a thin chain of edges (low confidence) is exactly the transitive-closure failure mode."""
    uf = _UnionFind()
    for a, b in edges:
        uf.union(a, b)

    members_by_root: dict[str, set[str]] = defaultdict(set)
    edge_count_by_root: dict[str, int] = defaultdict(int)
    for a, b in edges:
        root = uf.find(a)
        members_by_root[root].add(a)
        members_by_root[root].add(b)
        edge_count_by_root[root] += 1

    clusters = []
    for root, members in members_by_root.items():
        size = len(members)
        scored_pairs = edge_count_by_root[root]
        possible_pairs = size * (size - 1) // 2
        confidence = scored_pairs / possible_pairs if possible_pairs else 1.0

        reasons = []
        if size > max_cluster_size:
            reasons.append("size")
        if confidence < min_cluster_density:
            reasons.append("density")

        clusters.append(
            Cluster(
                members=tuple(sorted(members)),
                scored_pairs=scored_pairs,
                possible_pairs=possible_pairs,
                confidence=confidence,
                flagged=bool(reasons),
                flag_reasons=tuple(reasons),
            )
        )
    return clusters


def finalize_cluster_membership(clusters: list[Cluster]) -> dict[str, tuple[str, ...]]:
    """record_key -> the full member tuple of its finalized cluster. Flagged clusters are
    never merged (P13: when uncertain, do not merge) -- each member maps to a singleton
    cluster of just itself instead of the full (uncertain) group."""
    membership: dict[str, tuple[str, ...]] = {}
    for cluster in clusters:
        if cluster.flagged:
            for member in cluster.members:
                membership[member] = (member,)
        else:
            for member in cluster.members:
                membership[member] = cluster.members
    return membership
