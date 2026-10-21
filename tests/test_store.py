import os
import sqlite3
import stat
import threading

import pytest
from cryptography.exceptions import InvalidTag

from git_sms.config import ConfigError
from git_sms.store import Store

from .conftest import ALICE, BOB, Clock

PAYLOAD = {"from": ALICE, "to": "+15550100100", "body": "new issue acme/x Secret plans", "num_media": 0}
META = {"action": "CreateIssue", "repo": "acme/x", "number": None, "fingerprint": "fp"}


def make(clock=None, path=":memory:"):
    return Store(path, "s" * 32, clock=clock or Clock())


# Intake queue --------------------------------------------------------------------------


def test_inbound_is_queued_once_and_claimed_in_order():
    store = make()
    a, b = store.sender_key(ALICE), store.sender_key(BOB)
    assert store.enqueue_inbound("SM1", a, PAYLOAD)
    assert not store.enqueue_inbound("SM1", a, PAYLOAD)
    store.enqueue_inbound("SM2", a, {**PAYLOAD, "body": "second"})
    store.enqueue_inbound("SM3", b, {**PAYLOAD, "from": BOB, "body": "bob"})

    first = store.claim_inbound()
    assert (first.message_sid, first.payload["body"]) == ("SM1", PAYLOAD["body"])
    # Alice's next message waits for her first; Bob is not blocked by her.
    assert store.claim_inbound().message_sid == "SM3"
    assert store.claim_inbound() is None
    store.complete_inbound(first, [])
    assert store.claim_inbound().message_sid == "SM2"
    assert store.inbound_status("SM1") == "done"


