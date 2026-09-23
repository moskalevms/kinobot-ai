# tests/test_config_rt.py
"""Тесты feature flag Rotten Tomatoes через OMDb (Epic B, задача B1).

Без сети: проверяются только переменные окружения и config.rt_scores_enabled().
conftest.py уже добавляет src/ в sys.path.
"""
import config


def test_flag_off_when_disabled(monkeypatch):
    """ENABLE_RT_SCORES=false выключает Epic B даже при заданном ключе."""
    monkeypatch.setenv('ENABLE_RT_SCORES', 'false')
    monkeypatch.setenv('OMDB_API_KEY', 'test-omdb-key')
    assert config.rt_scores_enabled() is False


def test_flag_off_when_variable_missing(monkeypatch):
    """Без переменной ENABLE_RT_SCORES действует default false — выключено."""
    monkeypatch.delenv('ENABLE_RT_SCORES', raising=False)
    monkeypatch.setenv('OMDB_API_KEY', 'test-omdb-key')
    assert config.rt_scores_enabled() is False


def test_flag_off_when_key_missing(monkeypatch):
    """Флаг включён, но OMDB_API_KEY отсутствует — Epic B выключен."""
    monkeypatch.setenv('ENABLE_RT_SCORES', 'true')
    monkeypatch.delenv('OMDB_API_KEY', raising=False)
    assert config.rt_scores_enabled() is False


def test_flag_off_when_key_blank(monkeypatch):
    """Флаг включён, но ключ состоит только из пробелов — выключено."""
    monkeypatch.setenv('ENABLE_RT_SCORES', 'true')
    monkeypatch.setenv('OMDB_API_KEY', '   ')
    assert config.rt_scores_enabled() is False


def test_flag_on(monkeypatch):
    """ENABLE_RT_SCORES=true + непустой ключ — Epic B активен."""
    monkeypatch.setenv('ENABLE_RT_SCORES', 'true')
    monkeypatch.setenv('OMDB_API_KEY', 'test-omdb-key')
    assert config.rt_scores_enabled() is True


def test_flag_on_case_insensitive(monkeypatch):
    """Значение флага регистронезависимо и терпит пробелы по краям."""
    monkeypatch.setenv('ENABLE_RT_SCORES', ' TRUE ')
    monkeypatch.setenv('OMDB_API_KEY', 'test-omdb-key')
    assert config.rt_scores_enabled() is True
