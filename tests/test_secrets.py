"""Loading settings from AWS Secrets Manager, and settings validation."""

from __future__ import annotations

import json

import pytest

from app.config import SECRETS_ID_ENV, SecretsManagerSource, Settings


class FakeSecretsManager:
    def __init__(self, secret: dict | str):
        self.secret = secret if isinstance(secret, str) else json.dumps(secret)
        self.calls: list[str] = []

    def get_secret_value(self, SecretId):  # noqa: N803 - boto3's argument name
        self.calls.append(SecretId)
        return {"SecretString": self.secret}


def _settings_with(client, **init) -> Settings:
    class S(Settings):
        @classmethod
        def settings_customise_sources(cls, settings_cls, init_settings, env_settings, **_):
            return (init_settings, env_settings, SecretsManagerSource(settings_cls, client))

    return S(**init)


def test_settings_load_from_secrets_manager(monkeypatch):
    monkeypatch.setenv(SECRETS_ID_ENV, "hoa-portal/production")
    monkeypatch.delenv("SECRET_KEY", raising=False)
    fake = FakeSecretsManager(
        {
            "SECRET_KEY": "s" * 40,
            "DROPBOX_REFRESH_TOKEN": "refresh-placeholder",
            "ANTHROPIC_API_KEY": "api-placeholder",
            "SOMETHING_ELSE": "ignored",
        }
    )
    s = _settings_with(fake)
    assert fake.calls == ["hoa-portal/production"]
    assert s.secret_key == "s" * 40
    assert s.dropbox_refresh_token == "refresh-placeholder"
    assert s.anthropic_api_key == "api-placeholder"
    # Secrets never show up in logs or tracebacks.
    assert "refresh-placeholder" not in repr(s) and "api-placeholder" not in repr(s)


def test_environment_overrides_secret_and_no_secret_id_means_no_call(monkeypatch):
    monkeypatch.setenv(SECRETS_ID_ENV, "hoa-portal/production")
    monkeypatch.setenv("HOA_NAME", "From env")
    s = _settings_with(FakeSecretsManager({"HOA_NAME": "From secret"}))
    assert s.hoa_name == "From env"

    monkeypatch.delenv(SECRETS_ID_ENV)
    fake = FakeSecretsManager({})
    _settings_with(fake)
    assert fake.calls == []


def test_secret_must_be_a_json_object(monkeypatch):
    monkeypatch.setenv(SECRETS_ID_ENV, "hoa-portal/production")
    with pytest.raises(ValueError, match="JSON"):
        _settings_with(FakeSecretsManager("not json"))


def test_production_requires_dropbox_credentials():
    from pydantic import ValidationError

    with pytest.raises(ValidationError, match="DROPBOX_REFRESH_TOKEN"):
        Settings(app_env="production", document_source="dropbox")


def test_folder_mapping_is_validated():
    from pydantic import ValidationError

    with pytest.raises(ValidationError, match="unknown group"):
        Settings(dropbox_folder_roles='{"Board": ["bored"]}')
    with pytest.raises(ValidationError, match="JSON"):
        Settings(dropbox_folder_roles="Board=board")
    s = Settings(dropbox_folder_roles='{"Board": ["board"], "Board/Legal": ["board"]}')
    assert [p for p, _ in s.dropbox_folder_map] == ["board/legal", "board"]  # deepest first


def test_config_errors_dont_print_secrets(monkeypatch):
    from pydantic import ValidationError

    monkeypatch.setenv(SECRETS_ID_ENV, "hoa-portal/production")
    monkeypatch.delenv("SECRET_KEY", raising=False)
    fake = FakeSecretsManager({"SECRET_KEY": "k" * 64, "ANTHROPIC_API_KEY": "sk-placeholder-tail"})
    with pytest.raises(ValidationError) as exc:
        _settings_with(fake, app_env="production", document_source="dropbox")
    assert "DROPBOX_REFRESH_TOKEN" in str(exc.value)  # the problem is named
    assert "kkkk" not in str(exc.value) and "placeholder-tail" not in str(exc.value)


# --- the model and its prices -------------------------------------------------------

PRODUCTION = {
    "app_env": "production",
    "secret_key": "x" * 48,
    "session_cookie_secure": True,
    "database_url": "postgresql+psycopg://u:p@db.example/neondb",
    "llm_provider": "anthropic",
    "anthropic_api_key": "sk-placeholder",
    "email_provider": "ses",
    "retrieval": "keyword",
}


@pytest.mark.parametrize(
    "model,prices",
    [
        ("claude-haiku-5-5", (0.10, 0.50)),
        ("claude-sonnet-5-5", (2.0, 10.0)),
        ("claude-opus-5-5", (4.0, 20.0)),
    ],
)
def test_known_models_bring_their_prices(model, prices):
    s = Settings(**PRODUCTION, anthropic_model=model)
    assert (s.chat_price_input_per_mtok, s.chat_price_output_per_mtok) == prices


def test_unknown_model_needs_both_prices_in_production():
    from pydantic import ValidationError

    with pytest.raises(ValidationError, match="CHAT_PRICE_INPUT_PER_MTOK"):
        Settings(**PRODUCTION, anthropic_model="claude-opus-9")
    with pytest.raises(ValidationError, match="CHAT_PRICE_INPUT_PER_MTOK"):
        Settings(**PRODUCTION, anthropic_model="claude-opus-9", chat_price_input_per_mtok=5)
    s = Settings(
        **PRODUCTION,
        anthropic_model="claude-opus-9",
        chat_price_input_per_mtok=5,
        chat_price_output_per_mtok=25,
    )
    assert s.chat_price_output_per_mtok == 25
    # Outside production an unknown model is counted at the highest known price.
    assert Settings(anthropic_model="claude-opus-9").chat_price_output_per_mtok == 20.0


def test_model_settings_the_api_would_reject_are_refused():
    from pydantic import ValidationError

    def make(**kw):
        return Settings(llm_provider="anthropic", **kw)

    with pytest.raises(ValidationError, match="can't turn thinking off"):
        make(anthropic_model="claude-haiku-5-5", anthropic_effort="max")
    with pytest.raises(ValidationError, match="can't turn thinking off"):
        make(anthropic_model="claude-sonnet-5-5", anthropic_effort="xhigh")
    with pytest.raises(ValidationError, match="no server-side fallback"):
        make(anthropic_model="claude-haiku-5-5", anthropic_fallbacks=True)
    make(anthropic_model="claude-opus-5-5", anthropic_effort="max")  # thinking always on
    make(anthropic_model="claude-haiku-5-5", anthropic_effort="max", anthropic_thinking=True)
