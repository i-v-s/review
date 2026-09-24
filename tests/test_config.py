import os

import pytest

from review_service.config import Config, DEFAULT_FILE_FILTERS


def test_file_filters_use_defaults_without_project_file(tmp_path):
    config = Config(tmp_path / 'repo', state_dir=tmp_path / 'state')
    assert config.file_filters == DEFAULT_FILE_FILTERS


def test_dotenv_loads_settings_without_changing_process_environment(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    (tmp_path / '.env').write_text(
        "OPENAI_API_KEY='file key'\n"
        'REVIEW_MODEL=file-model\n'
        'REVIEW_TOKEN=file-token\n'
        'REVIEW_LLM_TIMEOUT_SECONDS=42.5\n'
    )
    monkeypatch.delenv('OPENAI_API_KEY', raising=False)
    monkeypatch.delenv('REVIEW_TOKEN', raising=False)
    monkeypatch.setenv('REVIEW_MODEL', 'process-model')

    config = Config.from_env(tmp_path / 'repo', state_dir=tmp_path / 'state')

    assert config.api_key == 'file key'
    assert config.model == 'process-model'
    assert config.token == 'file-token'
    assert config.llm_timeout_seconds == 42.5
    assert 'OPENAI_API_KEY' not in os.environ
    assert 'REVIEW_TOKEN' not in os.environ


def test_explicit_overrides_take_precedence_over_dotenv(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    (tmp_path / '.env').write_text('REVIEW_MODEL=file-model\n')
    config = Config.from_env(tmp_path / 'repo', state_dir=tmp_path / 'state', model='override-model')
    assert config.model == 'override-model'


def test_project_file_replaces_file_filter_patterns(tmp_path):
    repo = tmp_path / 'repo'
    repo.mkdir()
    (repo / '.review.toml').write_text('[file_filters]\ntests = ["checks/*"]\ndocs = []\n')
    config = Config(repo, state_dir=tmp_path / 'state')
    assert config.file_filters == {'tests': ['checks/*'], 'docs': []}


@pytest.mark.parametrize('content', [
    '[file_filters]\ntests = ["tests/*"]\n',
    '[file_filters]\ntests = "tests/*"\ndocs = []\n',
    '[file_filters]\ntests = ["bad[pattern"]\ndocs = []\n',
])
def test_invalid_project_file_is_rejected(tmp_path, content):
    repo = tmp_path / 'repo'
    repo.mkdir()
    (repo / '.review.toml').write_text(content)
    with pytest.raises(ValueError, match='.review.toml'):
        Config(repo, state_dir=tmp_path / 'state')
