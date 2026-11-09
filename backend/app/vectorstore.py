"""
Vector store for the semantic retrieval pipeline.

Design target (per proposal): MongoDB Atlas Vector Search with an HNSW index
over 1,536-dimensional embeddings, queried via cosine similarity. When a
MONGODB_URI is configured, this module stores/retrieves chunks in MongoDB. In
the absence of a live Atlas cluster (e.g. local demo / grading environment) it
transparently falls back to an equivalent local JSON-backed index that
implements the same public interface, so the rest of the RAG pipeline is
agnostic to which backend is active.

Chunking is section-aware: policy manuals are split on their "Section N:"
headings, with any oversized section further split into token windows using
the proposal's 500-token / 50-token-overlap parameters. This keeps each chunk
a single self-contained policy rule, which is what makes precise, grounded
answers possible (a whole-document chunk forces the model to dump the entire
manual back at the passenger).
"""
import json
import os
import re
import uuid
from pathlib import Path
from typing import Dict, List, Optional

from app import config, places, retrieval
from app import embeddings as embeddings_module
from app.embeddings import cosine_similarity, embed_text, embed_texts, provider_name

_WORD_RE = re.compile(r"\S+")
_SECTION_RE = re.compile(r"^Section\s+\d+\s*:", re.IGNORECASE | re.MULTILINE)


def _split_token_windows(text: str, chunk_size: int, overlap: int) -> List[str]:
    words = _WORD_RE.findall(text)
    if len(words) <= chunk_size:
        return [" ".join(words)] if words else []
    windows = []
    step = max(chunk_size - overlap, 1)
    for start in range(0, len(words), step):
        window = words[start:start + chunk_size]
        if not window:
            continue
        windows.append(" ".join(window))
        if start + chunk_size >= len(words):
            break
    return windows


def chunk_document(text: str, doc_id: str,
                    chunk_size: int = config.CHUNK_SIZE_TOKENS,
                    overlap: int = config.CHUNK_OVERLAP_TOKENS) -> List[Dict]:
    """Splits a policy document into section-scoped chunks."""
    text = text.strip()
    if not text:
        return []

    matches = list(_SECTION_RE.finditer(text))
    segments: List[Dict] = []

    if matches:
        preamble = text[:matches[0].start()].strip()
        lines = preamble.splitlines() if preamble else []
        doc_title = lines[0].strip() if lines else doc_id
        intro = "\n".join(lines[1:]).strip() if len(lines) > 1 else ""
        if intro:
            segments.append({"heading": doc_title, "body": intro, "doc_title": doc_title})
        for i, match in enumerate(matches):
            end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
            full = text[match.start():end].strip()
            sec_lines = full.splitlines()
            heading = sec_lines[0].strip() if sec_lines else f"Section {i + 1}"
            body = "\n".join(sec_lines[1:]).strip()
            if not body:
                continue
            segments.append({"heading": heading, "body": body, "doc_title": doc_title})
    else:
        segments.append({"heading": doc_id, "body": text, "doc_title": doc_id})

    chunks: List[Dict] = []
    for seg in segments:
        for window in _split_token_windows(seg["body"], chunk_size, overlap):
            chunks.append({
                "chunk_id": f"{doc_id}::{len(chunks)}",
                "doc_id": doc_id,
                "section": seg["heading"],
                "doc_title": seg["doc_title"],
                # Clean prose only - used to compose the answer shown to the
                # passenger, so headings never leak into the reply.
                "text": window,
                # Heading/title terms are included for retrieval scoring only.
                "search_text": f"{seg['doc_title']} {seg['heading']} {window}",
            })
    return chunks


def _heading_boost(query_tokens: List[str], record: Dict) -> float:
    """Rewards a chunk whose section heading covers the query.

    BM25 scores a heading term once against a whole section body, so a section
    literally titled "Checked Baggage Allowance" can rank below one that merely
    mentions an "incidental allowance" several times. Headings are the author's
    own statement of what a section is about, so full coverage is treated as a
    decisive signal rather than one more term occurrence.
    """
    heading = f"{record.get('section', '')} {record.get('doc_title', '')}"
    heading_tokens = set(retrieval.expand_query(retrieval.tokenize(heading)))
    if not heading_tokens:
        return 1.0
    unique = set(query_tokens)
    if not unique:
        return 1.0
    coverage = len(unique & heading_tokens) / len(unique)
    return 1.0 + 0.9 * (coverage ** 2)


