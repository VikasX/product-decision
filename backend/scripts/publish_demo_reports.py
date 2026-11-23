"""Export genuine cached reports; paid generation requires the explicit --generate option."""

import argparse
import asyncio
import sqlite3
import sys
import uuid
from pathlib import Path

# Running this file from backend/ should resolve the same application modules as uvicorn.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import Settings
from app.schemas.analysis import AnalysisResponse
from app.services.demo import DemoAnalysisService, DemoCatalog
from app.services.saved_reports import SavedReportCatalog
from app.services.usage import create_usage_store, credential_fingerprint


def cached_report(config, key, source):
    # Export can reuse an expired genuine report: analysis.json is permanent and validated against its evidence.
    if source == "sqlite":
        if not config.ai_state_path.is_file():
            return None
        with sqlite3.connect(config.ai_state_path.as_uri() + "?mode=ro", uri=True) as db:
            row = db.execute("SELECT body FROM cache WHERE key=?", (key,)).fetchone()
        return AnalysisResponse.model_validate_json(row[0]) if row else None
    from app.services.mongo_usage import MongoUsageStore
    store = MongoUsageStore(config)
    try:
        row = store.state.find_one({"_id": store.state_id(key), "cache.key": key}, {"cache.body": 1})
        return AnalysisResponse.model_validate_json(row["cache"]["body"]) if row else None
    finally:
        store.close()


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("product_ids", nargs="*", help="e.g. prd3 prd9; omitted means all products")
    parser.add_argument("--source", choices=["sqlite", "mongodb"], default="sqlite")
    parser.add_argument("--workflow", choices=["httpx", "langchain"], default="httpx")
    parser.add_argument("--generate", action="store_true", help="Explicitly permit one guarded paid attempt per missing product")
    args = parser.parse_args()
    config = Settings()
    if config.vercel:
        parser.error("Run this private command locally, never in Vercel")
    datasets = [d for _, d in DemoCatalog().datasets()]
    if set(args.product_ids) - {d.id for d in datasets}:
        parser.error("Unknown product ID")
    published = SavedReportCatalog()
    store = None
    missing = []
    try:
        for dataset in datasets:
            if args.product_ids and dataset.id not in args.product_ids:
                continue
            try:
                published.report_for(dataset)
                print(dataset.id, "already published")
                continue
            except (OSError, ValueError):
                pass
            service = DemoAnalysisService(config, store=object(), dataset=dataset, workflow=args.workflow)
            report = cached_report(config, service.cache_key(dataset.product.asin), args.source)
            if report is not None:
                try:
                    published.publish(dataset, report, model=config.openai_model, workflow=args.workflow)
                    print(dataset.id, "published from existing cache; no AI call")
                    continue
                except ValueError:
                    # A cached technical/insufficient report is not a substitute for completed analysis.
                    pass
            if args.generate:
                # No loop/retry around a model attempt. Existing shared caps and adapter validation remain active.
                store = store or create_usage_store(config)
                service = DemoAnalysisService(config, store=store, dataset=dataset, workflow=args.workflow)
                # A cached insufficient result is not a completed report. This explicit command
                # permits one fresh guarded attempt, rather than reusing that old failure forever.
                key = service.cache_key(dataset.product.asin)
                owner = uuid.uuid4().hex
                if not store.acquire(key, owner):
                    missing.append(dataset.id)
                    print(dataset.id, "generation already running; no duplicate attempt")
                    continue
                try:
                    report, verified = await service.run_analysis(dataset.product.url, dataset.product.asin)
                    cache_key = key if verified else f"{key}:failure:{credential_fingerprint(config)}"
                    store.finish(key, owner, cache_key, report, ttl=86400 if verified else 60)
                finally:
                    store.release(key, owner)
                try:
                    published.publish(dataset, report, model=config.openai_model, workflow=args.workflow)
                    print(dataset.id, "published after private generation")
                    continue
                except ValueError:
                    pass
            missing.append(dataset.id)
            print(dataset.id, "not published; completed matching report required")
    finally:
        if store is not None:
            store.close()
    # Useful for CI/manual checks without changing or resetting any spending ledger.
    return 1 if missing else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
