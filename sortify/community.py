"""Louvain community detection.

Hand-rolled rather than pulling in networkx: this project has no scientific
computing dependencies and one function does not justify the first. The
algorithm is the standard two-phase Louvain — local moving to maximise
modularity, then collapse each community into a single node, repeat.

Node iteration is sorted throughout, so results are reproducible. A split the
user cannot reproduce is a split they cannot trust.
"""

from __future__ import annotations


def _degrees(adj: dict) -> dict:
    # Node degree (strength) is just the sum of incident edge weights. A
    # self-loop entry (only present in the aggregated graphs of later passes,
    # never in the caller's input) already represents *twice* the internal
    # edge weight it summarises: the collapse step below builds it by walking
    # every node's neighbour list, and a symmetric adjacency lists each
    # internal edge from both endpoints, so it lands in the self-loop slot
    # twice. Summing the neighbour dict therefore already counts that
    # self-loop weight correctly — adding it again would double it a second
    # time and inflate degree (and so total graph weight m2) at every
    # aggregation level, skewing every modularity-gain comparison after the
    # first collapse.
    return {n: sum(nbrs.values()) for n, nbrs in adj.items()}


def _one_level(adj: dict, resolution: float) -> dict:
    """One pass of local moving. Returns {node: community index}."""
    deg = _degrees(adj)
    m2 = sum(deg.values())
    comm = {n: i for i, n in enumerate(sorted(adj))}
    if m2 == 0:
        return comm
    tot = {}
    for n, c in comm.items():
        tot[c] = tot.get(c, 0.0) + deg[n]

    improved = True
    while improved:
        improved = False
        for n in sorted(adj):
            c_old = comm[n]
            tot[c_old] -= deg[n]
            links: dict[int, float] = {}
            for nb, w in adj[n].items():
                if nb != n:
                    links[comm[nb]] = links.get(comm[nb], 0.0) + w
            best_c = c_old
            best_gain = links.get(c_old, 0.0) - resolution * tot.get(c_old, 0.0) * deg[n] / m2
            for c, w_in in sorted(links.items()):
                gain = w_in - resolution * tot.get(c, 0.0) * deg[n] / m2
                if gain > best_gain + 1e-12:
                    best_c, best_gain = c, gain
            tot[best_c] = tot.get(best_c, 0.0) + deg[n]
            comm[n] = best_c
            if best_c != c_old:
                improved = True
    return comm


def louvain(adj: dict[str, dict[str, float]], resolution: float = 1.0) -> dict[str, int]:
    """Partition a weighted undirected graph into communities.

    `adj` must be symmetric: adj[a][b] == adj[b][a].
    """
    if not adj:
        return {}
    mapping = {n: n for n in adj}
    cur = {n: dict(nbrs) for n, nbrs in adj.items()}

    while True:
        comm = _one_level(cur, resolution)
        if len(set(comm.values())) == len(cur):
            break
        mapping = {orig: comm[node] for orig, node in mapping.items()}
        collapsed: dict = {}
        for n, nbrs in cur.items():
            cn = comm[n]
            row = collapsed.setdefault(cn, {})
            for nb, w in nbrs.items():
                cnb = comm[nb]
                row[cnb] = row.get(cnb, 0.0) + w
        cur = collapsed

    # Relabel to a dense 0..k-1 range in a stable order.
    labels = {c: i for i, c in enumerate(sorted(set(mapping.values()), key=str))}
    return {n: labels[c] for n, c in mapping.items()}