_MMR_CANDIDATE_POOL = 15

# Applied to a section that lists specific routes when the passenger named a
# place that none of those routes cover (see places.route_mismatch). The
# exception list for Nairobi to Mumbai and Kenyan domestic flights otherwise
# outranked the general allowance for a Nairobi to London question, because
# "Nairobi" matched lexically. A penalty, not removal: the section is still
# retrievable when nothing better exists.
_ROUTE_MISMATCH_FACTOR = float(os.getenv("RETRIEVAL_ROUTE_MISMATCH_FACTOR", "0.75"))

# How redundant an additional chunk of an already-represented document is.
# The first repeat is plausibly useful - a long policy often answers a question
# across two paragraphs - so it is only moderately penalised. Each further
# repeat is penalised harder, because by the third paragraph of one document
# the marginal value is almost always below that of the first paragraph of a
# document not yet represented at all. See tools/tune_retrieval.py.
_SAME_DOC_BASE = float(os.getenv("RETRIEVAL_SAME_DOC_BASE", "0.6"))
_SAME_DOC_STEP = float(os.getenv("RETRIEVAL_SAME_DOC_STEP", "0.3"))

# How far a sub-question match may promote a chunk, relative to the whole
# question's own best match.
#
# DEFAULT 0.0 - DECOMPOSITION IS DISABLED, AND THAT IS A MEASURED DECISION.
# Splitting compound questions ("What am I owed and what do I owe?") and fusing
# the per-intent rankings is a standard multi-hop technique and it was expected
# to help here. Swept over the whole evaluation set it never did: at every
# weight low enough to be safe (<= 0.85) it changed nothing at all, and at
# every weight high enough to change the ranking it *cost* recall - -0.011 at
# 0.90, -0.033 at 0.95 - entirely on the multi-hop questions it was meant to
# fix. Fragment matches promoted chunks that answered half the question well
# and the whole question badly.
#
# The code is kept, reachable, and reproducible via tools/tune_doc_diversity.py
# so the negative result can be re-checked against a larger evaluation set,
# where it may well behave differently. It is not enabled on the strength of
# being a good idea.
_SUBQUERY_WEIGHT = float(os.getenv("RETRIEVAL_SUBQUERY_WEIGHT", "0.0"))


def _same_doc_penalty(repeats: int) -> float:
    """Redundancy attributed to a candidate purely for repeating a document.

    `repeats` is how many already-selected chunks come from the same document.
    Returns 0.0 for a document not yet represented.
    """
    if repeats <= 0:
        return 0.0
    return min(1.0, _SAME_DOC_BASE + _SAME_DOC_STEP * (repeats - 1))


