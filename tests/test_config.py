import pytest

from review_service.config import Config, DEFAULT_FILE_FILTERS


def test_file_filters_use_defaults_without_project_file(tmp_path):
    config = Config(tmp_path / 'repo', state_dir=tmp_path / 'state')
    assert config.file_filters == DEFAULT_FILE_FILTERS


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
