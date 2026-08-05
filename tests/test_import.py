import os
import subprocess
import sys
from pathlib import Path


def test_modules_import_without_configuration():
    repo_root = Path(__file__).resolve().parents[1]
    env = os.environ.copy()
    for name in ("GITHUB_TOKEN", "GITHUB_OPENAI_API_KEY", "OPENAI_API_KEY"):
        env.pop(name, None)
    env["PYTHONDONTWRITEBYTECODE"] = "1"

    code = (
        "import os, sys; "
        "sys.path.insert(0, os.getcwd()); "
        "import commands, llm_client, summarizers, natural_language_router, main"
    )
    result = subprocess.run(
        [sys.executable, "-I", "-c", code],
        cwd=repo_root,
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )

    assert result.returncode == 0, result.stderr
