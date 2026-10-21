import threading

from git_sms.store import Store

from .conftest import ALICE, Clock


def make(clock=None, path=":memory:"):
    return Store(path, "s" * 32, clock=clock or Clock())


def test_inbound_is_claimed_once():
    store = make()
    assert store.claim_inbound("SM1", "k")
    assert not store.claim_inbound("SM1", "k")
    store.finish_inbound("SM1", "done")
    assert store.inbound_status("SM1") == "done"


def test_concurrent_claims_admit_exactly_one(tmp_path):
    store = make(path=str(tmp_path / "db.sqlite"))
    results = []
    threads = [threading.Thread(target=lambda: results.append(store.claim_inbound("SMX", "k"))) for _ in range(16)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert results.count(True) == 1


def test_fixed_windows_reset():
    clock = Clock()
    store = make(clock)
    assert [store.consume("c", 60) for _ in range(3)] == [1, 2, 3]
    assert store.consume("c", 60, amount=5) == 8
    assert store.peek("c", 60) == 8
    clock.advance(60)
    assert store.peek("c", 60) == 0
    assert store.consume("c", 60) == 1


def test_pending_is_taken_at_most_once():
    store = make()
    store.set_pending("k", "1234", {"type": "CreateIssue"}, 600)
    assert store.get_pending("k").code == "1234"
    assert store.fail_pending("k") == 1
    assert store.take_pending("k").attempts == 1
    assert store.take_pending("k") is None
    assert not store.drop_pending("k")


def test_pages_and_default_repo_share_context():
    store = make()
    store.set_default_repo("k", "acme/docs")
    store.set_pages("k", ["two", "three"])
    assert store.default_repo("k") == "acme/docs"
    assert [store.next_page("k"), store.next_page("k"), store.next_page("k")] == ["two", "three", None]
    store.set_default_repo("k", "acme/other")
    assert store.default_repo("k") == "acme/other"


def test_opt_out_state():
    store = make()
    store.set_opted_out("k", True)
    assert store.is_opted_out("k")
    store.set_opted_out("k", False)
    assert not store.is_opted_out("k")


def test_sender_keys_are_keyed_hashes():
    a, b = Store(":memory:", "a" * 32), Store(":memory:", "b" * 32)
    assert a.sender_key(ALICE) == a.sender_key(ALICE)
    assert a.sender_key(ALICE) != b.sender_key(ALICE)
    assert ALICE[1:] not in a.sender_key(ALICE)


def test_database_file_holds_no_phone_numbers(tmp_path):
    path = tmp_path / "git-sms.db"
    store = make(path=str(path))
    key = store.sender_key(ALICE)
    store.claim_inbound("SM1", key)
    store.consume(f"commands:{key}", 3600)
    store.set_pending(key, "1234", {"type": "CreateIssue", "repo": "acme/x"}, 600)
    store.set_default_repo(key, "acme/x")
    store.audit(sender_key=key, action="CreateIssue", outcome="done", alias="alice")
    store.close()
    raw = b"".join(p.read_bytes() for p in tmp_path.iterdir())
    assert ALICE.encode() not in raw and ALICE[2:].encode() not in raw


def test_audit_detail_is_bounded():
    store = make()
    store.audit(sender_key="k", action="x", outcome="y", detail="z" * 2000)
    assert len(store.audit_log()[0].detail) == 500