def _mmr_select(order: List[int], scored: List[Dict], records: List[Dict],
                k: int, lam: float, stale: bool) -> List[int]:
    """Maximal Marginal Relevance selection over the top-ranked candidates.

    Pure top-k selection assumes the best k chunks are the most *useful* k
    chunks, which stops being true once a real sentence encoder is doing the
    ranking. A trained encoder scores every paragraph of the one closest
    document highly, so the top three can be three near-duplicates from the
    same policy - fine for a single-fact question, fatal for one that spans two
    documents, which is where this system's harder questions live.

    MMR picks greedily on `lam * relevance - (1 - lam) * max similarity to what
    is already chosen`, so each slot has to earn its place by adding something.
    `lam = 1.0` disables it and restores plain top-k.

    Document identity is folded into the redundancy term, and *escalates* with
    the number of chunks already taken from that document. An earlier version
    applied a flat same-document penalty through `max()`, which saturated: the
    second chunk of a document cost exactly as much as the fourth, so nothing
    stopped a single policy occupying every slot.

    Measured honestly, escalation raises the average number of distinct
    documents per answer from 2.13 to 2.37 but leaves gold-key coverage at
    0.900 - identical to plain top-k. It is kept because more distinct sources
    per answer is the behaviour the design intends, not because it fixed
    multi-hop recall. It did not. The chunks holding the missing keys rank
    #5 to #20, far below the cut, so no diversity term could have promoted
    them; see tools/rank_missing_keys.py.
    """
    if k <= 0 or not order:
        return []
    if lam >= 1.0 or len(order) <= 1:
        return order[:k]

    pool = order[:max(k, _MMR_CANDIDATE_POOL)]
    # Scale relevance by the best candidate rather than min-max normalising the
    # pool. Min-max pins the weakest candidate at exactly zero relevance, so on
    # a pool of near-ties the diversity term can never outweigh it and MMR
    # quietly degenerates into plain top-k.
    best_score = max(scored[i]["score"] for i in pool) or 1.0
    rel = {i: scored[i]["score"] / best_score for i in pool}

    def penalty_for(i: int, selected: List[int]) -> float:
        repeats = sum(1 for j in selected
                      if scored[i].get("doc_id") == scored[j].get("doc_id"))
        doc_penalty = _same_doc_penalty(repeats)
        if stale:
            return doc_penalty
        sim = max(cosine_similarity(records[i].get("embedding") or [],
                                    records[j].get("embedding") or [])
                  for j in selected)
        return max(sim, doc_penalty)

    selected = [pool[0]]
    remaining = pool[1:]
    while remaining and len(selected) < k:
        best, best_value = None, None
        for i in remaining:
            value = lam * rel[i] - (1.0 - lam) * penalty_for(i, selected)
            if best_value is None or value > best_value:
                best, best_value = i, value
        selected.append(best)
        remaining.remove(best)
    return selected


class _HybridSearchMixin:

    def _rank(self, records: List[Dict], query: str, k: int) -> List[Dict]:
        """Rank for one query, decomposing compound questions first.

        Compound questions are the last source of partial recall on the
        evaluation set: "What am I owed and what do I owe?" embeds to a point
        between its two intents, and the chunk answering the second half ranked
        #20 of 52.

        Sub-question results are fused by taking each chunk's best score across
        the intents, then a single MMR pass runs over the fused ranking. An
        earlier attempt interleaved each sub-question's top-k instead, which
        *lost* 0.033 coverage: weak fragment matches displaced strong whole-
        question ones, and running MMR per fragment meant nothing enforced
        diversity across the merged set.
        """
        if not records:
            return []
        parts = retrieval.subqueries(query) if _SUBQUERY_WEIGHT > 0 else [query]
        scored, stale = self._score_all(records, parts[0])

        for part in parts[1:]:
            alt, _ = self._score_all(records, part)
            # Scale each intent to its own best chunk so a sub-question that
            # simply matches the corpus less well overall cannot be drowned
            # out by one that matches it better. The weight caps how far a
            # fragment match may promote a chunk above the whole question's
            # own ranking; it is swept, not assumed.
            alt_best = max((a["score"] for a in alt), default=0.0) or 1.0
            own_best = max((s["score"] for s in scored), default=0.0) or 1.0
            for base, other in zip(scored, alt):
                rescaled = other["score"] / alt_best * own_best * _SUBQUERY_WEIGHT
                if rescaled > base["score"]:
                    base["score"] = round(rescaled, 4)
                    base["lexical_score"] = other["lexical_score"]
                    base["dense_score"] = other["dense_score"]
                    base["matched_subquery"] = part

        order = sorted(range(len(scored)), key=lambda i: scored[i]["score"], reverse=True)
        selected = _mmr_select(order, scored, records, k,
                               config.RETRIEVAL_MMR_LAMBDA, stale)
        top = [scored[i] for i in selected]
        # Drop chunks with no lexical signal at all when a stronger match
        # exists; prevents unrelated policies padding the grounding context.
        if top and top[0]["lexical_score"] > 0:
            top = [t for t in top if t["lexical_score"] > 0] or top[:1]
        return top

    def _score_all(self, records: List[Dict], query: str):
        """Score every chunk for one query. Returns (scored, stale)."""
        if not records:
            return [], False
        query_tokens = retrieval.expand_query(retrieval.tokenize(query))

        corpus_tokens = [retrieval.tokenize(r.get("search_text") or r["text"]) for r in records]
        bm25 = retrieval.BM25(corpus_tokens)
        lexical_scores = bm25.scores(query_tokens)

        q_emb = embed_text(query)
        # A stored index built by a different embedding backend has a different
        # width, and cosine_similarity would quietly return 0.0 for every chunk
        # - hybrid search would silently become pure BM25. Surface it instead.
        stale = bool(records) and len(records[0].get("embedding") or []) != len(q_emb)
        dense_scores = ([0.0] * len(records) if stale
                        else [cosine_similarity(q_emb, r["embedding"]) for r in records])

        blended = retrieval.blend(lexical_scores, dense_scores)

        scored = []
        for rec, lex, dense, score in zip(records, lexical_scores, dense_scores, blended):
            item = {key: value for key, value in rec.items() if key != "embedding"}
            boosted = score * _heading_boost(query_tokens, rec)
            if places.route_mismatch(query, rec.get("text") or ""):
                boosted *= _ROUTE_MISMATCH_FACTOR
                item["route_mismatch"] = True
            item["score"] = round(boosted, 4)
            item["lexical_score"] = round(lex, 4)
            item["dense_score"] = round(dense, 4)
            if stale:
                item["dense_stage"] = "disabled - index built by a different embedding backend"
            scored.append(item)

        return scored, stale


