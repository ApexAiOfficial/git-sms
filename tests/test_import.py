import importlib

def test_modules_import_without_configuration(monkeypatch):
    for name in ("GITHUB_TOKEN", "GITHUB_OPENAI_API_KEY", "OPENAI_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    for module_name in ("commands", "llm_client", "summarizers", "natural_language_router", "main"):
        importlib.import_module(module_name)