def test_concurrent_enqueue_admits_exactly_one(tmp_path):
    store = make(path=str(tmp_path / "db.sqlite"))
    results = []
    threads = [
        threading.Thread(target=lambda: results.append(store.enqueue_inbound("SMX", "k", PAYLOAD))) for _ in range(16)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert results.count(True) == 1


def test_concurrent_claims_never_run_one_sender_twice(tmp_path):
    store = make(path=str(tmp_path / "db.sqlite"))
    key = store.sender_key(ALICE)
    for n in range(10):
        store.enqueue_inbound(f"SM{n}", key, PAYLOAD)
    claimed = []
    threads = [threading.Thread(target=lambda: claimed.append(store.claim_inbound())) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert [job.message_sid for job in claimed if job] == ["SM0"]


def test_recover_requeues_interrupted_messages_and_never_resends_in_flight_replies():
    store = make()
    key = store.sender_key(ALICE)
    store.enqueue_inbound("SM1", key, PAYLOAD)
    store.enqueue_inbound("SM2", key, PAYLOAD)
    done = store.claim_inbound()
    store.complete_inbound(done, [({"to": ALICE, "from": "+1", "body": "reply"}, 1)])
    assert store.claim_outbound() is not None  # in flight when the process dies
    assert store.claim_inbound().message_sid == "SM2"  # interrupted while processing

    assert store.recover() == {"requeued": 1, "unknown_sends": 1}
    assert store.claim_inbound().message_sid == "SM2"
    assert store.outbound_for("SM1")[0][0] == "unknown"
    assert store.claim_outbound() is None


def test_completed_messages_drop_their_sealed_payload(tmp_path):
    path = tmp_path / "db.sqlite"
    store = make(path=str(path))
    store.enqueue_inbound("SM1", "k", PAYLOAD)
    store.complete_inbound(store.claim_inbound(), [])
    assert sqlite3.connect(path).execute("SELECT payload FROM inbound").fetchone() == (None,)


# Outbound ledger ------------------------------------------------------------------------


def test_outbound_retry_and_delivery_callbacks(clock):
    store = make(clock)
    store.enqueue_inbound("SM1", "k", PAYLOAD)
    store.complete_inbound(store.claim_inbound(), [({"to": ALICE, "from": "+1", "body": "hi"}, 1)])
    message = store.claim_outbound()
    store.retry_outbound(message.id, 4, "connect timeout")
    assert store.claim_outbound() is None
    clock.advance(5)
    again = store.claim_outbound()
    assert again.attempts == 2 and again.payload["body"] == "hi"
    store.settle_outbound(again.id, "accepted", provider_sid="SMout1")
    assert store.record_delivery("SMout1", "undelivered", "30034") == "applied"
    assert store.outbound_for("SM1") == [("accepted", "SMout1", "twilio 30034", 2, "undelivered")]


# Write journal ---------------------------------------------------------------------------


def test_operations_are_journalled_once(clock):
    store = make(clock)
    assert store.record_intent("op1", "k", "CreateIssue", "acme/x", None, "fp", {"type": "CreateIssue"}) is None
    again = store.record_intent("op1", "k", "CreateIssue", "acme/x", None, "fp", {"type": "CreateIssue"})
    assert again.status == "confirmed"
    assert store.intent("op1") == {"type": "CreateIssue"}
    assert store.start_operation("op1")
    assert not store.start_operation("op1")
    assert store.intent("op1") is None
    store.finish_operation("op1", "done", "https://github.com/acme/x/issues/1")
    assert store.operation("op1").url.endswith("/issues/1")
    assert store.similar_operation("k", "fp", 3600).op_id == "op1"
    assert store.similar_operation("k", "other", 3600) is None
    clock.advance(7200)
    assert store.similar_operation("k", "fp", 3600) is None


def test_op_ids_are_unguessable_and_stable():
    a, b = Store(":memory:", "a" * 32), Store(":memory:", "b" * 32)
    assert a.op_id("SM1") == a.op_id("SM1") != a.op_id("SM2")
    assert a.op_id("SM1") != b.op_id("SM1")
    assert "SM1" not in a.op_id("SM1")


# Limits ---------------------------------------------------------------------------------


def test_fixed_windows_reset_and_refunds_floor_at_zero():
    clock = Clock()
    store = make(clock)
    assert [store.consume("c", 60) for _ in range(3)] == [1, 2, 3]
    assert store.consume("c", 60, amount=5) == 8
    assert store.consume("c", 60, amount=-20) == 0
    clock.advance(60)
    assert store.peek("c", 60) == 0


# Confirmations ---------------------------------------------------------------------------


def test_confirmation_is_atomic_and_single_use(clock):
    store = make(clock)
    assert not store.set_pending("k", "1234", {"type": "CreateIssue"}, 600, **META)
    assert store.confirm_pending("k", "0000", 3, "op1") == ("wrong", None)
    assert store.operation("op1") is None
    assert store.confirm_pending("k", "1234", 3, "op1") == ("ok", {"type": "CreateIssue"})
    assert store.confirm_pending("k", "1234", 3, "op2") == ("none", None)


def test_confirmation_becomes_a_durable_intent_in_the_same_transaction(tmp_path):
    """Second review P1-01: consuming the pending change and journalling it must not be separable."""
    path = str(tmp_path / "db.sqlite")
    store = make(path=path)
    store.set_pending("k", "1234", {"type": "CreateIssue", "title": "T"}, 600, **META)
    store.confirm_pending("k", "1234", 3, "op1")
    store.close()  # the process dies here, before any attempt
    revived = make(path=path)
    op = revived.operation("op1")
    assert (op.status, op.action, op.repo) == ("confirmed", "CreateIssue", "acme/x")
    assert revived.intent("op1") == {"type": "CreateIssue", "title": "T"}
    assert revived.confirm_pending("k", "1234", 3, "op9") == ("none", None)


def test_confirmation_locks_after_max_attempts_and_expires(clock):
    store = make(clock)
    store.set_pending("k", "1234", {"type": "CreateIssue"}, 600, **META)
    assert [store.confirm_pending("k", "0000", 3, "op")[0] for _ in range(3)] == ["wrong", "wrong", "locked"]
    assert store.confirm_pending("k", "1234", 3, "op") == ("none", None)
    store.set_pending("k", "1234", {"type": "CreateIssue"}, 600, **META)
    clock.advance(601)
    assert store.confirm_pending("k", "1234", 3, "op") == ("expired", None)
    assert store.operation("op") is None


def test_concurrent_confirmations_execute_once(tmp_path):
    store = make(path=str(tmp_path / "db.sqlite"))
    store.set_pending("k", "1234", {"type": "CreateIssue"}, 600, **META)
    outcomes = []

    def attempt():
        outcomes.append(store.confirm_pending("k", "1234", 3, f"op-{threading.get_ident()}")[0])

    threads = [threading.Thread(target=attempt) for _ in range(12)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert outcomes.count("ok") == 1 and outcomes.count("none") == 11


def test_replacing_reports_only_live_requests(clock):
    store = make(clock)
    store.set_pending("k", "1111", {"type": "CreateIssue"}, 600, **META)
    assert store.set_pending("k", "2222", {"type": "CreateIssue"}, 600, **META)
    clock.advance(601)
    assert not store.set_pending("k", "3333", {"type": "CreateIssue"}, 600, **META)
    assert store.confirm_pending("k", "2222", 3, "op")[0] == "wrong"


# Context, opt-outs, retention ------------------------------------------------------------


def test_pages_expire(clock):
    store = make(clock)
    store.set_default_repo("k", "acme/docs")
    store.set_pages("k", ["two", "three"], 3600)
    assert store.next_page("k") == "two"
    clock.advance(3601)
    assert store.next_page("k") is None
    assert store.default_repo("k") == "acme/docs"


def test_opt_out_state():
    store = make()
    store.set_opted_out("k", True)
    assert store.is_opted_out("k")
    store.set_opted_out("k", False)
    assert not store.is_opted_out("k")


def test_sweep_applies_retention_but_keeps_unresolved_writes(clock):
    store = make(clock)
    store.enqueue_inbound("SM1", "k", PAYLOAD)
    store.complete_inbound(store.claim_inbound(), [({"to": ALICE, "from": "+1", "body": "x"}, 1)])
    store.settle_outbound(store.claim_outbound().id, "accepted", provider_sid="SMo")
    store.record_intent("done-op", "k", "CreateIssue", "acme/x", None, "fp", {})
    store.finish_operation("done-op", "done", "u")
    store.record_intent("unknown-op", "k", "CreateIssue", "acme/x", None, "fp2", {})
    store.finish_operation("unknown-op", "unknown")
    store.set_pending("k", "1234", {"type": "CreateIssue"}, 600, **META)
    store.set_pages("k", ["p"], 3600)
    store.audit(sender_key="k", action="x", outcome="y")

    clock.advance(31 * 86400)
    removed = store.sweep(retention_days=30, audit_retention_days=365)
    assert {k: removed[k] for k in ("pending", "pages", "inbound", "outbound", "operations", "audit")} == {
        "pending": 1,
        "pages": 1,
        "inbound": 1,
        "outbound": 1,
        "operations": 1,
        "audit": 0,
    }
    assert store.operation("unknown-op") is not None
    assert store.enqueue_inbound("SM2", "k", PAYLOAD)  # the queue still works after a sweep

    clock.advance(366 * 86400)
    removed = store.sweep(retention_days=30, audit_retention_days=365)
    assert removed["audit"] == 1 and store.operation("unknown-op") is None


# At rest -------------------------------------------------------------------------------------


def test_database_file_holds_no_phone_numbers_or_message_text(tmp_path):
    path = tmp_path / "git-sms.db"
    store = make(path=str(path))
    key = store.sender_key(ALICE)
    store.enqueue_inbound("SM1", key, PAYLOAD)  # still queued: sealed, not plaintext
    store.enqueue_inbound("SM2", key, PAYLOAD)
    store.complete_inbound(store.claim_inbound(), [({"to": ALICE, "from": "+1", "body": "Secret plans reply"}, 1)])
    store.set_pending(key, "1234", {"type": "CreateIssue", "repo": "acme/x", "title": "Secret plans"}, 600, **META)
    store.record_intent("op1", key, "CreateIssue", "acme/x", None, "fp", {"title": "Secret plans"})
    store.set_pages(key, ["Secret plans page two"], 3600)
    store.set_default_repo(key, "acme/x")
    store.audit(sender_key=key, action="CreateIssue", outcome="done", alias="alice")
    store.close()
    raw = b"".join(p.read_bytes() for p in tmp_path.iterdir())
    for needle in (ALICE, ALICE[2:], "Secret plans", "1234"):
        assert needle.encode() not in raw, needle


def test_database_file_is_private(tmp_path):
    path = tmp_path / "git-sms.db"
    make(path=str(path)).close()
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600


def test_sealed_rows_are_bound_to_their_sender(tmp_path):
    path = tmp_path / "git-sms.db"
    store = make(path=str(path))
    store.enqueue_inbound("SM1", "alice-key", PAYLOAD)
    db = sqlite3.connect(path)
    db.execute("UPDATE inbound SET sender_key = 'mallory-key'")
    db.commit()
    with pytest.raises(InvalidTag):
        store.claim_inbound()


def test_foreign_schema_versions_are_refused(tmp_path):
    path = tmp_path / "old.db"
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE inbound (message_sid TEXT)")
    db.commit()
    db.close()
    with pytest.raises(ConfigError, match="schema version 0"):
        make(path=str(path))


# Second review: outbound ordering and delivery callbacks ---------------------------------


def _replies(store, sender, sids):
    for message_sid in sids:
        store.enqueue_inbound(message_sid, sender, {"from": ALICE, "to": "+1", "body": "x"})
        store.complete_inbound(store.claim_inbound(), [({"to": ALICE, "from": "+1", "body": message_sid}, 1)])


def test_one_senders_replies_are_handed_over_one_at_a_time_in_order(clock):
    """P2-03: a later reply must not overtake an earlier one that is in flight or waiting to retry."""
    store = make(clock)
    _replies(store, "alice", ["SM1", "SM2"])
    _replies(store, "bob", ["SM3"])
    first = store.claim_outbound()
    assert first.payload["body"] == "SM1"
    assert store.claim_outbound().payload["body"] == "SM3"  # other senders are not blocked
    assert store.claim_outbound() is None  # SM2 waits while SM1 is in flight
    store.retry_outbound(first.id, 30, "twilio 429")
    assert store.claim_outbound() is None  # and while SM1 waits to retry
    clock.advance(31)
    assert store.claim_outbound().payload["body"] == "SM1"


def test_delivery_status_only_moves_forward(clock):
    """P2-04: callbacks can repeat and arrive out of order."""
    store = make(clock)
    _replies(store, "alice", ["SM1"])
    store.settle_outbound(store.claim_outbound().id, "accepted", provider_sid="SMo")
    assert store.record_delivery("SMo", "sent", None) == "applied"
    assert store.record_delivery("SMo", "delivered", None) == "applied"
    assert store.record_delivery("SMo", "sent", None) == "stale"
    assert store.record_delivery("SMo", "delivered", None) == "stale"
    assert store.record_delivery("SMo", "undelivered", "30003") == "stale"  # one terminal status wins
    assert store.record_delivery("SMo", "read", None) == "applied"
    assert store.record_delivery("SMo", "nonsense", None) == "ignored"
    assert store.outbound_for("SM1")[0][4] == "read"


def test_a_callback_that_beats_the_send_record_is_kept(clock):
    """P2-04: Twilio may call back before the send has recorded the SID."""
    store = make(clock)
    _replies(store, "alice", ["SM1"])
    message = store.claim_outbound()
    assert store.record_delivery("SMo", "sent", None) == "early"
    assert store.record_delivery("SMo", "undelivered", "30034") == "early"
    assert store.record_delivery("SMo", "queued", None) == "early"  # an older early event does not regress it
    store.settle_outbound(message.id, "accepted", provider_sid="SMo")
    assert store.outbound_for("SM1") == [("accepted", "SMo", "twilio 30034", 1, "undelivered")]


def test_twilio_statuses_never_collide_with_the_send_lifecycle(clock):
    """Found in the second pass: Twilio's own 'sending' status must not look like an interrupted send."""
    store = make(clock)
    _replies(store, "alice", ["SM1", "SM2"])
    store.settle_outbound(store.claim_outbound().id, "accepted", provider_sid="SMo")
    store.record_delivery("SMo", "sending", None)
    assert store.recover() == {"requeued": 0, "unknown_sends": 0}
    assert store.outbound_for("SM1")[0][0] == "accepted"
    assert store.claim_outbound().payload["body"] == "SM2"  # the sender is not blocked by it


def test_health_counts_without_identifiers(clock):
    store = make(clock)
    store.enqueue_inbound("SM1", "alice", PAYLOAD)
    store.record_intent("op1", "k", "CreateIssue", "acme/x", None, "fp", {})
    clock.advance(30)
    health = store.health()
    assert health["inbound_waiting"] == 1 and health["oldest_inbound_seconds"] == 30
    assert health["writes_approved_unattempted"] == 1
    assert ALICE not in str(health)
