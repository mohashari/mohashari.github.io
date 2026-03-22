---
layout: post
title: "Knowledge Graphs for RAG: Enhancing Retrieval with Graph Context"
date: 2026-03-22 08:00:00 +0700
tags: [rag, knowledge-graph, vector-search, llm, ai-engineering]
description: "How to augment a vector RAG pipeline with a knowledge graph layer to answer multi-hop questions without sacrificing latency."
---

You're building a RAG system for an internal knowledge base. Isolated factual questions work fine—"what's the retention policy for PII data?"—but the moment a user asks something like "which teams are affected by the GDPR changes we made last quarter and what systems do they own?", the system halves. Not because the answer isn't in your corpus. It's there—scattered across a compliance doc, an org chart, and three architecture ADRs. Pure vector search retrieves whichever chunk is most semantically similar to the query. It doesn't *connect* those chunks. The LLM gets partial context, fills in the gap with plausible-sounding nonsense, and your system confidently gives a wrong answer.

This is the multi-hop problem. It's not a prompting issue and it's not a chunking issue. You can't embed your way out of it.

## Why Dense Embeddings Fail at Relational Retrieval

A dense embedding collapses a chunk of text into a single point in vector space. The geometric distance between two points represents semantic similarity—words used in similar contexts land near each other. This works well for synonym matching and paraphrase detection. It breaks down when the relationship between two facts isn't encoded in co-occurrence patterns.

Consider: "Service A calls Service B" and "Service B owns the payments database." The embedding for a query about "what databases does Service A depend on transitively?" will not rank both chunks highly. There's no direct semantic overlap between the query and either document. The answer requires *traversal*—following an edge from A to B, then another edge from B to the database.

This is exactly what a graph is built for. The fix isn't to abandon vector search—it's to layer graph traversal on top of it.

## The Architecture

The system has two phases: index time and query time.

**Index time:**
1. Extract entities and relations from each document chunk using an LLM or a fine-tuned NER/RE model.
2. Upsert entities as nodes and relations as edges into a graph database (Neo4j, Amazon Neptune, or a lighter option like Kuzu for self-hosted).
3. Store the chunk embeddings in your vector store as usual, but add a `chunk_id` to each entity node so you can traverse back to source text.

**Query time:**
1. Run the query through your vector store—get top-K chunks by cosine similarity.
2. For each retrieved chunk, look up the entities it contains in the graph.
3. Traverse N hops from those entities, collecting neighboring nodes and edges.
4. Fetch the source chunks for any newly discovered entities.
5. Merge graph-retrieved chunks with vector-retrieved chunks, deduplicate, score, and pass to the LLM.

The key insight is that the graph doesn't replace retrieval—it *expands* the retrieved context by following edges the vector search couldn't see.

## Entity and Relation Extraction at Index Time

The quality of your graph is entirely determined by extraction quality. Don't underestimate this. Garbage relations in, garbage traversals out.

For a production system, you have two options: a prompted LLM (GPT-4o, Claude Sonnet) or a task-specific model like REBEL or GLiNER for relation extraction. LLMs are slower and more expensive but handle ambiguity better. Task-specific models are faster and cheaper but brittle on domain-specific terminology.

Here's a production-grade extraction prompt that returns structured output:

