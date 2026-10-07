import pytest
from pydantic import ValidationError
from sqlalchemy import make_url

from app.config import DatabaseSettings, Settings


@pytest.mark.parametrize("settings_class", [DatabaseSettings, Settings])
@pytest.mark.parametrize("password", ["simple-password", "p@ss:/?#%$ with spaces"])
def test_database_password_is_encoded_for_both_bot_and_migrations(settings_class, password):
    settings = settings_class(
        _env_file=None,
        database_url="",
        postgres_password=password,
        telegram_bot_token="123456:test",
    )
    url = make_url(settings.database_url)
    assert url.password == password
    assert (url.drivername, url.username, url.host, url.port, url.database) == (
        "postgresql+asyncpg", "monitor", "postgres", 5432, "monitor"
    )
    assert password not in repr(settings)


def test_explicit_database_url_takes_precedence():
    url = "sqlite+aiosqlite:///:memory:"
    settings = DatabaseSettings(_env_file=None, database_url=url, postgres_password="ignored")
    assert settings.database_url == url


def test_password_can_be_loaded_from_dotenv(tmp_path, monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("POSTGRES_PASSWORD", raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text("POSTGRES_PASSWORD='p@ss$word'\n", encoding="utf-8")
    settings = DatabaseSettings(_env_file=env_file)
    assert make_url(settings.database_url).password == "p@ss$word"


def test_missing_database_configuration_has_clear_error():
    with pytest.raises(ValidationError, match="Set POSTGRES_PASSWORD or DATABASE_URL"):
        DatabaseSettings(_env_file=None, database_url="", postgres_password=None)
