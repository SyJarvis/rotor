from sqlalchemy import UniqueConstraint

from rotor.models.request_attempt import RequestAttempt


def test_request_attempt_has_request_ordering_constraint() -> None:
    constraints = {
        constraint.name: tuple(column.name for column in constraint.columns)
        for constraint in RequestAttempt.__table__.constraints
        if isinstance(constraint, UniqueConstraint)
    }

    assert constraints["uq_request_attempt_index"] == (
        "request_id",
        "attempt_index",
    )


def test_request_attempt_contains_diagnostic_fact_columns() -> None:
    columns = set(RequestAttempt.__table__.columns.keys())

    assert {
        "request_id",
        "attempt_index",
        "channel_id",
        "requested_model",
        "provider_model",
        "request_protocol",
        "provider_protocol",
        "outcome",
        "upstream_status",
        "error_category",
        "error_code",
        "retryable",
        "sanitized_error",
        "provider_request_ids",
        "request_origin",
        "agent_run_id",
    }.issubset(columns)
