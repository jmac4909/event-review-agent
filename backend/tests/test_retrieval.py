import math
from copy import deepcopy
from io import BytesIO

import pytest

from event_review import embeddings
from event_review.embeddings import LocalEncoder
from event_review.retrieval import MAX_CANDIDATES, TEXT_LIMIT, event_text, memory_text, rank_memory


def case(identifier, text, created_at="2026-11-11T12:00:00Z"):
    return {"id": identifier, "reason": text, "created_at": created_at}


def test_lexical_ranking_selects_three_without_changing_source_records():
    candidates = [case("unrelated", "boxes obstruct exit"), case("exact", "moving forklift"),
                  case("partial", "forklift lane"), case("other", "permit missing")]
    original = deepcopy(candidates)
    selected, scores = rank_memory({"title": "Moving forklift"}, candidates)
    assert [item["id"] for item in selected[:2]] == ["exact", "partial"]
    assert len(selected) == sum(item["selected"] for item in scores) == 3
    assert all(item["semantic"] is None for item in scores)
    assert candidates == original


def test_semantics_can_find_a_match_without_shared_words():
    class Encoder:
        def encode(self, texts):
            assert len(texts) == 4
            return [[1, 0], [0, 1], [1, 0], [-1, 0]]

    candidates = [case("lexical", "forklift permit"), case("semantic", "industrial truck"),
                  case("unrelated", "blocked exit")]
    selected, scores = rank_memory({"title": "forklift"}, candidates, Encoder())
    assert [item["id"] for item in selected] == ["semantic", "lexical", "unrelated"]
    assert scores[0]["lexical"] == 0 and scores[0]["semantic"] == 1
    assert scores[0]["combined"] == 0.5


def test_equal_scores_prefer_newer_cases_then_use_a_stable_id_order():
    candidates = [case("a", "forklift"), case("b", "forklift"),
                  case("old", "forklift", "2026-11-10T12:00:00Z")]
    first, _ = rank_memory({"title": "forklift"}, candidates)
    reversed_order, _ = rank_memory({"title": "forklift"}, list(reversed(candidates)))
    assert first == reversed_order
    assert [item["id"] for item in first] == ["b", "a", "old"]


def test_empty_and_excess_candidates_never_reach_the_encoder():
    class NoCalls:
        def encode(self, texts):
            pytest.fail("No embedding call was expected")

    assert rank_memory({}, [], NoCalls()) == ([], [])
    with pytest.raises(ValueError, match="bound"):
        rank_memory({}, [case(str(i), "text") for i in range(MAX_CANDIDATES + 1)], NoCalls())


def test_matching_text_is_bounded_and_excludes_identifiers_and_queue_state():
    event = {"title": "forklift", "description": "x" * 5000, "id": "private-id", "status": "claimed"}
    assert len(event_text(event)) == TEXT_LIMIT
    assert "private-id" not in event_text(event) and "claimed" not in event_text(event)
    assert len(memory_text({"reason": "y" * 2000, "event_snapshot": event})) == TEXT_LIMIT


@pytest.mark.parametrize("present", [False, True])
def test_missing_or_changed_local_files_fail_before_the_library_can_download(tmp_path, monkeypatch, present):
    import model2vec

    def forbidden(*args, **kwargs):
        pytest.fail("Unverified files must never reach from_pretrained")

    monkeypatch.setattr(model2vec.StaticModel, "from_pretrained", forbidden)
    if present:
        (tmp_path / "config.json").write_text("changed")
    with pytest.raises(ValueError, match="missing or changed"):
        LocalEncoder(tmp_path)


def test_setup_rejects_a_download_with_the_wrong_checksum(tmp_path, monkeypatch):
    monkeypatch.setattr(embeddings, "urlopen", lambda *args, **kwargs: BytesIO(b"unexpected content"))
    with pytest.raises(ValueError, match="checksum"):
        embeddings.download_model(tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_actual_pinned_model_ranks_related_wording_without_network_access():
    # The suite blocks outbound sockets. Missing weights are a setup failure,
    # never an excuse to download or silently skip this contract.
    encoder = LocalEncoder()
    selected, scores = rank_memory({"title": "A worker walking close to a moving forklift."}, [
        case("exit", "Boxes obstructed a marked fire exit."),
        case("truck", "A pedestrian stood beside a powered industrial truck."),
        case("barrier", "A guardrail separates the footpath from the forklift lane."),
    ], encoder)
    assert [item["id"] for item in selected] == ["barrier", "truck", "exit"]
    assert scores[1]["semantic"] > scores[2]["semantic"] + 0.1
    vectors = encoder.encode(["forklift", "exit"])
    assert len(vectors[0]) == 256
    assert math.sqrt(sum(value * value for value in vectors[0])) == pytest.approx(1, abs=1e-5)
    with pytest.raises(ValueError, match="64 candidates"):
        encoder.encode(["forklift"] * 66)
