"""CloudTrim command line.

    cloudtrim seed        # terraform-apply the wasteful demo account + billing
    cloudtrim audit       # scan + CUR + rules -> findings
    cloudtrim remediate   # apply Tier 0/1 findings, verify, re-audit
    cloudtrim report      # generate the client-ready PDF audit report
    cloudtrim demo        # seed -> audit -> remediate -> report (full story)
    cloudtrim serve       # run the FastAPI service for the dashboard
    cloudtrim status      # quick state dump
"""
from __future__ import annotations

import argparse
import json
import sys

from . import __version__, config, db


def cmd_seed(args) -> None:
    from . import seeder
    result = seeder.seed(force=True)
    print(json.dumps(result, indent=2))


def cmd_audit(args) -> None:
    from . import audit
    result = audit.run_audit(label=args.label)
    s = result["summary"]
    print(f"scan #{result['scan_id']}: {s['findings_total']} findings, "
          f"CUR ${s['cur_monthly']:,.2f}/mo, "
          f"applied-tier savings ${s['savings_applied_monthly']:,.2f}/mo "
          f"({s['reduction_if_applied_pct']}% reduction)")
    if args.json:
        print(json.dumps(result, indent=2))


def cmd_remediate(args) -> None:
    from . import remediate
    result = remediate.run_remediation(scan_id=args.scan_id)
    print(json.dumps(result, indent=2, default=str))


def cmd_report(args) -> None:
    from . import report
    path = report.generate()
    print(f"report written: {path}")


def cmd_demo(args) -> None:
    from . import audit, remediate, report, seeder
    print("=" * 70)
    print("CloudTrim end-to-end demo: seed -> audit -> remediate -> verify")
    print(f"target: {config.mode_label()}")
    print("=" * 70)
    seeder.seed(force=True)
    print("\n--- STAGE 2: AUDIT ---------------------------------------------\n")
    audit.run_audit(label="audit")
    print("\n--- STAGE 3: REMEDIATE (Tier 0/1, zero downtime) ---------------\n")
    remediate.run_remediation()
    print("\n--- STAGE 4: REPORT --------------------------------------------\n")
    report.generate()


def cmd_serve(args) -> None:
    import uvicorn
    from . import api
    uvicorn.run(api.app, host=args.host, port=args.port, log_level="info")


def cmd_status(args) -> None:
    db.init_db()
    print(f"cloudtrim {__version__} | {config.mode_label()}")
    print(f"db: {config.DB_PATH}")
    with db.SessionLocal() as s:
        scans = s.query(db.Scan).order_by(db.Scan.id).all()
        state = s.query(db.PipelineState).first()
    for sc in scans:
        summary = json.loads(sc.summary_json or "{}")
        print(f"  scan #{sc.id} [{sc.label}] {sc.phase} "
              f"CUR=${sc.cur_monthly:,.2f}/mo findings={summary.get('findings_total', '-')} "
              f"applied=${summary.get('savings_applied_monthly', 0):,.2f}/mo")
    if state:
        print(f"  pipeline: {state.step} — {state.detail}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="cloudtrim",
                                     description="CloudTrim cloud cost audit engine")
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("seed", help="terraform-apply the wasteful demo account").set_defaults(func=cmd_seed)
    p = sub.add_parser("audit", help="run a full audit pass")
    p.add_argument("--label", default="audit")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_audit)

    p = sub.add_parser("remediate", help="apply Tier 0/1 findings + re-audit")
    p.add_argument("--scan-id", type=int, default=None)
    p.set_defaults(func=cmd_remediate)

    sub.add_parser("report", help="generate the client PDF report").set_defaults(func=cmd_report)
    sub.add_parser("demo", help="full end-to-end story").set_defaults(func=cmd_demo)

    p = sub.add_parser("serve", help="run the dashboard API")
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--port", type=int, default=int(__import__("os").environ.get("PORT", "8000")))
    p.set_defaults(func=cmd_serve)

    sub.add_parser("status", help="show state").set_defaults(func=cmd_status)

    args = parser.parse_args(argv)
    args.func(args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