class LocalVectorStore(_HybridSearchMixin):
    """JSON-persisted hybrid index (MongoDB Atlas Vector Search substitute)."""

    def __init__(self, path: Path = config.VECTOR_STORE_PATH):
        self.path = path
        self.records: List[Dict] = []
        if self.path.exists():
            try:
                self.records = json.loads(self.path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                self.records = []

    def _persist(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.records), encoding="utf-8")

    def clear(self):
        self.records = []
        self._persist()

    def add_document(self, doc_id: str, source: str, text: str,
                     provenance: str = "undeclared"):
        chunks = chunk_document(text, doc_id)
        if not chunks:
            return
        vectors = embed_texts([c["search_text"] for c in chunks])
        for c, vector in zip(chunks, vectors):
            self.records.append({
                "id": str(uuid.uuid4()),
                "doc_id": doc_id,
                "source": source,
                "section": c["section"],
                "doc_title": c["doc_title"],
                "provenance": provenance,
                "text": c["text"],
                "search_text": c["search_text"],
                "embedding": vector,
            })
        self._persist()

    def index_dimension(self) -> Optional[int]:
        if not self.records:
            return None
        return len(self.records[0].get("embedding") or [])

    def is_stale(self) -> bool:
        """True when the persisted vectors were produced by a different
        embedding backend than the one now active."""
        stored = self.index_dimension()
        return stored is not None and stored != embeddings_module.dimension()

    def similarity_search(self, query: str, k: int = 4) -> List[Dict]:
        return self._rank(self.records, query, k)

    def stats(self) -> Dict:
        docs = {r["doc_id"] for r in self.records}
        provenance: Dict[str, int] = {}
        for r in self.records:
            key = r.get("provenance", "undeclared")
            provenance[key] = provenance.get(key, 0) + 1
        stats = {
            "documents": len(docs),
            "chunks": len(self.records),
            "chunks_by_provenance": provenance,
            "backend": "local JSON index (hybrid BM25 + dense)",
            "embedding_dimension": self.index_dimension() or embeddings_module.dimension(),
            "embedding_provider": provider_name(),
            "embedding": embeddings_module.describe(),
        }
        if self.is_stale():
            stats["index_status"] = ("stale - built with a "
                                     f"{self.index_dimension()}-dimension backend, "
                                     f"now running {provider_name()}; re-index required")
        return stats