```python
# snippet-1
import json
from anthropic import Anthropic

client = Anthropic()

EXTRACTION_PROMPT = """Extract all entities and relationships from the following text chunk.

Return a JSON object with this exact schema:
{
  "entities": [
    {"id": "string", "label": "string", "type": "SERVICE|TEAM|DATABASE|POLICY|PERSON|SYSTEM"}
  ],
  "relations": [
    {"source": "entity_id", "target": "entity_id", "type": "string", "properties": {}}
  ]
}

Entity IDs must be stable, lowercase, snake_case identifiers (e.g. "payments_service", "gdpr_policy_v2").
Relation types should be active verbs in SCREAMING_SNAKE_CASE (e.g. DEPENDS_ON, OWNS, IMPLEMENTS, AFFECTS).
Only extract relations that are explicitly stated—do not infer.

Text chunk:
{chunk_text}
"""

def extract_graph_elements(chunk_text: str, chunk_id: str) -> dict:
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        messages=[{
            "role": "user",
            "content": EXTRACTION_PROMPT.format(chunk_text=chunk_text)
        }]
    )
    
    raw = response.content[0].text.strip()
    # Strip markdown code fences if present
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
    
    extracted = json.loads(raw.strip())
    
    # Tag every entity with its source chunk for reverse lookup
    for entity in extracted["entities"]:
        entity["chunk_id"] = chunk_id
    
    return extracted
```

Run this across all chunks during your indexing pipeline. Batch calls to stay within rate limits—Claude Sonnet handles ~200 chunks/minute at concurrency 10 before you hit throttling.

## Loading into Neo4j

```python
# snippet-2
from neo4j import GraphDatabase
from typing import List

class KnowledgeGraphWriter:
    def __init__(self, uri: str, auth: tuple):
        self.driver = GraphDatabase.driver(uri, auth=auth)

    def upsert_extracted_elements(self, elements: dict, chunk_id: str):
        with self.driver.session() as session:
            # Upsert entities — MERGE prevents duplicates across chunks
            for entity in elements["entities"]:
                session.run(
                    """
                    MERGE (e:Entity {id: $id})
                    SET e.label = $label,
                        e.type = $type
                    WITH e
                    MERGE (c:Chunk {id: $chunk_id})
                    MERGE (e)-[:MENTIONED_IN]->(c)
                    """,
                    id=entity["id"],
                    label=entity["label"],
                    type=entity["type"],
                    chunk_id=chunk_id,
                )

            # Upsert relations — both entities must exist first
            for rel in elements["relations"]:
                session.run(
                    f"""
                    MATCH (src:Entity {{id: $source}})
                    MATCH (tgt:Entity {{id: $target}})
                    MERGE (src)-[r:{rel['type']}]->(tgt)
                    SET r += $properties
                    """,
                    source=rel["source"],
                    target=rel["target"],
                    properties=rel.get("properties", {}),
                )

    def close(self):
        self.driver.close()
```

One thing to get right: `MERGE` on entity ID prevents the graph from exploding with duplicates when the same entity appears in 50 chunks. You want one node per real-world entity, with multiple `MENTIONED_IN` edges pointing to every chunk that references it.

## Graph Traversal at Query Time

Once you have vector retrieval results, you expand them via the graph. The traversal depth is a tunable parameter—1 hop is usually safe, 2 hops is useful for multi-hop questions, 3+ hops gets expensive and noisy fast.

```python
# snippet-3
from neo4j import GraphDatabase

class GraphExpander:
    def __init__(self, uri: str, auth: tuple):
        self.driver = GraphDatabase.driver(uri, auth=auth)

    def get_entity_ids_for_chunks(self, chunk_ids: List[str]) -> List[str]:
        with self.driver.session() as session:
            result = session.run(
                """
                MATCH (e:Entity)-[:MENTIONED_IN]->(c:Chunk)
                WHERE c.id IN $chunk_ids
                RETURN DISTINCT e.id AS entity_id
                """,
                chunk_ids=chunk_ids,
            )
            return [r["entity_id"] for r in result]

    def expand_n_hops(
        self, entity_ids: List[str], hops: int = 2
    ) -> dict[str, list]:
        """
        Returns a dict: {chunk_id: [relation_descriptions]}
        Relation descriptions are human-readable strings fed to the LLM as context.
        """
        with self.driver.session() as session:
            result = session.run(
                f"""
                MATCH path = (start:Entity)-[*1..{hops}]-(neighbor:Entity)
                WHERE start.id IN $entity_ids
                AND NOT neighbor.id IN $entity_ids
                WITH neighbor, 
                     [rel in relationships(path) | type(rel)] AS rel_types,
                     [node in nodes(path) | node.label] AS node_labels
                MATCH (neighbor)-[:MENTIONED_IN]->(c:Chunk)
                RETURN DISTINCT c.id AS chunk_id,
                       collect({{
                           path_nodes: node_labels,
                           path_rels: rel_types
                       }})[..5] AS paths
                LIMIT 50
                """,
                entity_ids=entity_ids,
            )
            
            expanded = {}
            for record in result:
                chunk_id = record["chunk_id"]
                paths = record["paths"]
                expanded[chunk_id] = [
                    " -> ".join(
                        f"{n} -[{r}]->"
                        for n, r in zip(p["path_nodes"], p["path_rels"])
                    ) + f" {p['path_nodes'][-1]}"
                    for p in paths
                ]
            
            return expanded
```

