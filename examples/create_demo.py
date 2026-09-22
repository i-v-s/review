"""Create a fresh disposable repository; never modify an existing directory."""
import argparse
import subprocess
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    repo = args.destination.expanduser().resolve()
    repo.mkdir(parents=True, exist_ok=False)

    def git(*arguments):
        subprocess.run(["git", "-C", str(repo), *arguments], check=True, capture_output=True)

    git("init", "-q")
    git("config", "user.name", "Review demo")
    git("config", "user.email", "demo@example.invalid")
    (repo / "retry.py").write_text('''def should_retry(method: str, status: int) -> bool:
    return status == 503


def request_with_retry(send, method):
    for attempt in range(3):
        response = send(method)
        if not should_retry(method, response.status):
            return response
    return response
''')
    git("add", ".")
    git("commit", "-qm", "Initial request retry logic")
    (repo / "retry.py").write_text('''RETRYABLE_STATUSES = {429, 503}


def should_retry(method: str, status: int) -> bool:
    return method.upper() == "GET" and status in RETRYABLE_STATUSES


def request_with_retry(send, method):
    for attempt in range(3):
        response = send(method)
        if not should_retry(method, response.status):
            return response
    return response
''')
    (repo / "test_retry.py").write_text('''from retry import should_retry


def test_post_is_not_retried():
    assert not should_retry("POST", 503)


def test_get_rate_limit_is_retried():
    assert should_retry("GET", 429)
''')
    print(f"Demo repository: {repo}")
    print(f"Run: review-service --repo {repo}")
    print("Import examples/opencode-session.json in the Sources tab.")
    print("There is an intentional ambiguity: three total attempts versus three retries.")


if __name__ == "__main__":
    main()
