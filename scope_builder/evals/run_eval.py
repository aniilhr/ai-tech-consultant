"""Scope Builder eval: requirement recall, quote verification, change-detection accuracy, latency and cost.

    python evals/run_eval.py --label offline        # LLM_PROVIDER=offline for the heuristic baseline
    python evals/run_eval.py --label flash          # current .env model (Knowledge Brain must run at BRAIN_URL)

Writes evals/results/<label>.json and prints a summary."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scope.brain_client import BrainClient  # noqa: E402
from scope.config import Settings  # noqa: E402
from scope.db import Database  # noqa: E402
from scope.llm import LLMClient  # noqa: E402
from scope.service import ScopeService  # noqa: E402

HERE = Path(__file__).parent


def matches(expected: dict[str, Any], texts: list[str]) -> bool:
    return any(all(word in t for word in group) for group in expected["any"] for t in texts)


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--label", default="default")
    args = parser.parse_args()
    cases = json.loads((HERE / "cases.json").read_text())
    s = Settings(scope_db_path=":memory:")  # type: ignore[call-arg]
    svc = ScopeService(Database(":memory:"), LLMClient(s), BrainClient(s.brain_url, s.brain_timeout_seconds), s)
    brain_up = await svc.brain.health()
    results: dict[str, Any] = {"label": args.label, "provider": svc.llm.provider, "brain_reachable": brain_up, "transcripts": {}}

    scopes: dict[str, dict[str, Any]] = {}
    for sample_id, expected in cases["requirements"].items():
        pid = svc.create_project(sample_id, "eval")
        tid = svc.load_sample(pid, sample_id)
        started = time.perf_counter()
        scope = await svc.analyze(pid, tid)
        elapsed = time.perf_counter() - started
        scopes[sample_id] = {"pid": pid, "scope": scope}
        reqs = scope["data"]["requirements"]
        texts = [f"{r['title']} {r['description']}".lower() for r in reqs]
        found = [e["id"] for e in expected if matches(e, texts)]
        results["transcripts"][sample_id] = {
            "expected": len(expected),
            "recall": round(len(found) / len(expected), 3),
            "missed": [e["id"] for e in expected if e["id"] not in found],
            "extracted": len(reqs),
            "quotes_verified": round(sum(r["quote_verified"] for r in reqs) / max(len(reqs), 1), 3),
            "seconds": round(elapsed, 2),
            "llm": scope["data"]["llm"],
        }
        print(sample_id, results["transcripts"][sample_id]["recall"], "missed:", results["transcripts"][sample_id]["missed"])

    ch = cases["changes"]
    base = scopes[ch["baseline"]]
    svc.approve_scope(base["scope"]["id"], "eval", None)
    tid = svc.load_sample(base["pid"], ch["follow_up"])
    started = time.perf_counter()
    v2 = await svc.analyze(base["pid"], tid)
    items = v2["change_report"]["items"]
    rows = []
    for e in ch["expected"]:
        hit = next((i for i in items if any(k in f"{i['title']} {i['summary']}".lower() for k in e["keywords"])), None)
        type_ok = bool(hit and hit["change_type"] == e["type"])
        oos_ok = e["out_of_scope"] is None or bool(hit and hit["out_of_scope"] == e["out_of_scope"])
        rows.append({"id": e["id"], "found": bool(hit), "type_ok": type_ok, "out_of_scope_ok": oos_ok, "got": hit["change_type"] if hit else None})
    matched = {id(i) for e in ch["expected"] for i in items if any(k in f"{i['title']} {i['summary']}".lower() for k in e["keywords"])}
    results["changes"] = {
        "expected": len(rows),
        "correct_type": sum(r["type_ok"] for r in rows),
        "correct_out_of_scope_flag": sum(r["out_of_scope_ok"] and r["type_ok"] for r in rows),
        "spurious_changes": sum(1 for i in items if id(i) not in matched),
        "rows": rows,
        "seconds": round(time.perf_counter() - started, 2),
        "llm": v2["data"]["llm"],
    }
    out = HERE / "results"
    out.mkdir(exist_ok=True)
    (out / f"{args.label}.json").write_text(json.dumps(results, indent=2))
    print(json.dumps({k: v for k, v in results.items() if k != "transcripts"} | {
        "recall": {k: v["recall"] for k, v in results["transcripts"].items()},
        "quotes_verified": {k: v["quotes_verified"] for k, v in results["transcripts"].items()},
    }, indent=2, default=str))


if __name__ == "__main__":
    asyncio.run(main())
