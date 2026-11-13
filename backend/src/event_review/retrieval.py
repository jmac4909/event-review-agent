"""Rank already eligible memory. Similarity never grants trust or changes scope."""

import re


MAX_CANDIDATES = 64
MAX_SELECTED = 3
MAX_GUIDANCE = 16
TEXT_LIMIT = 3000
STOP_WORDS = {"a", "an", "the", "and", "or", "to", "of", "in", "is", "was", "for", "with", "this", "that"}


def words(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", text.casefold())) - STOP_WORDS


def event_text(event: dict) -> str:
    return f"{event.get('title', '')}\n{event.get('description', '')}"[:TEXT_LIMIT]


def memory_text(record: dict) -> str:
    return f"{record.get('reason', record.get('text', ''))}\n{event_text(record.get('event_snapshot', {}))}"[:TEXT_LIMIT]


def rank_memory(event: dict, candidates: list[dict], encoder=None, *, limit: int = MAX_SELECTED) -> tuple[list[dict], list[dict]]:
    if len(candidates) > MAX_CANDIDATES:
        raise ValueError("Filter and bound memory candidates before ranking.")
    if not candidates:
        return [], []
    query = event_text(event)
    texts = [memory_text(item) for item in candidates]
    query_words = words(query)
    vectors = encoder.encode([query, *texts]) if encoder is not None else None
    ranked = []
    for index, (candidate, text) in enumerate(zip(candidates, texts)):
        candidate_words = words(text)
        union = query_words | candidate_words
        lexical = len(query_words & candidate_words) / len(union) if union else 0.0
        semantic = None
        score = lexical
        if vectors is not None:
            # Encoder vectors have unit length, so their dot product is cosine.
            semantic = max(-1.0, min(1.0, sum(a * b for a, b in zip(vectors[0], vectors[index + 1]))))
            score = (lexical + semantic) / 2
        scores = {"lexical": round(lexical, 6), "semantic": round(semantic, 6) if semantic is not None else None,
                  "combined": round(score, 6)}
        ranked.append(candidate | {"match_scores": scores})
    ranked.sort(key=lambda item: (item["match_scores"]["combined"],
                                 str(item.get("created_at", item.get("decided_at", ""))), str(item["id"])), reverse=True)
    audit = [{"id": str(item["id"]), **item["match_scores"], "selected": index < limit}
             for index, item in enumerate(ranked)]
    return ranked[:limit], audit
