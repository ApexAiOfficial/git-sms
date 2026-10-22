"""Operator commands: status, reconcile and resolve for writes whose outcome is not settled."""

import json

import pytest

from git_sms import __main__ as cli

from .conftest import ALICE


@pytest.fixture
def service(make_service, monkeypatch):
    svc = make_service()
    monkeypatch.setattr(cli, "_service", lambda: svc)
    return svc


def unknown_write(service, op_id="op-unknown", action="CreateIssue", number=None):
    key = service.store.sender_key(ALICE)
    service.store.record_intent(op_id, key, action, "acme/field-reports", number, "fp", {})
    service.store.start_operation(op_id)
    service.store.finish_operation(op_id, "unknown")
    return op_id


def test_status_reports_health_and_unsettled_writes_without_identifiers(service, capsys):
    op_id = unknown_write(service)
    assert cli.main(["status"]) == 1  # non-zero while anything is unknown
    out = capsys.readouterr().out
    health = json.loads(out[: out.index("}") + 1])
    assert health["writes_unknown"] == 1
    assert op_id in out and "unknown" in out and "acme/field-reports" in out
    assert ALICE not in out and ALICE[2:] not in out


def test_reconcile_settles_writes_found_by_their_marker(service, api, capsys):
    op_id = unknown_write(service)
    api.add(
        "GET",
        "/repos/acme/field-reports/issues",
        [
            {
                "number": 7,
                "html_url": "https://github.com/acme/field-reports/issues/7",
                "body": f"<!-- git-sms:{op_id} -->",
            }
        ],
    )
    assert cli.main(["reconcile"]) == 0
    assert "proven by marker" in capsys.readouterr().out
    assert service.store.operation(op_id).status == "done"


def test_reconcile_reports_state_changes_as_unproven(service, capsys):
    op_id = unknown_write(service, action="SetState:open", number=12)
    assert cli.main(["reconcile"]) == 0
    assert "observed (target state only" in capsys.readouterr().out
    assert service.store.operation(op_id).status == "observed"  # never "done": nothing proves this write did it
    assert cli.main(["resolve", op_id, "done", "https://github.com/acme/field-reports/issues/12"]) == 0
    assert service.store.operation(op_id).status == "done"


def test_reconcile_leaves_unfound_writes_unknown(service, api, capsys):
    op_id = unknown_write(service)
    api.add("GET", "/repos/acme/field-reports/issues", [])
    assert cli.main(["reconcile"]) == 1
    assert "still unknown" in capsys.readouterr().out
    assert service.store.operation(op_id).status == "unknown"


def test_resolve_records_an_operator_verdict(service, capsys):
    op_id = unknown_write(service)
    with pytest.raises(SystemExit):
        cli.main(["resolve", op_id, "done"])  # done needs the URL
    assert cli.main(["resolve", op_id, "done", "https://github.com/acme/field-reports/issues/7"]) == 0
    assert service.store.operation(op_id).status == "done"
    assert service.store.audit_log()[0].outcome == "resolved_done"
    assert cli.main(["resolve", op_id, "failed"]) == 2  # already settled
    assert cli.main(["resolve", "no-such-op", "failed"]) == 2


def observed_write(service, op_id="op-observed"):
    key = service.store.sender_key(ALICE)
    service.store.record_intent(op_id, key, "SetState:closed", "acme/field-reports", 12, "fp", {})
    service.store.start_operation(op_id)
    service.store.finish_operation(op_id, "observed", "https://github.com/acme/field-reports/issues/12")
    return op_id


def test_observed_writes_stay_visible_until_an_operator_resolves_them(service, capsys):
    """Final review: an unproven state change must not drop out of operator status."""
    op_id = observed_write(service)
    assert [op.op_id for op in service.store.unresolved_operations()] == [op_id]
    assert service.store.health()["writes_observed_unproven"] == 1

    assert cli.main(["status"]) == 1
    out = capsys.readouterr().out
    assert op_id in out and "observed" in out and "SetState:closed" in out

    assert cli.main(["reconcile"]) == 1  # nothing more to find automatically: it still needs a person
    out = capsys.readouterr().out
    assert f"{op_id}  observed: target state seen, not proven" in out
    assert service.store.operation(op_id).status == "observed"

    assert cli.main(["resolve", op_id, "done", "https://github.com/acme/field-reports/issues/12"]) == 0
    capsys.readouterr()
    assert cli.main(["status"]) == 0
    assert op_id not in capsys.readouterr().out
    assert service.store.health()["writes_observed_unproven"] == 0
