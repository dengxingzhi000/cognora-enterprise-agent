import json
from pathlib import Path

from agent.assistance.model import fit_assistance_model, generate_bootstrap_records, save_bundle


def main() -> int:
    records = generate_bootstrap_records(n=2400, seed=42)
    bundle, metrics = fit_assistance_model(
        records,
        model_version="bootstrap-v2",
        training_source="synthetic_bootstrap",
        min_samples=500,
        min_per_class=50,
        false_positive_cost=1.0,
        false_negative_cost=2.0,
        seed=42,
    )
    output = (
        Path(__file__).resolve().parents[1]
        / "agent"
        / "assistance"
        / "models"
        / "bootstrap-v2.joblib"
    )
    save_bundle(output, bundle)
    print(
        json.dumps(
            {"artifact": str(output), "bytes": output.stat().st_size, "metrics": metrics},
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