The `LIMIT 50` on the Cypher query is intentional. Without it, a highly connected entity like "payments_service" can return thousands of neighboring chunks. You want breadth, not completeness—this is context enrichment, not a full graph traversal.

## Merging and Scoring the Final Context Window

You now have two sets of chunks: vector-retrieved (high semantic similarity) and graph-retrieved (high relational relevance). You need to merge them without blowing up your context window.

```python
# snippet-4
from dataclasses import dataclass
from typing import Optional

@dataclass
class RetrievedChunk:
    chunk_id: str
    text: str
    vector_score: Optional[float]  # None if graph-only retrieval
    graph_paths: list[str]         # Empty if vector-only retrieval

def merge_and_rank_chunks(
    vector_hits: list[dict],          # [{"chunk_id": str, "text": str, "score": float}]
    graph_expansion: dict[str, list], # {chunk_id: [path_descriptions]}
    chunk_store: dict[str, str],      # chunk_id -> text lookup
    max_chunks: int = 12,
    graph_weight: float = 0.4,
) -> list[RetrievedChunk]:
    seen = {}

    # Seed with vector results
    for hit in vector_hits:
        cid = hit["chunk_id"]
        seen[cid] = RetrievedChunk(
            chunk_id=cid,
            text=hit["text"],
            vector_score=hit["score"],
            graph_paths=[],
        )

    # Merge graph-expanded chunks
    for cid, paths in graph_expansion.items():
        if cid in seen:
            seen[cid].graph_paths = paths
        else:
            text = chunk_store.get(cid)
            if text:
                seen[cid] = RetrievedChunk(
                    chunk_id=cid,
                    text=text,
                    vector_score=None,
                    graph_paths=paths,
                )

    def combined_score(chunk: RetrievedChunk) -> float:
        v = chunk.vector_score or 0.0
        # Graph score: 1.0 if any paths, decays with path length
        g = min(1.0, len(chunk.graph_paths) * 0.25) if chunk.graph_paths else 0.0
        return (1 - graph_weight) * v + graph_weight * g

    ranked = sorted(seen.values(), key=combined_score, reverse=True)
    return ranked[:max_chunks]
```

The `graph_weight=0.4` is a starting point. Tune it based on the ratio of multi-hop to single-hop questions in your query distribution. If 80% of queries are simple lookups, drop it to 0.2. If users are consistently asking relational questions, push it to 0.5.

## Formatting Graph Context for the LLM

Raw graph paths are noise unless you surface them in a way the LLM can reason over. Don't just dump chunk text—include the relational paths that justify why each chunk was included.

```python
# snippet-5
def build_context_block(chunks: list[RetrievedChunk]) -> str:
    sections = []
    
    for i, chunk in enumerate(chunks):
        section = f"[Document {i+1}]\n{chunk.text}"
        
        if chunk.graph_paths:
            path_summary = "\n".join(f"  - {p}" for p in chunk.graph_paths[:3])
            section += f"\n\n[Graph context — why this document is relevant]\n{path_summary}"
        
        sections.append(section)
    
    return "\n\n---\n\n".join(sections)
```

