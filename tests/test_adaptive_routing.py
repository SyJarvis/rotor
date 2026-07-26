from types import SimpleNamespace

from rotor.gateway.accounting import AccountingService
from rotor.gateway.routing import RoutingDecision
from rotor.models.routing_decision import RoutingDecisionRecord


class _Session:
    def __init__(self) -> None:
        self.added: list[object] = []

    def add(self, value: object) -> None:
        self.added.append(value)


def test_routing_decision_record_preserves_training_snapshot() -> None:
    db = _Session()
    channel = SimpleNamespace(id=7)
    token = SimpleNamespace(id=3)
    decision = RoutingDecision(
        candidates=[channel],
        strategy="adaptive",
        scores={7: {"score": 0.8123, "observations": 12}},
    )

    AccountingService().record_routing_decision(
        db,
        request_id="req_training",
        token=token,
        model="test-model",
        request_protocol="openai_chat",
        decision=decision,
        required_capabilities={"stream", "function_call"},
        affinity_used=True,
        features={"message_count": 4, "has_tools": True},
    )

    assert len(db.added) == 1
    record = db.added[0]
    assert isinstance(record, RoutingDecisionRecord)
    assert record.candidate_channel_ids == [7]
    assert record.selected_channel_id == 7
    assert record.required_capabilities == ["function_call", "stream"]
    assert record.score_snapshot == {
        "7": {"score": 0.8123, "observations": 12}
    }
    assert record.policy_version == "adaptive-v1"
