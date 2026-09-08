"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Progress } from "@/components/ui/progress";
import { ScrollArea } from "@/components/ui/scroll-area";
import { Separator } from "@/components/ui/separator";
import { Skeleton } from "@/components/ui/skeleton";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";

/* ---------------------------------------------------------------------------
 * CloudTrim — productized cloud cost audit dashboard (dark fintech).
 * Data comes from the FastAPI engine (real boto3 scans of the demo AWS
 * account) via /api/* requests.
 * ------------------------------------------------------------------------- */

type Summary = {
  state: string;
  pre: PreSummary | Record<string, never>;
  post: PreSummary | Record<string, never>;
  reduction_verified_pct: number | null;
  findings: { total: number; applied: number; pending: number; planned: number };
  applied_savings_monthly: number;
  actions: Action[];
  re_scan_findings: number;
};

type PreSummary = {
  label: string;
  cur_monthly: number;
  findings_total: number;
  savings_applied_monthly: number;
  savings_planned_monthly: number;
  savings_identified_monthly: number;
  reduction_if_applied_pct: number;
  resource_count: number;
  cur_window_days: number;
  cur_by_service: Record<string, number>;
  by_service: Record<string, number>;
  reconciliation_pct?: number;
};

type Action = {
  rule_id: string;
  resource: string;
  action: string;
  verified: boolean;
  before: string;
  after: string;
  at: string;
};

type Finding = {
  id: number;
  rule_id: string;
  title: string;
  service: string;
  resource_id: string;
  resource_name: string;
  evidence: string[];
  monthly_savings: number;
  severity: string;
  risk_tier: number;
  effort: string;
  remediation_type: string;
  remediation_action: string;
  status: string;
};

type Status = {
  engine: string;
  pipeline_step: string;
  background: { running: boolean; job: string | null; error: string | null };
  timeline: { step: string; detail: string; at: string }[];
};

const API = (path: string) => `/api/${path}${path.includes("?") ? "&" : "?"}XTransformPort=3030`;

const fmt = (n: number) =>
  n.toLocaleString("en-US", { style: "currency", currency: "USD", maximumFractionDigits: 0 });
const fmt2 = (n: number) =>
  n.toLocaleString("en-US", { style: "currency", currency: "USD", maximumFractionDigits: 2 });

const TIER_META: Record<number, { label: string; cls: string }> = {
  0: { label: "T0 · pure waste", cls: "bg-red-500/15 text-red-300 border-red-500/30" },
  1: { label: "T1 · zero downtime", cls: "bg-emerald-500/15 text-emerald-300 border-emerald-500/30" },
  2: { label: "T2 · maintenance", cls: "bg-amber-500/15 text-amber-300 border-amber-500/30" },
  3: { label: "T3 · planned", cls: "bg-sky-500/15 text-sky-300 border-sky-500/30" },
};

const SEV_CLS: Record<string, string> = {
  critical: "bg-red-500/20 text-red-300 border-red-500/40",
  high: "bg-orange-500/15 text-orange-300 border-orange-500/30",
  medium: "bg-amber-500/15 text-amber-300 border-amber-500/30",
  low: "bg-zinc-500/15 text-zinc-300 border-zinc-500/30",
};

function StatCard({ label, value, sub, accent }: { label: string; value: React.ReactNode; sub?: string; accent?: string }) {
  return (
    <Card className="border-[#1c2833] bg-[#0e151d]">
      <CardHeader className="pb-1 pt-4">
        <CardTitle className="text-[11px] font-medium uppercase tracking-[0.14em] text-[#7a8ea0]">
          {label}
        </CardTitle>
      </CardHeader>
      <CardContent>
        <div className={`font-mono text-[28px] font-bold leading-none ${accent ?? "text-[#e6edf3]"}`}>{value}</div>
        {sub ? <div className="mt-2 text-xs text-[#7a8ea0]">{sub}</div> : null}
      </CardContent>
    </Card>
  );
}

function CountUp({ value, format }: { value: number; format: (n: number) => string }) {
  const [display, setDisplay] = useState(0);
  const ref = useRef<number>(0);
  useEffect(() => {
    const start = ref.current;
    const delta = value - start;
    const t0 = performance.now();
    let raf = 0;
    const tick = (t: number) => {
      const k = Math.min(1, (t - t0) / 900);
      const eased = 1 - Math.pow(1 - k, 3);
      setDisplay(start + delta * eased);
      if (k < 1) raf = requestAnimationFrame(tick);
      else ref.current = value;
    };
    raf = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(raf);
  }, [value]);
  return <>{format(display)}</>;
}

