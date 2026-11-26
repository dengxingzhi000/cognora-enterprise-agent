import argparse
import json
import os
import re
import sys
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from agent.assistance.model import (
    AssistanceModelError,
    fit_assistance_model,
    promote_active_model,
    rollback_active_model,
    save_bundle,
)
from agent.assistance.store import PostgresAssistanceStore
from infrastructure.pg.connector import PGConnector


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Train a tenant-scoped operator assistance model"
    )
    parser.add_argument("--tenant-id", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--promote", action="store_true")
    parser.add_argument("--rollback", action="store_true")
    parser.add_argument("--use-outcome-labels", action="store_true")
    parser.add_argument("--min-samples", type=int, default=200)
    parser.add_argument("--min-per-class", type=int, default=30)
    parser.add_argument("--false-positive-cost", type=float, default=1.0)
    parser.add_argument("--false-negative-cost", type=float, default=2.0)
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args(argv)
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", args.tenant_id):
        print("tenant-id contains unsupported characters", file=sys.stderr)
        return 2
    if args.promote and args.rollback:
        print("--promote and --rollback are mutually exclusive", file=sys.stderr)
        return 2
    manifest = Path(args.output_dir) / "active.json"
    if args.rollback:
        try:
            version = rollback_active_model(manifest, args.tenant_id)
            print(json.dumps({"tenant_id": args.tenant_id, "rolled_back_to": version}))
            return 0
        except (AssistanceModelError, OSError, ValueError) as exc:
            print(str(exc), file=sys.stderr)
            return 2
    dsn = os.getenv("AGENT_PG_DSN")
    if not dsn:
        print("AGENT_PG_DSN is required", file=sys.stderr)
        return 2
    try:
        connector = PGConnector(dsn)
        records = PostgresAssistanceStore(connector).training_records(
            args.tenant_id, include_outcome_labels=args.use_outcome_labels
        )
        version = f"tenant-{datetime.now(UTC):%Y%m%dT%H%M%SZ}-{uuid4().hex[:8]}"
        bundle, metrics = fit_assistance_model(
            records,
            model_version=version,
            training_source="operator_feedback",
            tenant_id=args.tenant_id,
            min_samples=args.min_samples,
            min_per_class=args.min_per_class,
            false_positive_cost=args.false_positive_cost,
            false_negative_cost=args.false_negative_cost,
            seed=args.seed,
        )
        output = Path(args.output_dir) / args.tenant_id / f"{version}.joblib"
        save_bundle(output, bundle)
        if args.promote:
            promote_active_model(manifest, args.tenant_id, version)
        print(
            json.dumps(
                {"artifact": str(output), "promoted": args.promote, "metrics": metrics},
                indent=2,
            )
        )
        return 0
    except (AssistanceModelError, OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except Exception as exc:
        print(f"training failed: {type(exc).__name__}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
