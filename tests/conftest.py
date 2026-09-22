from __future__ import annotations

import asyncio
import os
import subprocess

import pytest

from review_service.app import SERVICE, create_app
from review_service.config import Config


def git_command(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True,
                          env={**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null"}).stdout


@pytest.fixture
def repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    git_command(repo, "init", "-q")
    git_command(repo, "config", "user.email", "test@example.invalid")
    git_command(repo, "config", "user.name", "Test")
    (repo / "example.py").write_text("one\ntwo\nthree\nfour\n")
    git_command(repo, "add", ".")
    git_command(repo, "commit", "-qm", "Initial")
    return repo


@pytest.fixture
async def client(repo, tmp_path, aiohttp_client):
    config = Config(repo, state_dir=tmp_path / "state", token="test-secret", poll_interval=3600)
    app = create_app(config)
    client = await asyncio.wait_for(aiohttp_client(app, headers={"Authorization": "Bearer test-secret"}), 10)
    return client


@pytest.fixture
def service(client):
    return client.app[SERVICE]