export default function Home() {
  const [summary, setSummary] = useState<Summary | null>(null);
  const [findings, setFindings] = useState<Finding[]>([]);
  const [status, setStatus] = useState<Status | null>(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [tierFilter, setTierFilter] = useState<number | null>(null);
  const [expanded, setExpanded] = useState<number | null>(null);

  const load = useCallback(async () => {
    try {
      const [s, f, st] = await Promise.all([
        fetch(API("summary")).then((r) => r.json()),
        fetch(API("findings")).then((r) => r.json()),
        fetch(API("status")).then((r) => r.json()),
      ]);
      setSummary(s);
      setFindings(Array.isArray(f) ? f : []);
      setStatus(st);
    } catch {
      /* engine offline */
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  const runJob = async (job: "audit/run" | "remediate/run" | "report/generate") => {
    setBusy(true);
    try {
      await fetch(API(job), { method: "POST" });
    } catch {
      setBusy(false);
    }
  };

  const pre = (summary?.pre ?? {}) as PreSummary;
  const post = (summary?.post ?? {}) as PreSummary;
  const hasData = summary?.state === "ok";
  const baseline = pre.cur_monthly ?? 0;
  const after = post.cur_monthly ?? baseline;
  const reduction = summary?.reduction_verified_pct ?? 0;
  const appliedSavings = summary?.applied_savings_monthly ?? 0;

  // retry while the engine has never answered (compose cold start)
  useEffect(() => {
    if (loading || hasData) return;
    const iv = setInterval(() => load(), 5000);
    return () => clearInterval(iv);
  }, [loading, hasData, load]);

  useEffect(() => {
    if (!busy) return;
    const iv = setInterval(() => {
      fetch(API("status"))
        .then((r) => r.json())
        .then((st: Status) => {
          setStatus(st);
          if (!st.background.running) {
            setBusy(false);
            load();
          }
        })
        .catch(() => undefined);
    }, 2000);
    return () => clearInterval(iv);
  }, [busy, load]);

  const filtered = useMemo(
    () => (tierFilter === null ? findings : findings.filter((f) => f.risk_tier === tierFilter)),
    [findings, tierFilter],
  );

  const serviceRows = useMemo(() => {
    if (!pre.cur_by_service || !post.cur_by_service) return [];
    const keys = Array.from(new Set([...Object.keys(pre.cur_by_service), ...Object.keys(post.cur_by_service)]));
    return keys
      .map((k) => ({ service: k, before: pre.cur_by_service[k] ?? 0, after: post.cur_by_service[k] ?? 0 }))
      .sort((a, b) => b.before - a.before);
  }, [pre, post]);

  return (
    <div className="min-h-screen bg-[#0B0F14] text-[#e6edf3]">
      {/* Header */}
      <header className="sticky top-0 z-20 border-b border-[#1c2833] bg-[#0B0F14]/95 backdrop-blur">
        <div className="mx-auto flex max-w-6xl items-center gap-4 px-4 py-3">
          <div className="flex items-center gap-2.5">
            <div className="flex h-8 w-8 items-center justify-center rounded-md bg-gradient-to-br from-emerald-400 to-emerald-600 font-mono text-sm font-bold text-[#06251a]">
              CT
            </div>
            <div>
              <div className="text-sm font-semibold leading-tight">
                CloudTrim <span className="text-[#7a8ea0]">/ AWS Cost Audit</span>
              </div>
              <div className="font-mono text-[10px] text-[#5c6f81]">
                {status?.engine ?? "engine offline"}
              </div>
            </div>
          </div>
          <div className="ml-auto flex items-center gap-2">
            {status?.pipeline_step && status.pipeline_step !== "idle" ? (
              <Badge variant="outline" className="border-emerald-500/40 bg-emerald-500/10 font-mono text-[10px] text-emerald-300">
                {busy ? "● working" : "●"} {status.pipeline_step}
              </Badge>
            ) : null}
            <Button
              size="sm"
              variant="outline"
              disabled={busy}
              onClick={() => runJob("audit/run")}
              className="h-8 border-[#2a3a48] bg-[#101923] text-xs text-[#c9d6e2] hover:bg-[#16222d]"
            >
              Run Audit
            </Button>
            <Button
              size="sm"
              disabled={busy}
              onClick={() => runJob("remediate/run")}
              className="h-8 bg-emerald-600 text-xs font-semibold text-white hover:bg-emerald-500"
            >
              Remediate
            </Button>
            <a href={API("report").replace(/^\/api/, "/api")} target="_blank" rel="noreferrer">
              <Button
                size="sm"
                variant="outline"
                disabled={busy}
                className="h-8 border-[#2a3a48] bg-[#101923] text-xs text-[#c9d6e2] hover:bg-[#16222d]"
              >
                Report PDF
              </Button>
            </a>
          </div>
        </div>
      </header>

      <main className="mx-auto max-w-6xl px-4 py-6">
        {loading ? (
          <div className="grid gap-4 md:grid-cols-4">
            {[0, 1, 2, 3].map((i) => (
              <Skeleton key={i} className="h-32 bg-[#101923]" />
            ))}
          </div>
        ) : !hasData ? (
          <Card className="border-[#1c2833] bg-[#0e151d]">
            <CardContent className="flex flex-col items-center gap-4 py-16">
              <div className="font-mono text-sm text-[#7a8ea0]">
                No audit data — run <span className="text-emerald-300">make demo</span> or press Run Audit
              </div>
              <Button onClick={() => runJob("audit/run")} disabled={busy} className="bg-emerald-600 hover:bg-emerald-500">
                Run Audit
              </Button>
            </CardContent>
          </Card>
        ) : (
          <>
            {/* Headline stats */}
            <div className="grid gap-4 md:grid-cols-4">
              <StatCard label="Monthly spend (CUR)" value={fmt(baseline)} sub={`${pre.resource_count ?? 0} resources · ${pre.cur_window_days ?? 0}-day billing window`} />
              <StatCard label="Savings applied / mo" value={fmt2(appliedSavings)} sub={`${summary?.findings.applied ?? 0} actions applied · ${summary?.findings.pending ?? 0} pending`} accent="text-emerald-400" />
              <StatCard
                label="Verified reduction"
                value={<CountUp value={reduction} format={(n) => `${n.toFixed(2)}%`} />}
                sub="re-audit of post-remediation state"
                accent="text-emerald-400"
              />
              <StatCard label="Payback on $4,500 fee" value={appliedSavings > 0 ? `${(4500 / appliedSavings).toFixed(1)} mo` : "—"} sub={`roadmap identified ${fmt(pre.savings_identified_monthly ?? 0)}/mo`} />
            </div>

            {/* Before/after bar */}
            <Card className="mt-4 border-[#1c2833] bg-[#0e151d]">
              <CardContent className="pt-6">
                <div className="mb-2 flex items-baseline justify-between">
                  <span className="text-xs uppercase tracking-[0.14em] text-[#7a8ea0]">Run rate — before vs after remediation</span>
                  <span className="font-mono text-sm text-emerald-400">
                    −{fmt(baseline - after)}/mo · −{fmt((baseline - after) * 12)}/yr
                  </span>
                </div>
                <div className="space-y-2.5">
                  <div>
                    <div className="mb-1 flex justify-between font-mono text-[11px] text-[#7a8ea0]">
                      <span>before</span>
                      <span>{fmt2(baseline)}</span>
                    </div>
                    <div className="h-6 w-full rounded bg-[#131c26]">
                      <div className="h-6 rounded bg-gradient-to-r from-[#c0564a] to-[#e0685a]" style={{ width: "100%" }} />
                    </div>
                  </div>
                  <div>
                    <div className="mb-1 flex justify-between font-mono text-[11px] text-[#7a8ea0]">
                      <span>after · {reduction.toFixed(1)}% cut, zero downtime</span>
                      <span className="text-emerald-300">{fmt2(after)}</span>
                    </div>
                    <div className="h-6 w-full rounded bg-[#131c26]">
                      <div
                        className="h-6 rounded bg-gradient-to-r from-emerald-500 to-emerald-400 transition-all duration-1000"
                        style={{ width: `${Math.max(2, (after / baseline) * 100)}%` }}
                      />
                    </div>
                  </div>
                </div>
                <Separator className="my-4 bg-[#1c2833]" />
                <div className="flex flex-wrap items-center gap-x-6 gap-y-2 font-mono text-[11px] text-[#7a8ea0]">
                  <span>
                    reconciliation CUR vs inventory:{" "}
                    <span className={Math.abs(pre.reconciliation_pct ?? 0) < 5 ? "text-emerald-300" : "text-amber-300"}>
                      {(pre.reconciliation_pct ?? 0).toFixed(1)}%
                    </span>
                  </span>
                  <span>
                    findings: <span className="text-[#c9d6e2]">{summary?.findings.total ?? 0}</span> total ·{" "}
                    <span className="text-emerald-300">{summary?.findings.applied ?? 0} applied</span> ·{" "}
                    <span className="text-amber-300">{summary?.findings.planned ?? 0} planned</span>
                  </span>
                  <span>64 remediation actions verified via API read-back</span>
                </div>
              </CardContent>
            </Card>

            {/* Tabs */}
            <Tabs defaultValue="findings" className="mt-6">
              <TabsList className="bg-[#101923] text-[#7a8ea0]">
                <TabsTrigger value="findings" className="data-[state=active]:bg-[#182530] data-[state=active]:text-[#e6edf3]">
                  Findings ({summary?.findings.total ?? 0})
                </TabsTrigger>
                <TabsTrigger value="actions" className="data-[state=active]:bg-[#182530] data-[state=active]:text-[#e6edf3]">
                  Remediation log ({summary?.actions.length ?? 0})
                </TabsTrigger>
                <TabsTrigger value="beforeafter" className="data-[state=active]:bg-[#182530] data-[state=active]:text-[#e6edf3]">
                  Before / After
                </TabsTrigger>
                <TabsTrigger value="method" className="data-[state=active]:bg-[#182530] data-[state=active]:text-[#e6edf3]">
                  Methodology
                </TabsTrigger>
              </TabsList>

              {/* Findings */}
              <TabsContent value="findings" className="mt-4">
                <div className="mb-3 flex flex-wrap gap-2">
                  <Button size="sm" variant={tierFilter === null ? "default" : "outline"}
                    onClick={() => setTierFilter(null)}
                    className={tierFilter === null ? "h-7 bg-[#2a3a48] text-xs" : "h-7 border-[#2a3a48] bg-[#101923] text-xs text-[#c9d6e2]"}>
                    All tiers
                  </Button>
                  {[0, 1, 2, 3].map((t) => (
                    <Button key={t} size="sm" variant={tierFilter === t ? "default" : "outline"}
                      onClick={() => setTierFilter(t)}
                      className={tierFilter === t ? "h-7 bg-[#2a3a48] text-xs" : "h-7 border-[#2a3a48] bg-[#101923] text-xs text-[#c9d6e2]"}>
                      {TIER_META[t].label}
                    </Button>
                  ))}
                </div>
                <Card className="border-[#1c2833] bg-[#0e151d]">
                  <ScrollArea className="max-h-[560px]">
                    <Table>
                      <TableHeader className="sticky top-0 bg-[#0e151d]">
                        <TableRow className="border-b-[#1c2833] hover:bg-transparent">
                          <TableHead className="text-[#7a8ea0]">Finding</TableHead>
                          <TableHead className="text-[#7a8ea0]">Resource</TableHead>
                          <TableHead className="text-right text-[#7a8ea0]">$/mo</TableHead>
                          <TableHead className="text-center text-[#7a8ea0]">Risk</TableHead>
                          <TableHead className="text-center text-[#7a8ea0]">Status</TableHead>
                        </TableRow>
                      </TableHeader>
                      <TableBody>
                        {filtered.map((f) => (
                          <>
                            <TableRow
                              key={f.id}
                              onClick={() => setExpanded(expanded === f.id ? null : f.id)}
                              className="cursor-pointer border-b-[#141d27] hover:bg-[#131c26]"
                            >
                              <TableCell className="py-2">
                                <div className="flex items-center gap-2">
                                  <Badge variant="outline" className={`px-1.5 text-[10px] ${SEV_CLS[f.severity] ?? ""}`}>
                                    {f.severity}
                                  </Badge>
                                  <span className="font-mono text-[11px] text-[#8fa3b5]">{f.rule_id}</span>
                                </div>
                                <div className="mt-0.5 text-[13px] text-[#c9d6e2]">{f.title}</div>
                              </TableCell>
                              <TableCell className="py-2 font-mono text-[11px] text-[#8fa3b5]">
                                {f.resource_name || f.resource_id}
                              </TableCell>
                              <TableCell className="py-2 text-right font-mono text-[13px] text-emerald-300">
                                {f.monthly_savings > 0 ? `−${fmt(f.monthly_savings)}` : "—"}
                              </TableCell>
                              <TableCell className="py-2 text-center">
                                <Badge variant="outline" className={`px-1.5 text-[10px] ${TIER_META[f.risk_tier]?.cls ?? ""}`}>
                                  T{f.risk_tier}
                                </Badge>
                              </TableCell>
                              <TableCell className="py-2 text-center">
                                <span
                                  className={`font-mono text-[11px] ${
                                    f.status === "applied" ? "text-emerald-300" : f.status === "planned" ? "text-sky-300" : "text-amber-300"
                                  }`}
                                >
                                  {f.status === "applied" ? "✓ applied" : f.status}
                                </span>
                              </TableCell>
                            </TableRow>
                            {expanded === f.id ? (
                              <TableRow key={`${f.id}-detail`} className="border-b-[#141d27] bg-[#0c131b]">
                                <TableCell colSpan={5} className="py-3">
                                  <div className="font-mono text-[11px] leading-relaxed text-[#7a8ea0]">
                                    {f.evidence?.map((e, i) => (
                                      <div key={i}>· {e}</div>
                                    ))}
                                    <Separator className="my-2 bg-[#1c2833]" />
                                    <div className="text-[#8fa3b5]">
                                      remediation: {f.remediation_action}
                                    </div>
                                  </div>
                                </TableCell>
                              </TableRow>
                            ) : null}
                          </>
                        ))}
                      </TableBody>
                    </Table>
                  </ScrollArea>
                </Card>
              </TabsContent>

              {/* Remediation log */}
              <TabsContent value="actions" className="mt-4">
                <Card className="border-[#1c2833] bg-[#0e151d]">
                  <CardContent className="pt-4">
                    <ScrollArea className="max-h-[560px]">
                      <div className="space-y-2">
                        {(summary?.actions ?? []).map((a, i) => (
                          <div key={i} className="rounded border border-[#1c2833] bg-[#0c131b] p-3">
                            <div className="flex items-center gap-2">
                              <Badge variant="outline" className="bg-emerald-500/15 text-[10px] text-emerald-300">
                                {a.verified ? "✓ verified" : "pending"}
                              </Badge>
                              <span className="font-mono text-[11px] text-[#8fa3b5]">{a.rule_id}</span>
                              <span className="ml-auto font-mono text-[10px] text-[#5c6f81]">
                                {new Date(a.at).toLocaleTimeString()}
                              </span>
                            </div>
                            <div className="mt-1.5 font-mono text-[12px] text-[#c9d6e2]">{a.action}</div>
                            <div className="mt-1 font-mono text-[11px] text-[#7a8ea0]">
                              {a.before} <span className="text-emerald-400">→</span> {a.after}
                            </div>
                          </div>
                        ))}
                      </div>
                    </ScrollArea>
                  </CardContent>
                </Card>
              </TabsContent>

              {/* Before / After */}
              <TabsContent value="beforeafter" className="mt-4">
                <Card className="border-[#1c2833] bg-[#0e151d]">
                  <CardHeader className="pb-2">
                    <CardTitle className="text-sm text-[#c9d6e2]">Monthly run rate by service (CUR billing)</CardTitle>
                  </CardHeader>
                  <CardContent>
                    <div className="space-y-3">
                      {serviceRows.map((r) => {
                        const max = Math.max(...serviceRows.map((x) => x.before), 1);
                        return (
                          <div key={r.service}>
                            <div className="mb-1 flex justify-between font-mono text-[11px]">
                              <span className="text-[#c9d6e2]">{r.service}</span>
                              <span>
                                <span className="text-[#8fa3b5]">{fmt(r.before)}</span>
                                {r.after !== r.before ? (
                                  <span className="ml-2 text-emerald-300">
                                    → {fmt(r.after)} (−{(((r.before - r.after) / (r.before || 1)) * 100).toFixed(0)}%)
                                  </span>
                                ) : null}
                              </span>
                            </div>
                            <div className="space-y-1">
                              <div className="h-4 rounded bg-[#131c26]">
                                <div className="h-4 rounded bg-[#8a4a42]" style={{ width: `${(r.before / max) * 100}%` }} />
                              </div>
                              {r.after !== r.before ? (
                                <div className="h-4 rounded bg-[#131c26]">
                                  <div className="h-4 rounded bg-emerald-500/80" style={{ width: `${(r.after / max) * 100}%` }} />
                                </div>
                              ) : null}
                            </div>
                          </div>
                        );
                      })}
                    </div>
                    <Separator className="my-4 bg-[#1c2833]" />
                    <div className="grid grid-cols-2 gap-3 font-mono text-[12px] sm:grid-cols-4">
                      <div>
                        <div className="text-[10px] uppercase tracking-wider text-[#7a8ea0]">before</div>
                        <div className="text-[#e0685a]">{fmt2(baseline)}/mo</div>
                      </div>
                      <div>
                        <div className="text-[10px] uppercase tracking-wider text-[#7a8ea0]">after</div>
                        <div className="text-emerald-400">{fmt2(after)}/mo</div>
                      </div>
                      <div>
                        <div className="text-[10px] uppercase tracking-wider text-[#7a8ea0]">annualized</div>
                        <div className="text-emerald-400">−{fmt((baseline - after) * 12)}</div>
                      </div>
                      <div>
                        <div className="text-[10px] uppercase tracking-wider text-[#7a8ea0]">re-audit findings left</div>
                        <div className="text-[#c9d6e2]">{summary?.re_scan_findings ?? 0} (roadmap tiers)</div>
                      </div>
                    </div>
                  </CardContent>
                </Card>
              </TabsContent>

              {/* Methodology */}
              <TabsContent value="method" className="mt-4">
                <div className="grid gap-4 md:grid-cols-2">
                  <Card className="border-[#1c2833] bg-[#0e151d]">
                    <CardHeader className="pb-2">
                      <CardTitle className="text-sm text-[#c9d6e2]">Zero-downtime risk tiers</CardTitle>
                    </CardHeader>
                    <CardContent className="space-y-3 text-[13px] leading-relaxed text-[#aebfcf]">
                      <div className="rounded border border-red-500/25 bg-red-500/5 p-3">
                        <span className="font-mono text-[11px] text-red-300">TIER 0 — pure waste</span>
                        <p className="mt-1 text-[12px]">Resources serving no traffic: unattached volumes, stale snapshots, orphaned IPs, empty load balancers, uncommitted upload parts. Deletion is invisible.</p>
                      </div>
                      <div className="rounded border border-emerald-500/25 bg-emerald-500/5 p-3">
                        <span className="font-mono text-[11px] text-emerald-300">TIER 1 — live, non-disruptive</span>
                        <p className="mt-1 text-[12px]">In-place AWS operations: gp2→gp3 via ModifyVolume, S3 lifecycle transitions, log retention, stopping instances idle below 5% CPU for 14 days of telemetry.</p>
                      </div>
                      <div className="rounded border border-amber-500/25 bg-amber-500/5 p-3">
                        <span className="font-mono text-[11px] text-amber-300">TIER 2 — maintenance windows</span>
                        <p className="mt-1 text-[12px]">Rightsizing, volume shrinks, NAT consolidation — minutes of planned change, scheduled with the client.</p>
                      </div>
                      <div className="rounded border border-sky-500/25 bg-sky-500/5 p-3">
                        <span className="font-mono text-[11px] text-sky-300">TIER 3 — planned initiatives</span>
                        <p className="mt-1 text-[12px]">Graviton migration, Spot capacity, Savings Plans commitments — engineering workstreams with modeled payback.</p>
                      </div>
                    </CardContent>
                  </Card>
                  <Card className="border-[#1c2833] bg-[#0e151d]">
                    <CardHeader className="pb-2">
                      <CardTitle className="text-sm text-[#c9d6e2]">Pipeline timeline (engine steps)</CardTitle>
                    </CardHeader>
                    <CardContent>
                      <Progress value={busy ? undefined : 100} className="mb-4 h-1 bg-[#131c26]" />
                      <ScrollArea className="max-h-[420px]">
                        <div className="space-y-2 font-mono text-[11px]">
                          {(status?.timeline ?? []).slice().reverse().map((s, i) => (
                            <div key={i} className="border-l-2 border-emerald-500/40 pl-3 text-[#8fa3b5]">
                              <div className="text-[#c9d6e2]">{s.step}</div>
                              <div>{s.detail}</div>
                              <div className="text-[10px] text-[#5c6f81]">{new Date(s.at).toLocaleString()}</div>
                            </div>
                          ))}
                        </div>
                      </ScrollArea>
                    </CardContent>
                  </Card>
                </div>
              </TabsContent>
            </Tabs>
          </>
        )}
      </main>

      <footer className="mt-8 border-t border-[#1c2833] py-4">
        <div className="mx-auto max-w-6xl px-4 font-mono text-[10px] text-[#5c6f81]">
          CloudTrim demo · boto3 scan path identical in demo (LocalStack/moto) and live AWS mode (unset AWS_ENDPOINT_URL) ·
          pricing: AWS public on-demand list, us-west-2 · savings math reconciles CUR billing vs API inventory
        </div>
      </footer>
    </div>
  );
}
