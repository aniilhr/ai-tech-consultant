"""Command line for batch jobs (works without the API server running).

python -m brain.cli sync --limit 25          # first small batch
python -m brain.cli sync --limit 200         # continue; already-done articles are skipped
python -m brain.cli sync                     # everything left (cron this daily)
python -m brain.cli images --limit 100       # describe pending screenshots with Gemini vision
python -m brain.cli embed                    # backfill embeddings after adding GEMINI_API_KEY
python -m brain.cli stats
python -m brain.cli ask "How do I add a membership plan?"
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging

from .config import get_settings
from .db import Database
from .ingest import Ingestor
from .llm import LLMClient
from .service import KnowledgeService


async def main() -> None:
    parser = argparse.ArgumentParser(prog="brain")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_sync = sub.add_parser("sync", help="Incremental sitemap sync")
    p_sync.add_argument("--limit", type=int, default=None)
    p_sync.add_argument("--no-images", action="store_true")
    p_sync.add_argument("--max-images", type=int, default=None)
    p_img = sub.add_parser("images", help="Describe pending images")
    p_img.add_argument("--limit", type=int, default=50)
    sub.add_parser("embed", help="Embed chunks missing an embedding")
    sub.add_parser("stats")
    p_ask = sub.add_parser("ask")
    p_ask.add_argument("question")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    s = get_settings()
    db = Database(s.db_path)
    llm = LLMClient(s)
    ingestor = Ingestor(db, llm, s)
    service = KnowledgeService(db, llm, s, ingestor)

    if args.cmd == "sync":
        result = await ingestor.sync(limit=args.limit, describe_images=not args.no_images, max_images=args.max_images)
    elif args.cmd == "images":
        result = await ingestor.describe_images(args.limit)
    elif args.cmd == "embed":
        warnings: list[str] = []
        result = {"embedded": await ingestor.embed_pending(warnings), "warnings": warnings}
    elif args.cmd == "stats":
        result = service.stats()
    else:
        result = await service.ask(args.question, create_escalation=False, source="api")
    print(json.dumps(result, indent=2, default=str))


if __name__ == "__main__":
    asyncio.run(main())
