from __future__ import annotations

import hashlib
import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import dotenv_values


def settings_environment() -> dict[str, str]:
    """Read local settings without changing the process environment."""
    file_values = dotenv_values(Path.cwd() / ".env")
    return {key: value for key, value in file_values.items() if value is not None} | dict(os.environ)


DEFAULT_FILE_FILTERS = {
    "tests": ["tests/*", "*/tests/*", "test_*.py", "*/test_*.py", "*_test.py", "*.test.*", "*.spec.*"],
    "docs": ["docs/*", "*/docs/*", "README*", "*.md", "*.rst", "*.adoc"],
}


@dataclass
class Config:
    repo: Path
    state_dir: Path | None = None
    token: str | None = None
    host: str = "127.0.0.1"
    port: int = 8765
    poll_interval: float = 5.0
    api_key: str = ""
    base_url: str = "https://api.openai.com/v1"
    model: str = ""
    codex_command: str = "codex"
    opencode_url: str = ""
    opencode_username: str = "opencode"
    opencode_password: str = ""
    max_file_bytes: int = 2_000_000
    max_context_chars: int = 100_000
    max_output_tokens: int = 8192
    llm_timeout_seconds: float = 600.0
    llm_max_retries: int = 0
    llm_structured_output: bool = True
    llm_proxy: str = field(default="", repr=False)
    file_filters: dict[str, list[str]] = field(init=False)

    def __post_init__(self):
        self.repo = self.repo.expanduser().resolve()
        if self.state_dir is None:
            key = hashlib.sha256(str(self.repo).encode()).hexdigest()[:16]
            root = Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local/state")))
            self.state_dir = root / "review-service" / key
        self.state_dir = self.state_dir.expanduser().resolve()
        if self.state_dir == self.repo or self.repo in self.state_dir.parents:
            raise ValueError("State directory must be outside the reviewed repository")
        if self.llm_timeout_seconds <= 0:
            raise ValueError("LLM timeout must be greater than zero")
        if self.llm_max_retries < 0:
            raise ValueError("LLM max retries cannot be negative")
        if self.max_context_chars <= 0 or self.max_output_tokens <= 0:
            raise ValueError("LLM context and output limits must be greater than zero")
        rules_path = self.repo / ".review.toml"
        self.file_filters = {name: patterns.copy() for name, patterns in DEFAULT_FILE_FILTERS.items()}
        if rules_path.exists():
            try:
                rules = tomllib.loads(rules_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, tomllib.TOMLDecodeError) as exc:
                raise ValueError(f"{rules_path}: не удалось прочитать настройки фильтров: {exc}") from exc
            if set(rules) != {"file_filters"} or not isinstance(rules["file_filters"], dict):
                raise ValueError(f"{rules_path}: ожидается таблица [file_filters]")
            filters = rules["file_filters"]
            if set(filters) != {"tests", "docs"}:
                raise ValueError(f"{rules_path}: укажите списки tests и docs")
            for name, patterns in filters.items():
                if (not isinstance(patterns, list) or
                        any(not isinstance(pattern, str) or not pattern or len(pattern) > 256 or
                            any(char in pattern for char in "[]\\") for pattern in patterns)):
                    raise ValueError(f"{rules_path}: {name} должен быть списком glob-шаблонов с * и ?")
                self.file_filters[name] = patterns
        if self.llm_proxy:
            try:
                proxy = urlsplit(self.llm_proxy)
                if (proxy.scheme not in ("socks5", "socks5h") or not proxy.hostname
                        or not proxy.port or proxy.path not in ("", "/")
                        or proxy.query or proxy.fragment or any(c.isspace() for c in self.llm_proxy)):
                    raise ValueError()
            except ValueError:
                raise ValueError("REVIEW_LLM_PROXY: ожидается socks5://[user:password@]host:port") from None

    @classmethod
    def from_env(cls, repo: Path, **overrides):
        env = settings_environment()
        structured = env.get("REVIEW_LLM_STRUCTURED_OUTPUT", "1")
        if structured not in ("0", "1"):
            raise ValueError("REVIEW_LLM_STRUCTURED_OUTPUT: ожидается 0 или 1")
        values = dict(
            api_key=env.get("OPENAI_API_KEY", ""),
            base_url=env.get("OPENAI_BASE_URL", "https://api.openai.com/v1"),
            model=env.get("REVIEW_MODEL", ""),
            token=env.get("REVIEW_TOKEN"),
            llm_timeout_seconds=float(env.get("REVIEW_LLM_TIMEOUT_SECONDS", "600")),
            llm_max_retries=int(env.get("REVIEW_LLM_MAX_RETRIES", "0")),
            llm_structured_output=structured == "1",
            llm_proxy=env.get("REVIEW_LLM_PROXY", ""),
            max_context_chars=int(env.get("REVIEW_MAX_CONTEXT_CHARS", "100000")),
            max_output_tokens=int(env.get("REVIEW_MAX_OUTPUT_TOKENS", "8192")),
            opencode_url=env.get("OPENCODE_URL", ""),
            opencode_username=env.get("OPENCODE_SERVER_USERNAME", "opencode"),
            opencode_password=env.get("OPENCODE_SERVER_PASSWORD", ""),
        )
        return cls(repo=repo, **(values | overrides))
