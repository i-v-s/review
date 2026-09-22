from __future__ import annotations

import argparse
import asyncio
import json
import os
import ssl
from pathlib import Path

import aiohttp
from aiohttp import web

from .app import create_app
from .config import Config


async def send_decision(args):
    token = os.environ.get("REVIEW_TOKEN", "")
    if args.token_file:
        token = Path(args.token_file).read_text().strip()
    if not token:
        raise SystemExit("Set REVIEW_TOKEN or pass --token-file")
    async with aiohttp.ClientSession() as client:
        async with client.post(args.url.rstrip("/") + "/api/v1/decisions",
                               headers={"Authorization": "Bearer " + token},
                               json={"text": args.text, "session_id": args.session}) as response:
            body = await response.json()
            if response.status >= 400:
                raise SystemExit(body.get("error", str(response.status)))
            print(body["id"])


def main():
    parser = argparse.ArgumentParser(description="Local review for agent-assisted development")
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--state-dir", type=Path)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--tls-cert", type=Path)
    parser.add_argument("--tls-key", type=Path)
    sub = parser.add_subparsers(dest="command")
    decision = sub.add_parser("decision", help="Record an explicit engineering decision")
    decision.add_argument("--text", required=True)
    decision.add_argument("--session", default="manual")
    decision.add_argument("--url", default="http://127.0.0.1:8765")
    decision.add_argument("--token-file", type=Path)
    args = parser.parse_args()
    if args.command == "decision":
        asyncio.run(send_decision(args))
        return
    if bool(args.tls_cert) != bool(args.tls_key):
        parser.error("--tls-cert and --tls-key must be supplied together")
    if args.host not in ("127.0.0.1", "::1", "localhost") and not args.tls_cert:
        parser.error("Remote listeners require --tls-cert and --tls-key")
    context = None
    if args.tls_cert:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.load_cert_chain(args.tls_cert, args.tls_key)
    config = Config.from_env(args.repo, state_dir=args.state_dir, host=args.host, port=args.port)
    print(f"Repository: {config.repo}\nState: {config.state_dir}")
    print("Browser access token: REVIEW_TOKEN" if config.token else f"Browser access token file: {config.state_dir / 'token'}")
    web.run_app(create_app(config), host=config.host, port=config.port, ssl_context=context, access_log=None)


if __name__ == "__main__":
    main()
