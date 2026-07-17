from __future__ import annotations

import argparse
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import uvicorn

from vonavy_agent.api import create_app, migrate
from vonavy_agent.jobs import Worker
from vonavy_agent.persistence import create_db_engine
from vonavy_agent.settings import Settings


def _settings(args: argparse.Namespace) -> Settings:
    if getattr(args, "managed_root", None):
        return Settings(managed_root=Path(args.managed_root))
    return Settings()


def demo_data(destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    start = date(2025, 1, 1)
    rows: list[dict[str, object]] = []
    for entity_index, entity in enumerate(("store-a", "store-b")):
        for offset in range(210):
            day = start + timedelta(days=offset)
            weekday = day.weekday()
            promotion = int((offset + entity_index) % 17 == 0)
            demand = (
                90
                + entity_index * 20
                + weekday * 3
                + promotion * 25
                + ((offset * 7 + entity_index * 3) % 11)
            )
            rows.append(
                {
                    "date": day.isoformat(),
                    "store": entity,
                    "demand": float(demand),
                    "promotion": promotion,
                    "region": "north" if entity_index == 0 else "south",
                }
            )
    pd.DataFrame(rows).to_csv(destination, index=False)


def main() -> None:
    parser = argparse.ArgumentParser(prog="vonavy-agent")
    subparsers = parser.add_subparsers(dest="command", required=True)
    serve = subparsers.add_parser("serve")
    serve.add_argument("--managed-root")
    serve.add_argument("--host")
    serve.add_argument("--port", type=int)
    worker = subparsers.add_parser("worker")
    worker.add_argument("--managed-root")
    migration = subparsers.add_parser("migrate")
    migration.add_argument("--managed-root")
    demo = subparsers.add_parser("demo-data")
    demo.add_argument("destination", type=Path)
    args = parser.parse_args()
    if args.command == "demo-data":
        demo_data(args.destination)
        print(f"Wrote synthetic daily panel data to {args.destination}")
        return
    settings = _settings(args)
    migrate(settings)
    if args.command == "migrate":
        print(f"Database is current at {settings.database_path}")
        return
    if args.command == "worker":
        Worker(settings, create_db_engine(settings.database_path)).run_forever()
        return
    host = args.host or settings.host
    if host not in {"127.0.0.1", "localhost", "::1"}:
        raise SystemExit("Refusing non-loopback bind; set up an explicit reverse proxy if required")
    uvicorn.run(create_app(settings), host=host, port=args.port or settings.port)


if __name__ == "__main__":
    main()
