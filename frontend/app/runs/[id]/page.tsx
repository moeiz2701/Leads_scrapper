"use client";

/**
 * §13 Screen 2 — Run Progress.
 *
 * Per-stage counters, a log tail, per-source status pills and a Cancel button.
 *
 * The counters read `runs.stats`, which every stage already writes under its own
 * key — not a second counter path maintained alongside it. That matters because
 * the run summary printed by the scripts reads the same place, so the screen and
 * the CLI cannot report different numbers for the same run.
 *
 * Two honesty constraints the layout enforces:
 *
 *   - `partial` is rendered as its own status, never as a slightly worse `done`.
 *     §5.5's failure mode is harvesting nothing and not noticing.
 *   - The score bar stops at `unattributed_ceiling` (85 today) rather than 100,
 *     because §8's attribution engine is Phase 9 and the missing 15 points are a
 *     fact about the pipeline, not about the business.
 */

import { use, useCallback, useEffect, useState } from "react";
import Link from "next/link";
import { api, type RunDetail } from "@/lib/api";

const POLL_MS = 2000;

const STAGE_LABELS: Record<string, string> = {
  discovery: "1 · Discovery",
  contact_enrichment: "2 · Contact enrichment",
  social_enrichment: "3 · Social enrichment",
  person_attribution: "4 · Person attribution",
  normalise_score: "5 · Normalise & score",
  dedupe_export: "6 · Dedupe & export",
};

const ACTIVE = new Set(["queued", "running"]);

