import pytest

from git_sms.config import ConfigError, Settings

PRODUCTION = {
    "GIT_SMS_PUBLIC_URL": "https://sms.example.org/",
    "GIT_SMS_SECRET_KEY": "x" * 40,
    "GIT_SMS_USERS_FILE": "users.toml",
    "TWILIO_ACCOUNT_SID": "AC" + "0" * 32,
    "TWILIO_AUTH_TOKEN": "token",
}


def test_production_defaults_are_complete_and_strict():
    settings = Settings.from_env(PRODUCTION)
    assert settings.problems() == []
    assert settings.public_url == "https://sms.example.org"
    assert settings.confirm_writes and settings.environment == "production"


def test_empty_environment_refuses_to_start():
    problems = Settings.from_env({}).problems()
    joined = "; ".join(problems)
    for needle in ("GIT_SMS_PUBLIC_URL", "TWILIO_AUTH_TOKEN", "TWILIO_ACCOUNT_SID", "GIT_SMS_SECRET_KEY", "USERS_FILE"):
        assert needle in joined
    with pytest.raises(ConfigError):
        Settings.from_env({}).validate()


@pytest.mark.parametrize(
    ("change", "needle"),
    [
        ({"GIT_SMS_PUBLIC_URL": "http://sms.example.org"}, "https://"),
        ({"GIT_SMS_SECRET_KEY": "short"}, "32 characters"),
        ({"GIT_SMS_SENDER": "console"}, "development only"),
        ({"GIT_SMS_SENDER": "pigeon"}, "twilio or console"),
        ({"GIT_SMS_ENV": "staging"}, "production or development"),
        ({"GITHUB_APP_ID": "1"}, "GitHub App auth needs"),
    ],
)
def test_unsafe_production_settings(change, needle):
    assert any(needle in problem for problem in Settings.from_env({**PRODUCTION, **change}).problems())


def test_token_and_app_are_mutually_exclusive():
    env = {
        **PRODUCTION,
        "GITHUB_TOKEN": "t",
        "GITHUB_APP_ID": "1",
        "GITHUB_APP_PRIVATE_KEY": "k",
        "GITHUB_APP_INSTALLATION_ID": "2",
    }
    assert any("not both" in problem for problem in Settings.from_env(env).problems())


def test_development_needs_no_secrets():
    settings = Settings.from_env({"GIT_SMS_ENV": "development", "GIT_SMS_SENDER": "console"})
    assert settings.problems() == []
    assert settings.effective_secret_key


def test_private_key_file_and_escaped_newlines(tmp_path):
    key_file = tmp_path / "app.pem"
    key_file.write_text("-----BEGIN KEY-----\nabc\n-----END KEY-----\n")
    assert Settings.from_env({"GITHUB_APP_PRIVATE_KEY_FILE": str(key_file)}).github_app_private_key.count("\n") == 3
    escaped = Settings.from_env({"GITHUB_APP_PRIVATE_KEY": "-----BEGIN-----\\nabc\\n-----END-----"})
    assert escaped.github_app_private_key == "-----BEGIN-----\nabc\n-----END-----"
    with pytest.raises(ConfigError, match="cannot read"):
        Settings.from_env({"GITHUB_APP_PRIVATE_KEY_FILE": str(tmp_path / "missing.pem")})


@pytest.mark.parametrize(
    ("env", "base", "model"),
    [
        ({"OPENAI_API_KEY": "o", "OPENAI_MODEL": "gpt-x"}, "https://api.openai.com/v1", "gpt-x"),
        ({"OPENAI_API_KEY": "o", "LLM_MODEL": "gpt-y"}, "https://api.openai.com/v1", "gpt-y"),
        ({"LLM_API_KEY": "l", "LLM_BASE_URL": "https://llm.local/v1/", "LLM_MODEL": "m"}, "https://llm.local/v1", "m"),
    ],
)
def test_llm_provider_resolution(env, base, model):
    settings = Settings.from_env(env)
    assert (settings.llm_base_url, settings.llm_model, settings.llm_configured) == (base, model, True)


def test_a_model_must_be_named_explicitly():
    settings = Settings.from_env({"OPENAI_API_KEY": "o"})
    assert not settings.llm_configured
    assert any("LLM_MODEL" in problem for problem in settings.problems())


@pytest.mark.parametrize("name", ["GITHUB_MODELS_TOKEN", "GITHUB_OPENAI_API_KEY", "GITHUB_MODELS_MODEL"])
def test_retired_github_models_settings_are_refused(name):
    settings = Settings.from_env({name: "x", "GIT_SMS_ENV": "development", "GIT_SMS_SENDER": "console"})
    assert not settings.llm_configured
    assert any("GitHub Models was retired" in problem and name in problem for problem in settings.problems())


def test_no_llm_by_default():
    assert not Settings.from_env({}).llm_configured


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("GIT_SMS_MAX_COMMANDS_PER_HOUR", "lots"),
        ("GIT_SMS_MAX_COMMANDS_PER_HOUR", "0"),
        ("GIT_SMS_CONFIRM_WRITES", "maybe"),
    ],
)
def test_bad_numbers_and_booleans(name, value):
    with pytest.raises(ConfigError, match=name):
        Settings.from_env({name: value})
