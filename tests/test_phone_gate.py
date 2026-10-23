"""Static checks and a stubbed run of the Android/Termux gate (e2e/phone/git-sms-gate.sh)."""

import base64
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

GATE = Path(__file__).resolve().parents[1] / "e2e" / "phone" / "git-sms-gate.sh"
ALLOWED = "+15550100100"


def test_no_regex_repeat_bound_exceeds_androids_limit():
    """Bionic libc caps {m,n} at 255 (RE_DUP_MAX); glibc does not, so this only fails on the phone."""
    bounds = [int(n) for pair in re.findall(r"\{(\d+)(?:,(\d+))?\}", GATE.read_text()) for n in pair if n]
    assert bounds and max(bounds) <= 255


@pytest.fixture
def gate(tmp_path):
    if not shutil.which("bash"):
        pytest.skip("bash not available")
    bin_dir, home = tmp_path / "bin", tmp_path / "home"
    (home / ".config" / "git-sms-gate").mkdir(parents=True)
    (home / ".config" / "git-sms-gate" / "allow").write_text(ALLOWED + "\n")
    bin_dir.mkdir()
    log = tmp_path / "calls.log"
    (bin_dir / "termux-sms-send").write_text(f'#!/bin/bash\necho "SEND $*" >> {log}\n')
    (bin_dir / "termux-sms-list").write_text(
        "#!/bin/bash\n"
        'if [[ "$1" == "-h" ]]; then echo "Usage: termux-sms-list [-d] [-f number]";'
        " for i in $(seq 1 2000); do echo pad; done; exit 1; fi\n"
        f'echo "LIST $*" >> {log}\necho "[]"\n'
    )
    (bin_dir / "termux-telephony-deviceinfo").write_text("#!/bin/bash\necho '{}'\n")
    for stub in bin_dir.iterdir():
        stub.chmod(0o755)

    def run(command: str) -> tuple[int, str, str]:
        env = {"HOME": str(home), "PATH": f"{bin_dir}:/usr/bin:/bin", "SSH_ORIGINAL_COMMAND": command}
        result = subprocess.run(["bash", str(GATE)], env=env, capture_output=True, text=True, timeout=30)
        calls = log.read_text() if log.exists() else ""
        return result.returncode, result.stdout.strip(), calls

    return run


def b64(text: str) -> str:
    return base64.b64encode(text.encode()).decode()


def test_send_accepts_a_realistic_reply(gate):
    reply = "You are twilio-harness. Read: owner/repo. Write: owner/repo. Default repo: none. " * 3
    code, out, calls = gate(f"send {ALLOWED} {b64(reply)}")
    assert (code, out) == (0, "sent") and calls.startswith(f"SEND -n {ALLOWED} You are")


@pytest.mark.parametrize(
    "command",
    [
        f"send +15550100999 {b64('hi')}",  # not allowlisted
        f"send {ALLOWED} {b64('hi')}; id",  # chained command
        f"send {ALLOWED} $(id)",
        f"send {ALLOWED} {b64('x' * 1700)}",  # over 1600 characters
        f"send {ALLOWED} {'A' * 2204}",  # over 2200 encoded characters
        f"inbox {ALLOWED} 99",
        "inbox +15550100999",
        f"inbox {ALLOWED}; cat ~/.ssh/authorized_keys",
        "id",
        "",
    ],
)
def test_everything_else_is_refused_without_side_effects(gate, command):
    code, out, calls = gate(command)
    assert code == 2 and out.startswith("refused") and calls == ""


def test_inbox_reads_only_the_allowed_sender_even_with_a_slow_help_text(gate):
    for _ in range(5):  # the old grep -q pipeline lost this race every time
        code, out, calls = gate(f"inbox {ALLOWED} 20")
        assert code == 0 and out == "[]"
    assert f"-f {ALLOWED}" in calls


def test_ping_reports_state_without_message_data(gate):
    code, out, _ = gate("ping")
    assert code == 0 and "gate: git-sms-gate v4" in out and "sender_filter: supported" in out


@pytest.mark.skipif(os.name != "posix", reason="POSIX shell only")
def test_gate_is_valid_bash():
    assert subprocess.run(["bash", "-n", str(GATE)], check=False).returncode == 0


def test_the_handset_client_keeps_only_received_messages(monkeypatch):
    """Termux:API's sender filter returns both directions; a gateway must never read its own replies."""
    import json
    import sys
    from types import SimpleNamespace

    sys.path.insert(0, str(GATE.parents[1]))
    from handset import Handset

    listing = [
        {"_id": 1, "type": "inbox", "received": "2026-10-23 11:55:17", "body": "whoami"},
        {"_id": 2, "type": "sent", "received": "2026-10-23 11:55:25", "body": "You are handset."},
    ]
    handset = Handset("phone", "/dev/null")
    monkeypatch.setattr(handset, "_run", lambda command: SimpleNamespace(returncode=0, stdout=json.dumps(listing)))
    assert [m["body"] for m in handset.inbox("+15550100100")] == ["whoami"]