export default function RunProgressScreen({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  const { id } = use(params);
  const [run, setRun] = useState<RunDetail | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const refresh = useCallback(() => {
    api.getRun(id).then(setRun).catch((e) => setError(String(e)));
  }, [id]);

  useEffect(() => {
    refresh();
  }, [refresh]);

  // Polling stops the moment the run reaches a terminal status — §13 asks for
  // live counters, not for a page that hammers the API forever on finished runs.
  useEffect(() => {
    if (!run || !ACTIVE.has(run.status)) return;
    const timer = setInterval(refresh, POLL_MS);
    return () => clearInterval(timer);
  }, [run, refresh]);

  async function cancel() {
    setBusy(true);
    try {
      setRun(await api.cancelRun(id));
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  }

  async function rerun(stage: string) {
    setBusy(true);
    try {
      setRun(await api.rerunStage(id, stage));
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  }

  if (!run) {
    return <p className="muted">{error ?? "Loading…"}</p>;
  }

  const scoring = run.stats?.normalise_score ?? {};
  const ceiling = run.unattributed_ceiling ?? 100;

  return (
    <>
      <h1>
        {run.city} × {run.category}{" "}
        <span className={`badge ${run.status}`}>{run.status}</span>
      </h1>
      <p className="subtitle mono small">{run.id}</p>

      {error && <div className="notice bad">{error}</div>}
      {run.error && <div className="notice bad">{run.error}</div>}

      {run.status === "partial" && (
        <div className="notice warn">
          <b>Partial, not done.</b> This run could not complete every stage it was
          asked for. See the stage list below for which, and which §16 phase builds
          it. A run that skipped work must not report success.
        </div>
      )}

      <div className="toolbar">
        <Link href={`/results?run=${run.id}`}>
          <button className="primary">Open results table</button>
        </Link>
        {/* This run's businesses, pre-filtered to the two halves worth working
            separately. The Extract button and the website filter live on that
            screen because that is where the filter bar is, and a pull is
            defined as "the top N of what is filtered". */}
        <Link href={`/results?run=${run.id}&website=yes`}>
          <button>With a website</button>
        </Link>
        <Link href={`/results?run=${run.id}&website=no`}>
          <button>No website</button>
        </Link>
        <Link href="/extracted">
          <button>Extracted list</button>
        </Link>
        <button onClick={refresh} disabled={busy}>
          Refresh
        </button>
        <span className="spacer" />
        {ACTIVE.has(run.status) && (
          <button className="danger" onClick={cancel} disabled={busy}>
            Cancel run
          </button>
        )}
      </div>

      {ACTIVE.has(run.status) && (
        <div className="notice small">
          Cancel drops every <i>queued</i> stage immediately and stops the chain at
          the next stage boundary. A stage already executing runs to completion —
          killing a worker mid-crawl would leave the §7 cache and breaker state
          inconsistent.
        </div>
      )}

      <div className="grid-2">
        <div className="panel">
          <h2 style={{ marginTop: 0 }}>Stages</h2>
          {run.stages.map((s) => (
            <div key={s.stage} className="stage-row">
              <div>{STAGE_LABELS[s.stage] ?? s.stage}</div>
              <div>
                <span className={`badge ${s.state}`}>{s.state}</span>
              </div>
              <div className="small muted">
                {s.note ? (
                  s.note
                ) : (
                  <>
                    {s.processed !== null && <>processed {s.processed} </>}
                    {s.produced !== null && <>· produced {s.produced}</>}
                  </>
                )}
              </div>
              <div style={{ textAlign: "right" }}>
                {s.elapsed_seconds !== null && (
                  <span className="small mono muted">{s.elapsed_seconds}s </span>
                )}
                {s.state !== "unimplemented" && s.state !== "skipped" && (
                  <button
                    className="small"
                    onClick={() => rerun(s.stage)}
                    disabled={busy}
                    title="§2: re-run this stage without re-running the ones before it"
                  >
                    ↻
                  </button>
                )}
              </div>
            </div>
          ))}
        </div>

        <div>
          <div className="panel">
            <h2 style={{ marginTop: 0 }}>Sources</h2>
            {run.sources.length === 0 && (
              <p className="muted small">
                No source health recorded yet — written when a stage completes.
              </p>
            )}
            {run.sources.map((p) => (
              <div key={p.source} style={{ marginBottom: 8 }}>
                <span className={`badge ${p.status}`}>{p.status}</span>{" "}
                <span className="mono small">{p.source}</span>
                {p.detail && <div className="small muted">{p.detail}</div>}
              </div>
            ))}
            <div className="small muted" style={{ marginTop: 10 }}>
              §5.2: one host refusing is not the source refusing. A few refusals
              show as <b>throttled</b>; only our egress being blocked is{" "}
              <b>blocked</b>.
            </div>
          </div>

          <div className="panel">
            <h2 style={{ marginTop: 0 }}>Yield</h2>
            <Metric label="Businesses" value={run.businesses} />
            <Metric label="With a phone" value={scoring.with_phone} />
            <Metric label="With a mobile" value={scoring.with_mobile} />
            <Metric label="Qualified (≥ 60 + a mobile)" value={scoring.qualified} />
            <Metric label="Mean score" value={scoring.score_mean} />
            <Metric label="Max score" value={scoring.score_max} />

            {ceiling < 100 && (
              <div className="notice small warn">
                Scores top out at <b>{ceiling}</b>, not 100. §10.2 gives 15 points to
                person attribution and §8&apos;s engine is Phase 9, so that term is 0
                on almost every row. The missing points are a gap in the pipeline,
                not a fact about these businesses.
              </div>
            )}
          </div>

          <div className="panel">
            <h2 style={{ marginTop: 0 }}>Queue</h2>
            <div className="log">
              {Object.entries(run.queue_depths).length === 0
                ? "Queue depth unavailable — Redis unreachable."
                : Object.entries(run.queue_depths)
                    .map(([stage, depth]) => `${stage.padEnd(20)} ${depth}`)
                    .join("\n")}
            </div>
          </div>
        </div>
      </div>

      <div className="panel">
        <h2 style={{ marginTop: 0 }}>Stage reports (runs.stats)</h2>
        {/* The log tail §13 asks for. Structured rather than a text stream: every
            stage writes a report here, so this is the run's actual record — a
            separate log file would be a second version of the same events. */}
        <div className="log">{JSON.stringify(run.stats, null, 2)}</div>
      </div>
    </>
  );
}

function Metric({ label, value }: { label: string; value: unknown }) {
  return (
    <div style={{ display: "flex", justifyContent: "space-between", padding: "3px 0" }}>
      <span className="muted small">{label}</span>
      {/* Missing stays missing (§10.2): a stage that has not run yet shows a dash,
          never a 0 that reads as a measured result. */}
      <span className="mono">
        {value === undefined || value === null ? (
          <span className="muted">—</span>
        ) : (
          String(value)
        )}
      </span>
    </div>
  );
}
