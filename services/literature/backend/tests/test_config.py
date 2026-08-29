from backend.app.config import Settings


def test_settings_do_not_expose_secret_values() -> None:
    settings = Settings()
    assert "local-development-only" not in repr(settings.oidc_client_secret)
    assert settings.database_url.startswith("postgresql+psycopg://")
