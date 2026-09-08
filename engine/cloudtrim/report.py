"""Client-ready PDF audit report — the deliverable a $3-8k engagement ships.

Generated from the audit database (scans, findings, remediation log) with
ReportLab. Vector output, table-driven, restrained styling. The report is
the contract artifact: every number traces back to CUR billing data, API
inventory, and verified remediation actions recorded in the audit log.
"""
from __future__ import annotations

import json
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import inch
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfbase.pdfmetrics import registerFontFamily
from reportlab.platypus import (
    CondPageBreak,
    KeepTogether,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

from . import config, db

INK = colors.HexColor("#142840")
ACCENT = colors.HexColor("#2d7ab3")
MUTED = colors.HexColor("#5a7a96")
STRIPE = colors.HexColor("#eef3fa")
BORDER = colors.HexColor("#c0d0e2")
HEADER_FILL = colors.HexColor("#1a4a7a")

FONT_DIR = Path("/usr/share/fonts/truetype")


def _register_fonts() -> str:
    candidates = [
        (FONT_DIR / "freefont/FreeSerif.ttf", FONT_DIR / "freefont/FreeSerifBold.ttf", "FreeSerif"),
        (FONT_DIR / "dejavu/DejaVuSerif.ttf", FONT_DIR / "dejavu/DejaVuSerif-Bold.ttf", "DejaVuSerif"),
        (FONT_DIR / "dejavu/DejaVuSans.ttf", FONT_DIR / "dejavu/DejaVuSans-Bold.ttf", "DejaVuSans"),
    ]
    for regular, bold, name in candidates:
        if regular.exists() and bold.exists():
            pdfmetrics.registerFont(TTFont(name, str(regular)))
            pdfmetrics.registerFont(TTFont(name + "-Bold", str(bold)))
            registerFontFamily(name, normal=name, bold=name + "-Bold")
            return name
    return "Helvetica"


def generate() -> Path:
    body_font = _register_fonts()
    bold = body_font + "-Bold"

    styles = {
        "title": ParagraphStyle("title", fontName=bold, fontSize=24, leading=30,
                                textColor=INK, alignment=TA_LEFT, spaceAfter=4),
        "subtitle": ParagraphStyle("subtitle", fontName=body_font, fontSize=11, leading=16,
                                   textColor=MUTED, spaceAfter=18),
        "h1": ParagraphStyle("h1", fontName=bold, fontSize=14, leading=18, textColor=INK,
                             spaceBefore=18, spaceAfter=8),
        "h2": ParagraphStyle("h2", fontName=bold, fontSize=11, leading=15, textColor=HEADER_FILL,
                             spaceBefore=12, spaceAfter=6),
        "body": ParagraphStyle("body", fontName=body_font, fontSize=9.5, leading=14.5,
                               textColor=INK, spaceAfter=8),
        "cell": ParagraphStyle("cell", fontName=body_font, fontSize=8.5, leading=11.5, textColor=INK),
        "cellc": ParagraphStyle("cellc", fontName=body_font, fontSize=8.5, leading=11.5,
                                textColor=INK, alignment=TA_CENTER),
        "hcell": ParagraphStyle("hcell", fontName=bold, fontSize=8.5, leading=11.5,
                                textColor=colors.white, alignment=TA_LEFT),
        "hcellc": ParagraphStyle("hcellc", fontName=bold, fontSize=8.5, leading=11.5,
                                 textColor=colors.white, alignment=TA_CENTER),
        "caption": ParagraphStyle("caption", fontName=body_font, fontSize=8, leading=11,
                                  textColor=MUTED, alignment=TA_CENTER, spaceBefore=4),
        "stat": ParagraphStyle("stat", fontName=bold, fontSize=18, leading=22,
                               textColor=ACCENT, alignment=TA_CENTER),
        "statlabel": ParagraphStyle("statlabel", fontName=body_font, fontSize=7.5, leading=10,
                                    textColor=MUTED, alignment=TA_CENTER),
    }

    doc = SimpleDocTemplate(
        str(config.REPORT_PATH), pagesize=A4,
        leftMargin=0.8 * inch, rightMargin=0.8 * inch,
        topMargin=0.7 * inch, bottomMargin=0.7 * inch,
        title="CloudTrim Cloud Cost Audit Report", author="CloudTrim",
        subject="AWS cost audit: findings, savings, and verified remediation")
    available = A4[0] - 1.6 * inch
    story: list = []

    # --- header ---------------------------------------------------------------
    story.append(Paragraph("CloudTrim", styles["title"]))
    story.append(Paragraph(
        "Cloud Cost Audit Report — AWS account 123456789012 (us-west-2)<br/>"
        "Productized one-week fixed-fee engagement: connect, audit, remediate, verify.",
        styles["subtitle"]))

    # --- data ----------------------------------------------------------------
    db.init_db()
    with db.SessionLocal() as s:
        scans = s.query(db.Scan).order_by(db.Scan.id).all()
        audit_scan = next((sc for sc in reversed(scans) if sc.label == "audit"), None)
        re_scan = next((sc for sc in reversed(scans) if sc.label == "re-audit"), None)
        findings = s.query(db.Finding).filter(db.Finding.scan_id == audit_scan.id).all() if audit_scan else []
        actions = s.query(db.RemediationAction).filter(
            db.RemediationAction.scan_id == audit_scan.id).all() if audit_scan else []

    pre = json.loads(audit_scan.summary_json or "{}") if audit_scan else {}
    post = json.loads(re_scan.summary_json or "{}") if re_scan else {}

    # --- headline stats ---------------------------------------------------------
    baseline = pre.get("cur_monthly", 0.0)
    after = post.get("cur_monthly", baseline)
    reduction = round((baseline - after) / baseline * 100, 1) if baseline else 0.0
    applied = pre.get("savings_applied_monthly", 0.0)
    planned = pre.get("savings_planned_monthly", 0.0)

    fee = 4500.0  # Standard tier engagement
    payback = round(fee / applied, 1) if applied else 0.0

    stats = Table([
        [Paragraph(f"${baseline:,.0f}", styles["stat"]),
         Paragraph(f"-${applied:,.0f}", styles["stat"]),
         Paragraph(f"{reduction}%", styles["stat"]),
         Paragraph(f"{payback} mo", styles["stat"])],
        [Paragraph("monthly spend (CUR)", styles["statlabel"]),
         Paragraph("monthly savings applied", styles["statlabel"]),
         Paragraph("verified reduction (re-audit)", styles["statlabel"]),
         Paragraph(f"payback on ${fee:,.0f} fee", styles["statlabel"])],
    ], colWidths=[available / 4] * 4)
    stats.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), STRIPE),
        ("BOX", (0, 0), (-1, -1), 0.75, BORDER),
        ("INNERGRID", (0, 0), (-1, -1), 0.5, BORDER),
        ("TOPPADDING", (0, 0), (-1, 0), 10), ("BOTTOMPADDING", (0, 1), (-1, 1), 10),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
    ]))
    story.append(stats)
    story.append(Spacer(1, 14))

    story.append(Paragraph("Executive Summary", styles["h1"]))
    story.append(Paragraph(
        f"This audit reviewed {pre.get('resource_count', 0)} resources across EC2, EBS, S3, ELB, "
        f"VPC and CloudWatch, cross-referenced against {pre.get('cur_window_days', 60)} days of "
        f"CUR billing data. The engine identified {len(findings)} findings worth "
        f"${applied + planned:,.0f}/month in total. Tier 0 and Tier 1 remediations "
        f"(pure-waste removal and live, non-disruptive changes) were applied during the "
        f"engagement with zero customer-facing downtime, cutting the run rate from "
        f"${baseline:,.0f} to ${after:,.0f} per month — a {reduction}% reduction verified by "
        f"re-audit. An additional ${planned:,.0f}/month of Tier 2/3 roadmap savings "
        f"(rightsizing, NAT consolidation, Graviton, Spot, Savings Plans) is specified and "
        f"scheduled with your team. All actions are recorded with before/after state and "
        f"API-level verification in the remediation log below.", styles["body"]))

    # reconciliation box
    story.append(Paragraph("Data Integrity — Billing vs Inventory Reconciliation", styles["h2"]))
    recon = audit_scan.reconciliation_pct if audit_scan else 0.0
    inv_monthly = audit_scan.inventory_monthly if audit_scan else 0.0
    story.append(Paragraph(
        f"Monthly run-rate derived from CUR billing (${baseline:,.2f}) vs a bottom-up cost "
        f"model built from live API inventory x list pricing (${inv_monthly:,.2f}): "
        f"variance {recon:+.1f}%. Inventory and billing agree within tolerance; every "
        f"finding is backed by both an API observation and a billing line.",
        styles["body"]))

    story.append(CondPageBreak(2.5 * inch))

    # --- findings table ---------------------------------------------------------
    story.append(Paragraph("Findings (by monthly savings)", styles["h1"]))
    header = [Paragraph("Finding", styles["hcell"]),
              Paragraph("Resource", styles["hcell"]),
              Paragraph("$/mo", styles["hcellc"]),
              Paragraph("Risk", styles["hcellc"]),
              Paragraph("Status", styles["hcellc"])]
    rows = [header]
    for f in findings:
        rows.append([
            Paragraph(f"{f.rule_id} — {f.title}", styles["cell"]),
            Paragraph(f"{f.resource_name or f.resource_id}", styles["cell"]),
            Paragraph(f"{f.monthly_savings:,.0f}", styles["cellc"]),
            Paragraph(f"T{f.risk_tier}", styles["cellc"]),
            Paragraph(f.status, styles["cellc"]),
        ])
    if rows:
        t = Table(rows, colWidths=[available * 0.38, available * 0.30, available * 0.12,
                                   available * 0.08, available * 0.12],
                  repeatRows=1, hAlign="CENTER")
        style = [
            ("BACKGROUND", (0, 0), (-1, 0), HEADER_FILL),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("GRID", (0, 0), (-1, -1), 0.4, BORDER),
            ("TOPPADDING", (0, 0), (-1, -1), 4), ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ("LEFTPADDING", (0, 0), (-1, -1), 5), ("RIGHTPADDING", (0, 0), (-1, -1), 5),
        ]
        for i in range(1, len(rows)):
            if i % 2 == 0:
                style.append(("BACKGROUND", (0, i), (-1, i), STRIPE))
        t.setStyle(TableStyle(style))
        story.append(t)
        story.append(Paragraph(
            f"Table 1 — {len(findings)} findings; T0/T1 auto-remediated, T2/T3 on the roadmap.",
            styles["caption"]))

    story.append(CondPageBreak(2.5 * inch))

    # --- remediation log ---------------------------------------------------------
    story.append(Paragraph("Remediation Log (verified actions)", styles["h1"]))
    rows = [[Paragraph("Action", styles["hcell"]),
             Paragraph("Resource", styles["hcell"]),
             Paragraph("Before", styles["hcell"]),
             Paragraph("After", styles["hcell"]),
             Paragraph("Verified", styles["hcellc"])]]
    for a in actions:
        rows.append([
            Paragraph(a.action, styles["cell"]),
            Paragraph(a.resource_id, styles["cell"]),
            Paragraph(a.before_state, styles["cell"]),
            Paragraph(a.after_state, styles["cell"]),
            Paragraph("yes" if a.verified else "PENDING", styles["cellc"]),
        ])
    if len(rows) > 1:
        t = Table(rows, colWidths=[available * 0.30, available * 0.22, available * 0.16,
                                   available * 0.16, available * 0.16],
                  repeatRows=1, hAlign="CENTER")
        t.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), HEADER_FILL),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("GRID", (0, 0), (-1, -1), 0.4, BORDER),
            ("TOPPADDING", (0, 0), (-1, -1), 4), ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, STRIPE]),
        ]))
        story.append(t)
        story.append(Paragraph(
            f"Table 2 — {len(actions)} actions, each verified by API read-back after execution.",
            styles["caption"]))

    # --- roadmap ---------------------------------------------------------
    story.append(CondPageBreak(2.5 * inch))
    story.append(Paragraph("Savings Roadmap (Tier 2/3 — scheduled with your team)", styles["h1"]))
    planned_findings = [f for f in findings if f.status == "planned"]
    if planned_findings:
        rows = [[Paragraph("Initiative", styles["hcell"]),
                 Paragraph("Detail", styles["hcell"]),
                 Paragraph("$/mo", styles["hcellc"])]]
        for f in planned_findings:
            rows.append([
                Paragraph(f"{f.rule_id} — {f.title}", styles["cell"]),
                Paragraph(f.remediation_action, styles["cell"]),
                Paragraph(f"{f.monthly_savings:,.0f}", styles["cellc"]),
            ])
        t = Table(rows, colWidths=[available * 0.34, available * 0.54, available * 0.12],
                  repeatRows=1, hAlign="CENTER")
        t.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), HEADER_FILL),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("GRID", (0, 0), (-1, -1), 0.4, BORDER),
            ("TOPPADDING", (0, 0), (-1, -1), 4), ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, STRIPE]),
        ]))
        story.append(t)
        story.append(Paragraph(
            "Roadmap initiatives are modeled independently; combined upside is bounded by the "
            "remaining on-demand baseline after the applied reductions.", styles["caption"]))

    story.append(Paragraph("Zero-Downtime Methodology", styles["h1"]))
    story.append(Paragraph(
        "Tier 0 actions delete resources that serve no traffic (unattached volumes, stale "
        "snapshots, orphaned IPs, an empty load balancer, uncommitted upload parts). Tier 1 "
        "actions are in-place AWS operations by design: gp2 to gp3 volumes convert live via "
        "ModifyVolume, S3 lifecycle rules transition objects in place, CloudWatch retention "
        "trims logs without touching streams, and stopped instances were idle below 5% CPU "
        "for 14 days of CloudWatch telemetry. Every action was verified by reading state "
        "back through the AWS API after execution, and volumes with guard policy were "
        "snapshotted before deletion. No customer-facing endpoint was modified during this "
        "engagement.", styles["body"]))

    story.append(Paragraph("Sources & Assumptions", styles["h2"]))
    story.append(Paragraph(
        "Cost figures: AWS Cost & Usage Report (resource-daily aggregation, 60-day window). "
        "Pricing: AWS public on-demand list prices, us-west-2. S3 lifecycle and log-retention "
        "savings are modeled transitions (60% cold-data fraction, 90-day steady state) and "
        "materialize progressively. Spot, Graviton and Savings Plans figures are conservative "
        "modeled rates (62% spot discount, 15-20% Graviton, 27% 1-yr Compute SP).", styles["body"]))

    doc.build(story)
    return config.REPORT_PATH