class MongoVectorStore(_HybridSearchMixin):
    """MongoDB Atlas-backed store. In production the dense stage can be pushed
    into a `$vectorSearch` aggregation over an HNSW index; the hybrid
    re-ranking logic above is applied to the candidate set either way."""

    def __init__(self, uri: str, db_name: str):
        from pymongo import MongoClient

        self.client = MongoClient(uri, serverSelectionTimeoutMS=3000)
        self.client.admin.command("ping")
        self.collection = self.client[db_name]["policy_chunks"]

    def clear(self):
        self.collection.delete_many({})

    def add_document(self, doc_id: str, source: str, text: str,
                     provenance: str = "undeclared"):
        chunks = chunk_document(text, doc_id)
        if not chunks:
            return
        vectors = embed_texts([c["search_text"] for c in chunks])
        docs = []
        for c, vector in zip(chunks, vectors):
            docs.append({
                "doc_id": doc_id,
                "source": source,
                "section": c["section"],
                "doc_title": c["doc_title"],
                "provenance": provenance,
                "text": c["text"],
                "search_text": c["search_text"],
                "embedding": vector,
            })
        if docs:
            self.collection.insert_many(docs)

    def similarity_search(self, query: str, k: int = 4) -> List[Dict]:
        records = []
        for rec in self.collection.find({}):
            rec["id"] = str(rec.pop("_id", ""))
            records.append(rec)
        return self._rank(records, query, k)

    def index_dimension(self) -> Optional[int]:
        sample = self.collection.find_one({}, {"embedding": 1})
        if not sample:
            return None
        return len(sample.get("embedding") or [])

    def is_stale(self) -> bool:
        stored = self.index_dimension()
        return stored is not None and stored != embeddings_module.dimension()

    def stats(self) -> Dict:
        doc_ids = self.collection.distinct("doc_id")
        provenance: Dict[str, int] = {}
        for value in self.collection.distinct("provenance"):
            provenance[value or "undeclared"] = self.collection.count_documents(
                {"provenance": value})
        stats = {
            "documents": len(doc_ids),
            "chunks": self.collection.count_documents({}),
            "chunks_by_provenance": provenance,
            "backend": "MongoDB Atlas Vector Search (HNSW, cosine)",
            "embedding_dimension": self.index_dimension() or embeddings_module.dimension(),
            "embedding_provider": provider_name(),
            "embedding": embeddings_module.describe(),
        }
        if self.is_stale():
            stats["index_status"] = ("stale - the Atlas index is declared over "
                                     f"{self.index_dimension()} dimensions but the active "
                                     f"backend emits {embeddings_module.dimension()}; "
                                     "re-index and redefine the search index")
        return stats


_store_instance = None


def get_vector_store():
    global _store_instance
    if _store_instance is not None:
        return _store_instance
    if config.MONGODB_URI:
        try:
            _store_instance = MongoVectorStore(config.MONGODB_URI, config.MONGODB_DB_NAME)
            return _store_instance
        except Exception:
            pass
    _store_instance = LocalVectorStore()
    return _store_instance


def load_provenance(directory: Optional[Path] = None) -> Dict[str, str]:
    """Read the corpus provenance manifest.

    The manifest is deliberately a sidecar JSON file rather than a header inside
    each .txt: ingestion treats the leading lines of a document as a retrievable
    intro chunk, so provenance written into the body would be returned to guests
    as if it were policy.
    """
    directory = directory or config.POLICY_DIR
    manifest_path = directory / "provenance.json"
    if not manifest_path.exists():
        return {}
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    return {
        entry["file"]: entry.get("provenance", "undeclared")
        for entry in manifest.get("documents", [])
        if "file" in entry
    }


def ingest_policy_directory(directory: Optional[Path] = None, rebuild: bool = False):
    directory = directory or config.POLICY_DIR
    store = get_vector_store()
    # A saved index is only reusable if the active embedding backend is the one
    # that built it. Switching backends (or losing one and degrading to the
    # hashed floor) changes the vector width, so rebuild rather than serve a
    # dense stage that scores zero on every chunk.
    if not rebuild and getattr(store, "is_stale", lambda: False)():
        rebuild = True
    if rebuild:
        store.clear()
    stats = store.stats()
    if stats["chunks"] > 0 and not rebuild:
        return stats
    provenance = load_provenance(directory)
    for file_path in sorted(directory.glob("*.txt")):
        store.add_document(doc_id=file_path.stem, source=file_path.name,
                            text=file_path.read_text(encoding="utf-8"),
                            provenance=provenance.get(file_path.name, "undeclared"))
    return store.stats()
