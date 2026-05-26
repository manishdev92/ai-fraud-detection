import os

import pytest

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ["USE_ADK"] = "false"
os.environ["GEMINI_API_KEY"] = ""  # avoid live Gemini calls in CI/tests


@pytest.fixture(autouse=True)
def reset_settings_cache():
    from app.config import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()
