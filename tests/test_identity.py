import pytest

from git_sms.config import ConfigError
from git_sms.identity import Directory, User

from .conftest import ALICE, BOB, STRANGER, USERS_TOML


def test_lookup_and_grants():
    directory = Directory.from_toml(USERS_TOML)
    alice, bob = directory.lookup(ALICE), directory.lookup(BOB)
    assert alice.alias == "alice" and not alice.guest
    assert alice.can_read("anyone/anything")
    assert alice.can_write("acme/field-reports") and alice.can_write("ACME/Field-Notes")
    assert not alice.can_write("acme/docs")
    assert bob.can_read("acme/docs") and not bob.can_read("other/secret")
    assert not bob.can_write("acme/docs")
    assert directory.lookup(STRANGER) is None


def test_write_grant_implies_read():
    user = User(phone=ALICE, alias="a", write=("acme/field-reports",))
    assert user.can_read("acme/field-reports")
    assert not user.can_read("acme/docs")


def test_public_read_makes_unknown_senders_read_only_guests():
    directory = Directory.from_toml("public_read = true\n" + USERS_TOML)
    guest = directory.lookup(STRANGER)
    assert guest.guest and guest.alias == "guest"
    assert guest.can_read("any/repo") and not guest.can_write("any/repo")
    assert directory.lookup("not-a-number") is None


def test_wildcards_never_cross_the_owner_boundary():
    user = User(phone=ALICE, alias="a", read=("acme/*",))
    assert user.can_read("acme/x")
    assert not user.can_read("acme-evil/x")
    assert not user.can_read("other/acme")


@pytest.mark.parametrize(
    ("toml", "message"),
    [
        ('[[users]]\nphone = "5551234"\nalias = "a"', "E.164"),
        ('[[users]]\nphone = "+15550100001"\nalias = "has space"', "alias"),
        ('[[users]]\nphone = "+15550100001"\nalias = "guest"', "alias"),
        ('[[users]]\nphone = "+15550100001"\nalias = "a"\nwrite = "acme/x"', "write"),
        ('[[users]]\nphone = "+15550100001"\nalias = "a"\nread = ["acme/x/y"]', "read"),
        ('[[users]]\nphone = "+15550100001"\nalias = "a"\nadmin = true', "unknown keys"),
        ('[[users]]\nphone = "+15550100001"\nalias = "a"\n[[users]]\nphone = "+15550100001"\nalias = "b"', "twice"),
        ('[[users]]\nphone = "+15550100001"\nalias = "a"\n[[users]]\nphone = "+15550100002"\nalias = "A"', "twice"),
        ('public_read = "yes"', "public_read"),
        ("users = 3", "array"),
        ("not toml [", "TOML"),
    ],
)
def test_invalid_directories_fail_closed(toml, message):
    with pytest.raises(ConfigError, match=message):
        Directory.from_toml(toml)


def test_missing_file_fails_closed(tmp_path):
    with pytest.raises(ConfigError, match="cannot read"):
        Directory.load(str(tmp_path / "absent.toml"))
    assert Directory.load(None).lookup(ALICE) is None
