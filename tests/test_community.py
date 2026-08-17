from sortify.community import louvain


def test_empty_graph():
    assert louvain({}) == {}


def test_single_node():
    assert louvain({"a": {}}) == {"a": 0}


def test_two_disconnected_cliques_split():
    adj = {
        "a": {"b": 1.0, "c": 1.0}, "b": {"a": 1.0, "c": 1.0}, "c": {"a": 1.0, "b": 1.0},
        "x": {"y": 1.0, "z": 1.0}, "y": {"x": 1.0, "z": 1.0}, "z": {"x": 1.0, "y": 1.0},
    }
    comm = louvain(adj)
    assert comm["a"] == comm["b"] == comm["c"]
    assert comm["x"] == comm["y"] == comm["z"]
    assert comm["a"] != comm["x"]


def test_weak_bridge_does_not_merge_cliques():
    adj = {
        "a": {"b": 10.0, "c": 10.0}, "b": {"a": 10.0, "c": 10.0},
        "c": {"a": 10.0, "b": 10.0, "x": 0.1},
        "x": {"y": 10.0, "z": 10.0, "c": 0.1},
        "y": {"x": 10.0, "z": 10.0}, "z": {"x": 10.0, "y": 10.0},
    }
    comm = louvain(adj)
    assert comm["a"] == comm["b"] == comm["c"]
    assert comm["x"] == comm["y"] == comm["z"]
    assert comm["a"] != comm["x"]


def test_fully_isolated_nodes_each_get_own_community():
    comm = louvain({"a": {}, "b": {}, "c": {}})
    assert len(set(comm.values())) == 3


def test_deterministic_across_runs():
    adj = {
        "a": {"b": 1.0, "c": 2.0}, "b": {"a": 1.0, "c": 1.0},
        "c": {"a": 2.0, "b": 1.0, "d": 0.5}, "d": {"c": 0.5},
    }
    assert louvain(adj) == louvain(adj)


def test_every_node_gets_a_community():
    adj = {"a": {"b": 1.0}, "b": {"a": 1.0}, "lonely": {}}
    comm = louvain(adj)
    assert set(comm) == {"a", "b", "lonely"}


def test_hierarchical_clusters_require_multiple_aggregation_rounds():
    # Four tight triangles. t0+t1 are bridged strongly, as are t2+t3, so the
    # sensible partition is two super-clusters of six nodes each. Getting
    # there needs at least two real aggregation rounds: pass 1 finds the
    # four triangles (12 nodes -> 4 communities), pass 2 pairs them up on
    # the collapsed graph (4 -> 2), pass 3 finds nothing left to merge and
    # stops. That means the original-node -> community mapping has to
    # compose correctly across two collapses, not just one.
    groups = {
        "t0": ["a0", "a1", "a2"],
        "t1": ["b0", "b1", "b2"],
        "t2": ["c0", "c1", "c2"],
        "t3": ["d0", "d1", "d2"],
    }
    adj: dict[str, dict[str, float]] = {n: {} for nodes in groups.values() for n in nodes}

    def add_edge(u: str, v: str, w: float) -> None:
        adj[u][v] = adj[u].get(v, 0.0) + w
        adj[v][u] = adj[v].get(u, 0.0) + w

    for nodes in groups.values():
        for i in range(3):
            for j in range(i + 1, 3):
                add_edge(nodes[i], nodes[j], 10.0)

    # Strong bridges pairing t0<->t1 and t2<->t3.
    add_edge("a0", "b0", 15.0)
    add_edge("a1", "b1", 15.0)
    add_edge("c0", "d0", 15.0)
    add_edge("c1", "d1", 15.0)
    # A very weak bridge connecting the two super-clusters, too weak to merge them.
    add_edge("a2", "c2", 0.02)

    comm = louvain(adj)

    t0t1 = {comm[n] for nodes in (groups["t0"], groups["t1"]) for n in nodes}
    t2t3 = {comm[n] for nodes in (groups["t2"], groups["t3"]) for n in nodes}
    assert len(t0t1) == 1
    assert len(t2t3) == 1
    assert t0t1 != t2t3
    assert len(set(comm.values())) == 2