The "why this document is relevant" annotation is surprisingly effective. It gives the LLM an explicit signal that two documents are connected, reducing hallucinations on questions that require synthesizing across multiple sources. In our testing on an internal knowledge base with ~40K chunks, adding graph path annotations reduced factual errors on multi-hop questions by ~35% compared to vector-only retrieval with the same chunk budget.

## Handling Graph Freshness

The biggest operational headache with a knowledge graph layer is keeping it in sync with your document store. Documents get updated; entities get renamed; relations change.

The practical approach: treat graph updates as part of your document ingestion pipeline, not a separate process. When a chunk is updated or deleted, re-run extraction and do a full replace (delete old entities/relations tied to that chunk_id, re-upsert the new ones). This is cheap per-chunk but requires your ingestion pipeline to be chunk-aware.

```python
# snippet-6
def reindex_chunk(chunk_id: str, new_text: str, kg_writer: KnowledgeGraphWriter):
    with kg_writer.driver.session() as session:
        # Detach and delete all entities that are ONLY referenced by this chunk
        session.run(
            """
            MATCH (e:Entity)-[:MENTIONED_IN]->(c:Chunk {id: $chunk_id})
            WHERE NOT EXISTS {
                MATCH (e)-[:MENTIONED_IN]->(other:Chunk)
                WHERE other.id <> $chunk_id
            }
            DETACH DELETE e
            """,
            chunk_id=chunk_id,
        )
        # Delete the chunk node itself
        session.run(
            "MATCH (c:Chunk {id: $chunk_id}) DETACH DELETE c",
            chunk_id=chunk_id,
        )

    # Re-extract and re-upsert
    elements = extract_graph_elements(new_text, chunk_id)
    kg_writer.upsert_extracted_elements(elements, chunk_id)
```

The guard condition in the first Cypher query is important. An entity like "payments_service" mentioned in 50 chunks should not be deleted when you reindex chunk 37. Only delete orphaned entities.

## Latency Reality Check

The obvious concern: does adding graph traversal blow up latency? In practice, the vector search portion runs in 10–50ms against a properly indexed store (Pinecone, Weaviate, pgvector with HNSW). The Neo4j traversal for 1–2 hops over a graph with ~100K nodes runs in 5–30ms. The LLM call dominates at 1–5 seconds.

The graph traversal is not the bottleneck. Run the vector search and graph entity lookup in parallel—you can kick off the `get_entity_ids_for_chunks` query the moment you have chunk IDs from the vector store, before you've even fetched chunk text. The hop expansion can run in parallel with chunk text fetching. Total added latency versus vector-only is typically under 50ms.

Where it *does* get expensive: if your graph has highly connected hub nodes and you're doing 3+ hop traversals without the `LIMIT` guard, you'll see query times spike to 500ms–2s. Profile with `EXPLAIN` in Neo4j before going to production.

## When Not to Use This

Knowledge graphs add operational complexity. You're now maintaining two data stores in sync, running extraction as part of indexing, and managing graph schema evolution. This is worth it when:

- Your queries regularly require connecting facts across 2+ documents
- Your domain has a well-defined entity ontology (microservices, compliance frameworks, org charts)
- Hallucinations on multi-hop questions are causing real downstream problems

It's not worth it when:

- Most queries are single-document lookups
- Your corpus is small enough that a large context window (Gemini 1.5 Pro, Claude's 200K context) can just hold everything
- Entity extraction quality is poor because your domain is too ambiguous or unstructured

Start with baseline vector RAG. Add graph context when you have evidence—from user feedback or eval sets—that multi-hop retrieval failures are a significant portion of your error budget. Don't add it preemptively.

The architecture described here isn't new. Microsoft's GraphRAG paper formalized much of this in 2024. What matters for production is the operational layer: reliable extraction, incremental sync, graph traversal latency bounds, and a scoring function that degrades gracefully when the graph has low coverage. Get those right and you have a retrieval system that can actually reason across your knowledge base.
```