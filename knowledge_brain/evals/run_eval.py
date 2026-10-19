"""Knowledge Brain eval: answer/abstain accuracy, citation hit rate, latency, tokens and cost.

Run against the real, crawled knowledge base (same DB the API uses):

    python evals/run_eval.py                                   # current .env settings
    GEMINI_MODEL_SMART=gemini-3.5-flash-lite python evals/run_eval.py --label flash-lite
    LLM_PROVIDER=anthropic python evals/run_eval.py --label claude
    LLM_PROVIDER=offline python evals/run_eval.py --label offline-bm25

Results are printed as a markdown row and saved to evals/results/<label>.json. Labels in questions.jsonl
were written before the crawl; review expected_title_keywords against the real articles once.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from brain.config import Settings  # noqa: E402
from brain.db import Database  # noqa: E402
from brain.llm import LLMClient  # noqa: E402
from brain.service import KnowledgeService  # noqa: E402

HERE = Path(__file__).parent


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--label", default="default")
    parser.add_argument("--questions", default=str(HERE / "questions.jsonl"))
    args = parser.parse_args()

    s = Settings()
    db = Database(s.db_path)
    service = KnowledgeService(db, LLMClient(s), s)
    if not db.one("SELECT 1 FROM chunks LIMIT 1"):
        sys.exit("Knowledge base is empty. Run `python -m brain.cli sync --limit 50` first.")

    cases = [json.loads(line) for line in Path(args.questions).read_text().splitlines() if line.strip()]  # noqa: ASYNC240
    rows = []
    for case in cases:
        r = await service.ask(case["question"], create_escalation=False, source="api")
        titles = " | ".join(c["title"].lower() for c in r["citations"])
        cited_ok = (not case["expect_answerable"]) or any(k in titles for k in case["expected_title_keywords"])
        correct = r["answered"] == case["expect_answerable"] and (cited_ok if r["answered"] else True)
        rows.append(
            {
                "id": case["id"],
                "expect_answerable": case["expect_answerable"],
                "answered": r["answered"],
                "citation_hit": cited_ok if r["answered"] else None,
                "correct": correct,
                "confidence": r["confidence"],
                "mode": r["llm"]["mode"],
                "model": r["llm"]["model"],
                "latency_ms": r["latency_ms"],
                "input_tokens": r["llm"]["input_tokens"],
                "output_tokens": r["llm"]["output_tokens"],
                "cost_usd": r["llm"]["cost_usd"],
                "top_citation": r["citations"][0]["title"] if r["citations"] else None,
            }
        )
        print(f"{case['id']} correct={correct} answered={r['answered']} conf={r['confidence']} {r['latency_ms']}ms")

    answerable = [x for x in rows if x["expect_answerable"]]
    unanswerable = [x for x in rows if not x["expect_answerable"]]
    costs = [x["cost_usd"] for x in rows if x["cost_usd"] is not None]
    summary = {
        "label": args.label,
        "provider": service.llm.provider,
        "model": service.llm.model_for("smart") if service.llm.enabled else "none (extractive)",
        "retrieval": "hybrid" if service.llm.embeddings_enabled else "bm25",
        "cases": len(rows),
        "overall_correct": round(sum(x["correct"] for x in rows) / len(rows), 3),
        "answered_when_answerable": round(sum(x["answered"] for x in answerable) / max(len(answerable), 1), 3),
        "citation_hit_rate": round(
            sum(bool(x["citation_hit"]) for x in answerable if x["answered"]) / max(sum(x["answered"] for x in answerable), 1), 3
        ),
        "abstained_when_unanswerable": round(sum(not x["answered"] for x in unanswerable) / max(len(unanswerable), 1), 3),
        "p50_latency_ms": int(statistics.median(x["latency_ms"] for x in rows)),
        "avg_tokens": int(statistics.mean(x["input_tokens"] + x["output_tokens"] for x in rows)),
        "avg_cost_usd": round(statistics.mean(costs), 6) if costs else None,
    }
    out = HERE / "results"
    out.mkdir(exist_ok=True)
    (out / f"{args.label}.json").write_text(json.dumps({"summary": summary, "rows": rows}, indent=2))
    print("\n| label | provider/model | retrieval | correct | answered (answerable) | citation hit | abstain (unanswerable) | p50 ms | avg tokens | avg $ |")
    print("|---|---|---|---|---|---|---|---|---|---|")
    print(
        f"| {summary['label']} | {summary['provider']}/{summary['model']} | {summary['retrieval']} | {summary['overall_correct']} | "
        f"{summary['answered_when_answerable']} | {summary['citation_hit_rate']} | {summary['abstained_when_unanswerable']} | "
        f"{summary['p50_latency_ms']} | {summary['avg_tokens']} | {summary['avg_cost_usd']} |"
    )


if __name__ == "__main__":
    asyncio.run(main())
