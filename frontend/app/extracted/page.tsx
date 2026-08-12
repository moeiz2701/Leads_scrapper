"use client";

/**
 * The extraction ledger — what has already been handed out.
 *
 * This screen is the other half of the Extract button. The mark is only useful
 * if it can be inspected and undone: an operator who pulled 30 rows and never
 * sent them needs those businesses back in the queue, and one who is about to
 * send needs to see what went out last time.
 *
 * Two honesty constraints:
 *
 *   - The numbers shown are the ones **stored at pull time**, not a live read of
 *     the business's contacts. A later run raising a number's §9.3 evidence must
 *     not rewrite the record of a message that has already been sent.
 *   - Clearing is not §15. It retracts "already sent" and nothing else — no
 *     business row, no contact, no `do_not_contact` entry is touched. That is
 *     said on screen, because "clear" next to a delete button that *does* write
 *     a permanent suppression is exactly the confusion worth pre-empting.
 */

import { useCallback, useEffect, useMemo, useState } from "react";
import Link from "next/link";
import {
  api,
  copyToClipboard,
  type ExtractionEntry,
  type RunSummary,
} from "@/lib/api";

export default function ExtractedScreen() {
  const [entries, setEntries] = useState<ExtractionEntry[]>([]);
  const [runs, setRuns] = useState<RunSummary[]>([]);
  const [runFilter, setRunFilter] = useState<string>("");
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  const load = useCallback(() => {
    setLoading(true);
    api
      .extractions(runFilter ? [runFilter] : [])
      .then((rows) => {
        setEntries(rows);
        setError(null);
      })
      .catch((e) => setError(String(e)))
      .finally(() => setLoading(false));
  }, [runFilter]);

  useEffect(() => {
    load();
  }, [load]);

  useEffect(() => {
    api.listRuns().then(setRuns).catch((e) => setError(String(e)));
  }, []);

  const numbers = useMemo(
    () => [...new Set(entries.flatMap((e) => e.numbers))],
    [entries],
  );

  async function clearOne(entry: ExtractionEntry) {
    try {
      await api.clearExtraction(entry.id);
      setNotice(
        `${entry.business_name ?? "That business"} is back in the queue — the ` +
          "next Extract can offer it again. Nothing else changed.",
      );
      load();
    } catch (e) {
      setError(String(e));
    }
  }

  async function clearAll() {
    const scope = runFilter
      ? "this run's extracted list"
      : "the whole extracted list";
    if (
      !window.confirm(
        `Clear ${scope} (${entries.length} entr${entries.length === 1 ? "y" : "ies"})?\n\n` +
          "Every one of these businesses becomes extractable again. No business, " +
          "contact or §15 suppression is deleted — this only retracts the " +
          '"already sent" mark.',
      )
    ) {
      return;
    }
    try {
      const result = await api.clearExtractions(runFilter ? [runFilter] : []);
      setNotice(`Cleared ${result.cleared} entr${result.cleared === 1 ? "y" : "ies"}.`);
      load();
    } catch (e) {
      setError(String(e));
    }
  }

  return (
    <>
      <h1>Extracted</h1>
      <p className="subtitle">
        Businesses already pulled for outreach. Clearing one puts it back in the
        queue — it is not a §15 suppression.
      </p>

      {error && <div className="notice bad">{error}</div>}
      {notice && <div className="notice info">{notice}</div>}

      <div className="panel">
        <div className="row">
          <div className="field" style={{ flex: "2 1 320px" }}>
            <label>Run</label>
            <select value={runFilter} onChange={(e) => setRunFilter(e.target.value)}>
              <option value="">every run</option>
              {runs.map((r) => (
                <option key={r.id} value={r.id}>
                  {r.city} × {r.category} · {r.businesses} businesses
                </option>
              ))}
            </select>
          </div>
        </div>
      </div>

      <div className="toolbar">
        <b>{entries.length}</b>
        <span className="muted">
          extracted{loading && " · loading…"} · {numbers.length} distinct number(s)
        </span>
        <span className="spacer" />
        <Link href="/results">
          <button>Back to results</button>
        </Link>
        <button
          onClick={async () =>
            setNotice(
              (await copyToClipboard(numbers.join("\n")))
                ? `Copied ${numbers.length} number(s).`
                : "The browser refused the clipboard.",
            )
          }
          disabled={numbers.length === 0}
          title="Re-copy every number in this list, as it was sent"
        >
          Copy all numbers
        </button>
        <button className="danger" onClick={clearAll} disabled={entries.length === 0}>
          Clear {runFilter ? "this run" : "all"}
        </button>
      </div>

      <div className="table-wrap">
        <table>
          <thead>
            <tr>
              <th>business</th>
              <th>city</th>
              <th className="num">score</th>
              <th>website</th>
              <th>numbers sent</th>
              <th>extracted_at</th>
              <th>batch</th>
              <th style={{ width: 70 }} />
            </tr>
          </thead>
          <tbody>
            {entries.map((entry) => (
              <tr key={entry.id}>
                <td>{entry.business_name ?? <span className="muted">—</span>}</td>
                <td>{entry.city ?? <span className="muted">—</span>}</td>
                <td className="num mono">
                  {entry.lead_score ?? <span className="muted">—</span>}
                </td>
                <td>
                  {entry.website ? (
                    <a href={entry.website} target="_blank" rel="noreferrer">
                      {entry.website.replace(/^https?:\/\//, "").slice(0, 30)}
                    </a>
                  ) : (
                    <span className="muted">—</span>
                  )}
                </td>
                <td className="mono small">
                  {/* Blank, not 0 — a business can be extracted having had no
                      confirmed or likely number, and the batch count says so. */}
                  {entry.numbers.length ? (
                    entry.numbers.join(" · ")
                  ) : (
                    <span className="muted">no qualifying number</span>
                  )}
                </td>
                <td className="small mono muted">
                  {entry.extracted_at?.slice(0, 19).replace("T", " ") ?? "—"}
                </td>
                <td className="small muted">top {entry.batch_size ?? "—"}</td>
                <td style={{ textAlign: "right" }}>
                  <button
                    className="small"
                    onClick={() => clearOne(entry)}
                    title="Put this business back in the queue. Deletes nothing else."
                  >
                    Clear
                  </button>
                </td>
              </tr>
            ))}
            {entries.length === 0 && !loading && (
              <tr>
                <td colSpan={8}>
                  <span className="muted">
                    Nothing extracted yet. Use <b>Extract</b> on the results table
                    to pull the top 30, 50 or 100 of whatever is filtered there.
                  </span>
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
    </>
  );
}
