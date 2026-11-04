"""只读运维视图：列出卡住的写工具 effect（stale started 且审批无活跃租约）。

用法::

    AGENT_PG_DSN=postgresql://... python scripts/ops/stuck_tool_effects.py
    python scripts/ops/stuck_tool_effects.py --tenant t1 --stale-seconds 120

选择维护脚本而不是 admin 接口：不新增网络面与鉴权面，应用起不来时也能直接连库
排查；只读查询，不打印明文参数或工具结果（args_hash 只显示前 8 位）。
"""
import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tenant", default=None, help="只显示指定 tenant_id")
    parser.add_argument("--stale-seconds", type=int, default=None,
                        help="覆盖 AGENT_TOOL_EFFECT_STALE_SECONDS")
    parser.add_argument("--limit", type=int, default=50)
    args = parser.parse_args()

    dsn = os.getenv("AGENT_PG_DSN")
    if not dsn:
        print("AGENT_PG_DSN is not set; nothing to query", file=sys.stderr)
        return 2

    from infrastructure.pg.connector import PGConnector
    from security.tool_effect import (
        PostgresToolEffectRepository,
        tool_effect_stale_seconds,
    )

    connector = PGConnector(dsn)
    repo = PostgresToolEffectRepository(connector)
    seconds = args.stale_seconds or tool_effect_stale_seconds()
    rows = repo.list_stale_started(seconds)
    if args.tenant:
        rows = [r for r in rows if r["tenant_id"] == args.tenant]
    rows = rows[: max(1, args.limit)]

    print(f"stale window: {seconds}s; stuck effects: {len(rows)}")
    if not rows:
        return 0
    header = ("tenant_id", "approval_id", "tool_name", "args_hash", "status", "attempt",
              "maybe_side_effect", "updated_at", "approval_status", "lease_until")
    print("\t".join(header))
    for row in rows:
        print("\t".join([
            str(row["tenant_id"]),
            str(row["approval_id"]),
            str(row["tool_name"]),
            str(row["args_hash"])[:8],
            str(row["status"]),
            str(row["attempt"]),
            "true" if row["maybe_side_effect"] else "false",
            row["updated_at"].isoformat() if row.get("updated_at") else "-",
            str(row.get("approval_status") or "-"),
            row["approval_lease_until"].isoformat() if row.get("approval_lease_until") else "-",
        ]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
