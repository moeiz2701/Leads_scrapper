"use client";

/** Run history. Not one of §13's three screens, but §16 asks you to validate by
 *  re-running, and comparing a re-run to its predecessor needs them listed. */

import { useEffect, useState } from "react";
import Link from "next/link";
import { api, type RunSummary } from "@/lib/api";

export default function RunsScreen() {
  const [runs, setRuns] = useState<RunSummary[]>([]);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api.listRuns().then(setRuns).catch((e) => setError(String(e)));
  }, []);

  return (
    <>
      <h1>Runs</h1>
      <p className="subtitle">Every run in the database, newest first.</p>
      {error && <div className="notice bad">{error}</div>}

      <div className="table-wrap">
        <table>
          <thead>
            <tr>
              <th>Status</th>
              <th>City</th>
              <th>Category</th>
              <th>Preference</th>
              <th className="num">Businesses</th>
              <th className="num">Qualified</th>
              <th>Started</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {runs.map((run) => (
              <tr key={run.id}>
                <td>
                  <span className={`badge ${run.status}`}>{run.status}</span>
                </td>
                <td>{run.city ?? "—"}</td>
                <td>{run.category ?? "—"}</td>
                <td className="small muted">{run.number_preference}</td>
                <td className="num mono">{run.businesses}</td>
                <td className="num mono">
                  {/* A discovery-only run has 0 qualified by construction
                      (§10.2), so a bare 0 here reads as a failure when it is a
                      missing stage. */}
                  {run.qualified === 0 ? <span className="muted">0</span> : run.qualified}
                </td>
                <td className="small muted">
                  {run.started_at ? new Date(run.started_at).toLocaleString() : "—"}
                </td>
                <td>
                  <Link href={`/runs/${run.id}`}>progress</Link>
                  {" · "}
                  <Link href={`/results?run=${run.id}`}>results</Link>
                </td>
              </tr>
            ))}
            {runs.length === 0 && (
              <tr>
                <td colSpan={8} className="muted">
                  No runs yet. <Link href="/">Start one.</Link>
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
    </>
  );
}
