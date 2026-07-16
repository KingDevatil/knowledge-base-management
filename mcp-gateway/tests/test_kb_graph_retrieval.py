from src.kb_graph import KnowledgeGraphBuilder


def test_graph_extraction_adds_weighted_tag_and_directory_relations():
    builder = KnowledgeGraphBuilder.__new__(KnowledgeGraphBuilder)
    docs = [
        {"doc_id": "a", "title": "A", "path": "ops", "tags": ["deploy", "prod"]},
        {"doc_id": "b", "title": "B", "path": "ops", "tags": ["deploy", "prod"]},
    ]

    extraction = builder._build_extraction(docs, semantic_threshold=0.0)
    edges = {edge["relation"]: edge for edge in extraction["edges"]}

    assert edges["co_tag"]["shared_tags"] == ["deploy", "prod"]
    assert edges["co_tag"]["weight"] == 0.55
    assert edges["same_directory"]["directory"] == "ops"
    assert edges["same_directory"]["weight"] == 0.35


def test_graph_extraction_preserves_semantic_similarity_as_edge_weight():
    builder = KnowledgeGraphBuilder.__new__(KnowledgeGraphBuilder)
    builder._compute_semantic_edges = lambda docs, threshold: [("a", "b", 0.83)]
    docs = [
        {"doc_id": "a", "title": "A", "path": "one", "tags": []},
        {"doc_id": "b", "title": "B", "path": "two", "tags": []},
    ]

    extraction = builder._build_extraction(docs, semantic_threshold=0.8)

    assert extraction["edges"] == [{
        "source": "a",
        "target": "b",
        "relation": "semantically_similar",
        "confidence": "INFERRED",
        "weight": 0.83,
        "source_file": "kb",
        "similarity": 0.83,
    }]


def test_large_relation_groups_use_sparse_two_hop_graph():
    ids = [f"doc-{index}" for index in range(60)]

    pairs = list(KnowledgeGraphBuilder._relation_pairs(ids, clique_limit=50))

    assert len(pairs) == 59
    assert all(source == "doc-0" for source, _ in pairs)
    assert {target for _, target in pairs} == set(ids[1:])
