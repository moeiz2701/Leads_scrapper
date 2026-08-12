"use client";

/**
 * §13 Screen 3 — Results Table.
 *
 * Sticky header, virtualised rows, sort any column (default `lead_score DESC`),
 * the filter bar, a column-visibility toggle persisted to localStorage, green
 * badges on `confirmed` WhatsApp cells, row-expand to an evidence panel, Export
 * CSV of the current filtered view, and bulk select → delete.
 *
 * **The major filter is the outreach batch** (_BATCH_SPEC.md), not the
 * has-a-website split it replaced. The two are not alternatives at the same
 * level: a website is one property, while a batch is the answer to "which
 * message does this business get", and it already contains the website
 * distinction (`delivery-site` vs `delivery-nosite`). Picking a batch is
 * therefore picking a day's work, which is why this screen shows the batch's
 * definition and its send note next to the picker — the operator is about to
 * copy 51 numbers and write one message for all of them.
 *
 * `hasWebsite` survives as a chip rather than a control. Screen 2 deep-links
 * into one half of a run with `?website=yes|no`, and a filter that is applied
 * but not visible is the one thing a filter bar must never do.
 *
 * **Sorting and filtering are server-side, not TanStack's client-side models.**
 * §12.2 requires the CSV to be the filtered, sorted set the operator is looking
 * at, and it is generated server-side because the column set is dynamic. If the
 * table sorted in the browser, the Export button would produce a *differently
 * ordered* file from the screen it was clicked on. So the filter state is a
 * query string, both the table and the export read it, and TanStack is used for
 * what it is uniquely good at here — column definitions, visibility state and
 * virtualisation.
 */

import { Suspense, useCallback, useEffect, useMemo, useRef, useState } from "react";
import Link from "next/link";
import { useSearchParams } from "next/navigation";
import {
  flexRender,
  getCoreRowModel,
  useReactTable,
  type ColumnDef,
  type VisibilityState,
} from "@tanstack/react-table";
import { useVirtualizer } from "@tanstack/react-virtual";
import {
  api,
  BATCH_SIZES,
  copyToClipboard,
  downloadCsv,
  emptyFilters,
  EXTRACT_ALL,
  toQueryString,
  type BatchCatalogue,
  type BatchInfo,
  type ExtractionResult,
  type ExtractLimit,
  type LeadRow,
  type ResultsResponse,
  type RunSummary,
  type TableFilters,
} from "@/lib/api";

const VISIBILITY_KEY = "leadscraper.columnVisibility";
const ROW_HEIGHT = 33;

export default function ResultsPage() {
  return (
    <Suspense fallback={<p className="muted">Loading…</p>}>
      <ResultsScreen />
    </Suspense>
  );
}

function ResultsScreen() {
  const searchParams = useSearchParams();
  const [runs, setRuns] = useState<RunSummary[]>([]);
  const [catalogue, setCatalogue] = useState<BatchCatalogue | null>(null);
  const [data, setData] = useState<ResultsResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [filters, setFilters] = useState<TableFilters>({
    ...emptyFilters,
    runIds: searchParams.getAll("run"),
    // `?website=yes|no` — how Screen 2 deep-links into one half of a run.
    // Anything else, including its absence, is "any".
    hasWebsite:
      searchParams.get("website") === "yes"
        ? true
        : searchParams.get("website") === "no"
          ? false
          : null,
    // `?batch=delivery-nosite` — a link straight into one batch's work queue.
    batches: searchParams.get("batch")?.split(",").filter(Boolean) ?? [],
  });
  const [selected, setSelected] = useState<Set<string>>(new Set());
  // A drawer, not an inline expanded row: an extra row of variable height inside
  // a virtualised body throws off the spacer arithmetic that keeps the scrollbar
  // honest. §13 asks for an evidence panel, not specifically for an inline one.
  const [expanded, setExpanded] = useState<LeadRow | null>(null);
  const [columnVisibility, setColumnVisibility] = useState<VisibilityState>({});
  const [menuOpen, setMenuOpen] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const [extractOpen, setExtractOpen] = useState(false);
  const [extracting, setExtracting] = useState(false);
  // Kept on screen after the copy. A clipboard the browser refused is
  // indistinguishable from one it accepted until you paste, and the businesses
  // are already marked by then — so the numbers stay visible and re-copyable.
  const [pull, setPull] = useState<ExtractionResult | null>(null);
  const [copied, setCopied] = useState(false);

  useEffect(() => {
    api.listRuns().then(setRuns).catch((e) => setError(String(e)));
    // Fetched, never hard-coded: a batch list typed into TypeScript would be a
    // second definition of a controlled vocabulary the cascade owns.
    api.batches().then(setCatalogue).catch((e) => setError(String(e)));
  }, []);

  const query = useMemo(() => toQueryString(filters), [filters]);

  const load = useCallback(() => {
    setLoading(true);
    api
      .results(query)
      .then((response) => {
        setData(response);
        setError(null);
      })
      .catch((e) => setError(String(e)))
      .finally(() => setLoading(false));
  }, [query]);

  useEffect(() => {
    load();
  }, [load]);

  // §13: "column visibility toggle, persisted to localStorage". Defaults to the
  // §12.3 compact view — nobody wants 41 columns on screen — and only on first
  // visit, so a deliberate choice is never overwritten.
  useEffect(() => {
    if (!data || Object.keys(columnVisibility).length) return;
    const stored = localStorage.getItem(VISIBILITY_KEY);
    if (stored) {
      setColumnVisibility(JSON.parse(stored));
      return;
    }
    const compact = new Set(data.compact_columns);
    setColumnVisibility(
      Object.fromEntries(data.columns.map((c) => [c, compact.has(c)])),
    );
  }, [data, columnVisibility]);

  useEffect(() => {
    if (Object.keys(columnVisibility).length) {
      localStorage.setItem(VISIBILITY_KEY, JSON.stringify(columnVisibility));
    }
  }, [columnVisibility]);

  const columns = useMemo<ColumnDef<LeadRow>[]>(() => {
    if (!data) return [];
    const numeric = new Set(data.numeric_columns);
    return data.columns.map((name) => ({
      id: name,
      accessorFn: (row: LeadRow) => row[name],
      header: name,
      meta: { numeric: numeric.has(name) },
      cell: (info) => renderCell(name, info.getValue()),
    }));
  }, [data]);

  const table = useReactTable({
    data: data?.rows ?? [],
    columns,
    state: { columnVisibility },
    onColumnVisibilityChange: setColumnVisibility,
    getCoreRowModel: getCoreRowModel(),
  });

  const scrollRef = useRef<HTMLDivElement>(null);
  const rows = table.getRowModel().rows;
  const virtualizer = useVirtualizer({
    count: rows.length,
    getScrollElement: () => scrollRef.current,
    estimateSize: () => ROW_HEIGHT,
    overscan: 12,
  });

  const virtualRows = virtualizer.getVirtualItems();
  const paddingTop = virtualRows.length ? virtualRows[0].start : 0;
  const paddingBottom = virtualRows.length
    ? virtualizer.getTotalSize() - virtualRows[virtualRows.length - 1].end
    : 0;

  function patch(update: Partial<TableFilters>) {
    setFilters((prev) => ({ ...prev, ...update }));
    setSelected(new Set());
  }

  function toggleSort(column: string) {
    setFilters((prev) =>
      prev.sort === column
        ? { ...prev, descending: !prev.descending }
        : { ...prev, sort: column, descending: true },
    );
  }

  async function exportCsv() {
    try {
      // The same query string the table is showing — §12.2's "respect the active
      // filters and sort order", guaranteed by construction rather than by
      // remembering to keep two call sites in step.
      const single = filters.runIds.length === 1;
      const path = single
        ? `/api/runs/${filters.runIds[0]}/export.csv`
        : "/api/results/export.csv";
      await downloadCsv(path, query);
    } catch (e) {
      setError(String(e));
    }
  }

  /**
   * Extract → top N.
   *
   * `query` is the same string the table and the Export button send, so the
   * pull is whatever is filtered on screen — the backend reads it through the
   * one filter dependency and there is no second filter set here to drift.
   *
   * The order matters: the server marks and returns, then we copy. A failed
   * copy therefore leaves rows marked, which is why the numbers are rendered
   * below rather than only pushed at the clipboard.
   */
  async function extract(limit: ExtractLimit) {
    if (limit === EXTRACT_ALL) {
      // The one irreversible-feeling pull. It is undoable — clearing the ledger
      // puts every row back — but it marks the whole view in one click, so the
      // count and the scope get said out loud first.
      const scope = selectedBatch
        ? `${selectedBatch.id} · ${selectedBatch.name}`
        : "the current filtered view";
      if (
        !window.confirm(
          `Extract all ${remainingInView} un-extracted row(s) in ${scope}?\n\n` +
            "Every one is marked extracted and skipped by later pulls. Nothing " +
            "is deleted and nothing is suppressed — clearing them from the " +
            "Extracted screen puts them back in the queue.",
        )
      ) {
        return;
      }
    }
    setExtractOpen(false);
    setExtracting(true);
    setCopied(false);
    try {
      const result = await api.extract(query, limit);
      setPull(result);
      setCopied(await copyToClipboard(result.clipboard));
      setNotice(null);
      load();
    } catch (e) {
      setError(String(e));
    } finally {
      setExtracting(false);
    }
  }

  async function bulkDelete() {
    const ids = [...selected];
    const confirmed = window.confirm(
      `Delete ${ids.length} business(es)?\n\n` +
        "Every number on them goes onto the §15 suppression list first, so the " +
        "removal survives the next run. Deleting the rows alone would not — the " +
        "next scrape of this city and category would put them straight back.",
    );
    if (!confirmed) return;

    try {
      const result = await api.bulkDelete({
        business_ids: ids,
        reason: "removal request (§13 Screen 3)",
      });
      setNotice(
        `Deleted ${result.businesses_deleted} business(es) and ${result.contacts_deleted} ` +
          `contact(s). Suppressed ${result.numbers_suppressed.length} number(s) and ` +
          `${result.domains_suppressed.length} domain(s) permanently.`,
      );
      setSelected(new Set());
      load();
    } catch (e) {
      setError(String(e));
    }
  }

  const visibleColumns = table.getVisibleLeafColumns();
  const extractedInView = useMemo(
    () => (data?.rows ?? []).filter((r) => r._extracted).length,
    [data],
  );
  // What "Extract all" would take. The rows on screen minus the ones already on
  // the ledger — the same arithmetic the server does, shown before the click
  // rather than reported after it.
  const remainingInView = (data?.total ?? 0) - extractedInView;
  const selectedBatch = useMemo(
    () =>
      catalogue?.batches.find((b) => b.slug === filters.batches[0]) ?? null,
    [catalogue, filters.batches],
  );
  const unbatchedInView = catalogue
    ? (data?.batch_counts?.[catalogue.unbatched_slug] ?? 0)
    : 0;

  return (
    <>
      <h1>Results</h1>
      <p className="subtitle">§13 Screen 3 · default sort lead_score DESC</p>

      {error && <div className="notice bad">{error}</div>}
      {notice && <div className="notice info">{notice}</div>}

      <div className="panel">
        <div className="row">
          <div className="field" style={{ flex: "2 1 320px" }}>
            <label>Runs</label>
            <select
              multiple
              size={4}
              value={filters.runIds}
              onChange={(e) =>
                patch({
                  runIds: [...e.target.selectedOptions].map((o) => o.value),
                })
              }
            >
              {runs.map((r) => (
                <option key={r.id} value={r.id}>
                  {r.city} × {r.category} · {r.businesses} businesses · {r.status}
                </option>
              ))}
            </select>
          </div>
          <div className="field">
            <label>Free text · matches phone digits too</label>
            <input
              type="search"
              value={filters.search}
              placeholder="name, address, or 0300 1234567"
              onChange={(e) => patch({ search: e.target.value })}
            />
          </div>
          <div className="field" style={{ flex: "0 0 130px" }}>
            <label>Min score</label>
            <input
              type="number"
              min={0}
              max={100}
              value={filters.minScore ?? ""}
              onChange={(e) =>
                patch({ minScore: e.target.value === "" ? null : Number(e.target.value) })
              }
            />
          </div>
          <div className="field" style={{ flex: "0 0 150px" }}>
            <label>WhatsApp</label>
            <select
              value={filters.whatsapp[0] ?? ""}
              onChange={(e) => patch({ whatsapp: e.target.value ? [e.target.value] : [] })}
            >
              <option value="">any</option>
              <option value="confirmed">confirmed</option>
              <option value="likely">likely</option>
              <option value="no">no</option>
            </select>
          </div>
          <div className="field" style={{ flex: "0 0 150px" }}>
            <label>Phone type</label>
            <select
              value={filters.lineTypes[0] ?? ""}
              onChange={(e) =>
                patch({ lineTypes: e.target.value ? [e.target.value] : [] })
              }
            >
              <option value="">any</option>
              <option value="mobile">mobile</option>
              <option value="landline">landline</option>
              <option value="uan">uan</option>
            </select>
          </div>
          <div
            className="field"
            style={{ flex: "1 1 320px" }}
            title="_BATCH_SPEC.md — the outreach cascade. Mutually exclusive: every business is in exactly one batch, so nobody is messaged twice."
          >
            <label>Outreach batch</label>
            <select
              value={filters.batches[0] ?? ""}
              onChange={(e) => patch({ batches: e.target.value ? [e.target.value] : [] })}
            >
              <option value="">every batch — {data?.total ?? 0} rows</option>
              {(catalogue?.batches ?? []).map((b) => (
                <option key={b.slug} value={b.slug}>
                  {b.id} · {b.name} — {data?.batch_counts?.[b.slug] ?? 0}
                  {b.sendable ? "" : " (do not send)"}
                </option>
              ))}
              {catalogue && (
                /* Not a batch, and listed apart from them: the cascade has no
                   definitions for this business's category. Offered as a filter
                   because on a non-food run it is every row, and "why is every
                   batch empty?" needs an answer that is clickable. */
                <option value={catalogue.unbatched_slug}>
                  no batch defined —{" "}
                  {data?.batch_counts?.[catalogue.unbatched_slug] ?? 0}
                </option>
              )}
            </select>
          </div>
          <div className="field" style={{ flex: "0 0 150px" }}>
            <label>Source</label>
            <select
              value={filters.sources[0] ?? ""}
              onChange={(e) => patch({ sources: e.target.value ? [e.target.value] : [] })}
            >
              <option value="">any</option>
              <option value="google_maps">google_maps</option>
              <option value="business_website">business_website</option>
            </select>
          </div>
        </div>

        <div className="row" style={{ marginTop: 12 }}>
          <label className="check">
            <input
              type="checkbox"
              checked={filters.hasOwnerName === true}
              onChange={(e) => patch({ hasOwnerName: e.target.checked ? true : null })}
            />
            Has owner name
          </label>
          <label
            className="check"
            title="§10.1: collapse the same place_id across runs at query time. Non-destructive — no run is modified."
          >
            <input
              type="checkbox"
              checked={filters.collapse}
              onChange={(e) => patch({ collapse: e.target.checked })}
            />
            One table across runs (collapse on place_id)
          </label>

          {/* The website split, demoted from a control to a chip. It is still a
              real filter — Screen 2 links into it — so it has to be visible and
              clearable wherever it came from. */}
          {filters.hasWebsite !== null && (
            <span className="badge likely" style={{ alignSelf: "center" }}>
              {filters.hasWebsite ? "has a website" : "no website on record"}
              <button
                className="small"
                style={{ marginLeft: 6, padding: "0 5px" }}
                onClick={() => patch({ hasWebsite: null })}
                title="Clear the website filter. The batch cascade already splits on this — delivery-site vs delivery-nosite."
              >
                ×
              </button>
            </span>
          )}
        </div>

        {selectedBatch && <BatchNote batch={selectedBatch} />}

        {catalogue && unbatchedInView > 0 && !filters.batches.length && (
          /* Never silently: on a salon run every batch reads 0 and the reason is
             a fact about the spec, not about the data. */
          <div className="notice small" style={{ marginTop: 10 }}>
            <b>{unbatchedInView}</b> row(s) here are in a category the cascade has
            no definitions for, so they are in no batch. Batches are defined for{" "}
            <b>{catalogue.categories.join(", ")}</b> only — {catalogue.note}
          </div>
        )}
      </div>

      <div className="toolbar">
        <b>{data?.total ?? 0}</b>
        <span className="muted">rows{loading && " · loading…"}</span>

        {data && data.collapsed > 0 && (
          <span className="badge likely">{data.collapsed} folded by place_id</span>
        )}
        {data && (data.suppressed_contacts > 0 || data.suppressed_businesses > 0) && (
          /* §15 applied silently is §15 nobody trusts. */
          <span className="badge throttled">
            §15 hidden: {data.suppressed_businesses} business(es),{" "}
            {data.suppressed_contacts} contact(s)
          </span>
        )}
        {extractedInView > 0 && (
          /* Shown here as well as in the Extract menu: these rows are still in
             the table and still in the CSV — they are marked, not hidden. */
          <span className="badge done">{extractedInView} already extracted</span>
        )}

        <span className="spacer" />

        {selected.size > 0 && (
          <button className="danger" onClick={bulkDelete}>
            Delete {selected.size} selected
          </button>
        )}

        <div className="column-toggle">
          <button onClick={() => setMenuOpen((v) => !v)}>
            Columns ({visibleColumns.length}/{data?.columns.length ?? 0})
          </button>
          {menuOpen && data && (
            <div className="column-menu">
              <div className="row" style={{ marginBottom: 8 }}>
                <button
                  onClick={() =>
                    setColumnVisibility(
                      Object.fromEntries(data.columns.map((c) => [c, true])),
                    )
                  }
                >
                  All 41
                </button>
                <button
                  onClick={() => {
                    const compact = new Set(data.compact_columns);
                    setColumnVisibility(
                      Object.fromEntries(data.columns.map((c) => [c, compact.has(c)])),
                    );
                  }}
                >
                  Compact (§12.3)
                </button>
              </div>
              {data.columns.map((name) => (
                <label key={name} className="check small">
                  <input
                    type="checkbox"
                    checked={columnVisibility[name] ?? true}
                    onChange={(e) =>
                      setColumnVisibility((prev) => ({ ...prev, [name]: e.target.checked }))
                    }
                  />
                  {name}
                </label>
              ))}
            </div>
          )}
        </div>

        <div className="column-toggle">
          <button
            className="primary"
            onClick={() => setExtractOpen((v) => !v)}
            disabled={!data?.total || extracting}
            title="Copy the WhatsApp-worthy numbers off the top of this filtered view, and mark those businesses extracted"
          >
            {extracting ? "Extracting…" : "Extract ▾"}
          </button>
          {extractOpen && (
            <div className="column-menu" style={{ minWidth: 300 }}>
              <div className="small muted" style={{ marginBottom: 8 }}>
                Copies every <b>confirmed</b> and <b>likely</b> number from the top
                of <i>this filtered view</i>
                {selectedBatch && (
                  <>
                    {" "}
                    — <b>{selectedBatch.id} · {selectedBatch.name}</b>
                  </>
                )}
                , one per line, and marks those businesses extracted so the next
                pull moves past them.
              </div>
              <div className="row">
                {BATCH_SIZES.map((size) => (
                  <button key={size} onClick={() => extract(size)}>
                    Top {size}
                  </button>
                ))}
                {/* Working a batch means draining it. Two rounds of top-30 and a
                    third that returns 11 is the same pull said three times. */}
                <button
                  className="primary"
                  onClick={() => extract(EXTRACT_ALL)}
                  disabled={remainingInView <= 0}
                  title="Every un-extracted row in this filtered view, however many"
                >
                  All {remainingInView} remaining
                </button>
              </div>
              <div className="small muted" style={{ marginTop: 8 }}>
                {extractedInView} of {data?.total ?? 0} rows here are already
                extracted and will be skipped.
              </div>
              {selectedBatch && !selectedBatch.sendable && (
                <div className="notice small" style={{ marginTop: 8 }}>
                  <b>{selectedBatch.name}</b> is not a send batch — these
                  businesses have no WhatsApp-capable number, so a pull marks them
                  worked and copies nothing. Route them to email or a visit.
                </div>
              )}
            </div>
          )}
        </div>

        <button className="primary" onClick={exportCsv} disabled={!data?.total}>
          Export CSV
        </button>
      </div>

      {/* Keyed on the batch so a second pull remounts the panel — otherwise its
          "Copied ✓" state would carry over from the previous one and claim a
          copy that has not happened yet. */}
      {pull && (
        <PullPanel
          key={pull.batch_id ?? "pull"}
          pull={pull}
          copied={copied}
          onClose={() => setPull(null)}
        />
      )}

      <div className="table-wrap" ref={scrollRef}>
        <table>
          <thead>
            <tr>
              <th style={{ width: 30 }} />
              <th style={{ width: 26 }} />
              {/* The extraction mark decorates the row; it never filters it.
                  Hiding extracted rows would shrink the CSV too — §12.2 makes
                  them the same query — so a business would vanish from an
                  export because somebody once copied its number. */}
              <th style={{ width: 30 }} title="On the extraction ledger" />
              {/* Not one of §12.1's 41 columns and deliberately outside the
                  column-visibility toggle: the batch is how this screen is now
                  organised, and it is derived rather than exported. */}
              <th style={{ width: 74 }} title="_BATCH_SPEC batch · send rank within it">
                batch
              </th>
              {visibleColumns.map((column) => (
                <th
                  key={column.id}
                  onClick={() => toggleSort(column.id)}
                  className={(column.columnDef.meta as any)?.numeric ? "num" : ""}
                >
                  {column.id}
                  {filters.sort === column.id && (filters.descending ? " ↓" : " ↑")}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {/* Virtualised with spacer rows rather than absolutely-positioned
                ones. The transform approach needs flex rows with fixed widths,
                which loses the column alignment between <thead> and <tbody> that
                a real table gives for free — and §13 asks for a sticky header
                over sortable columns, so that alignment is the feature. */}
            {paddingTop > 0 && <tr style={{ height: paddingTop }} aria-hidden />}
            {virtualRows.map((virtualRow) => {
              const row = rows[virtualRow.index];
              const id = row.original._business_id;
              return (
                <tr
                  key={id}
                  className={selected.has(id) ? "selected" : ""}
                  style={{ height: ROW_HEIGHT }}
                >
                  <td>
                    <input
                      type="checkbox"
                      checked={selected.has(id)}
                      onChange={(e) =>
                        setSelected((prev) => {
                          const next = new Set(prev);
                          if (e.target.checked) next.add(id);
                          else next.delete(id);
                          return next;
                        })
                      }
                    />
                  </td>
                  <td>
                    <button
                      className="small"
                      style={{ padding: "0 5px" }}
                      onClick={() => setExpanded(row.original)}
                      title="Evidence panel — where each value came from"
                    >
                      +
                    </button>
                  </td>
                  <td style={{ textAlign: "center" }}>
                    {row.original._extracted && (
                      <span
                        className="badge done"
                        title="Already extracted — the next pull skips it. Clear it from the Extracted screen to make it eligible again."
                      >
                        ✓
                      </span>
                    )}
                  </td>
                  <td className="small">
                    <BatchCell row={row.original} catalogue={catalogue} />
                  </td>
                  {row.getVisibleCells().map((cell) => (
                    <td
                      key={cell.id}
                      className={
                        (cell.column.columnDef.meta as any)?.numeric ? "num" : ""
                      }
                    >
                      {flexRender(cell.column.columnDef.cell, cell.getContext())}
                    </td>
                  ))}
                </tr>
              );
            })}
            {paddingBottom > 0 && <tr style={{ height: paddingBottom }} aria-hidden />}
            {data?.total === 0 && (
              <tr>
                <td colSpan={visibleColumns.length + 4}>
                  <EmptyState filters={filters} catalogue={catalogue} />
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>

      {expanded && (
        <EvidenceDrawer row={expanded} onClose={() => setExpanded(null)} />
      )}
    </>
  );
}

/**
 * What the Extract click produced.
 *
 * The numbers are shown, not just copied. Three reasons, all of them the same
 * reason: the businesses are marked *before* the clipboard write can fail, a
 * browser can refuse `navigator.clipboard` after an `await`, and a clipboard is
 * invisible until you paste. A pull the operator cannot see is a pull they have
 * to redo — except the rows are already marked, so they cannot.
 */
function PullPanel({
  pull,
  copied,
  onClose,
}: {
  pull: ExtractionResult;
  copied: boolean;
  onClose: () => void;
}) {
  const [recopied, setRecopied] = useState(copied);

  return (
    <div className={`notice ${copied ? "info" : "warn"}`}>
      <div className="row" style={{ alignItems: "center" }}>
        <b>
          {copied
            ? `Copied ${pull.numbers.length} number(s) to the clipboard.`
            : `The browser refused the clipboard. ${pull.numbers.length} number(s) below.`}
        </b>
        <span className="muted small">
          {pull.marked} business(es) marked extracted
          {pull.skipped_already_extracted > 0 &&
            ` · ${pull.skipped_already_extracted} skipped as already extracted`}
          {pull.remaining > 0 && ` · ${pull.remaining} still un-extracted below them`}
        </span>
        <span className="spacer" />
        <button
          onClick={async () => setRecopied(await copyToClipboard(pull.clipboard))}
        >
          {recopied ? "Copied ✓" : "Copy again"}
        </button>
        <Link href="/extracted">
          <button>Extracted list</button>
        </Link>
        <button onClick={onClose}>Dismiss</button>
      </div>

      {/* §10.2's rule, restated for this screen: a short clipboard is explained,
          never left for the operator to infer from a count that does not add up. */}
      {pull.warnings.map((w) => (
        <div key={w} className="small" style={{ marginTop: 6 }}>
          {w}
        </div>
      ))}

      <div className="log" style={{ marginTop: 8, maxHeight: 180 }}>
        {pull.numbers.join("\n") || "No confirmed or likely numbers in this batch."}
      </div>
    </div>
  );
}

/**
 * The batch this row is in, and its position within it.
 *
 * `unbatched` renders as a muted dash rather than as a badge: it is not a
 * seventh batch, it is the absence of a definition covering this business's
 * category, and giving it the same visual weight as `B01` would imply there is
 * a message for it.
 */
function BatchCell({
  row,
  catalogue,
}: {
  row: LeadRow;
  catalogue: BatchCatalogue | null;
}) {
  const spec = catalogue?.batches.find((b) => b.slug === row._batch);
  if (!spec) {
    return (
      <span
        className="muted"
        title={
          catalogue
            ? `No batch: the cascade is defined for ${catalogue.categories.join(", ")} only.`
            : "No batch."
        }
      >
        —
      </span>
    );
  }
  return (
    <span
      className={`badge ${spec.sendable ? "likely" : "throttled"}`}
      title={`${spec.name} — ${spec.definition}${
        row._send_rank ? ` · send rank ${row._send_rank} in this view` : ""
      }`}
    >
      {spec.id}
      {row._send_rank ? <span className="muted"> #{row._send_rank}</span> : null}
    </span>
  );
}

/**
 * The selected batch, spelled out.
 *
 * The operator is about to copy tens of numbers and write *one* message for all
 * of them, so the thing worth putting on screen is not the row count — it is
 * what these businesses have in common and what the spec says not to say to
 * them. B06's "never reference the rating" is a sentence that saves a thread.
 */
function BatchNote({ batch }: { batch: BatchInfo }) {
  return (
    <div className="notice info small" style={{ marginTop: 10 }}>
      <b>
        {batch.id} · {batch.name}
      </b>
      {batch.send_priority !== null ? (
        <span className="muted"> · send priority {batch.send_priority}</span>
      ) : (
        <span className="muted"> · not a send batch</span>
      )}
      <div style={{ marginTop: 4 }}>{batch.definition}</div>
      <div style={{ marginTop: 4 }}>{batch.note}</div>
    </div>
  );
}

/** §12.1's values, rendered. `null` is always a blank, never a 0 or a dash-as-data. */
function renderCell(column: string, value: unknown) {
  if (value === null || value === undefined || value === "") {
    return <span className="muted">—</span>;
  }
  if (column.endsWith("_whatsapp")) {
    // §13: green on `confirmed`, grey on `likely`.
    return <span className={`badge ${value}`}>{String(value)}</span>;
  }
  if (column === "lead_score") {
    return <b className="mono">{String(value)}</b>;
  }
  if (column.startsWith("phone_") && !column.includes("_")) {
    return <span className="mono">{String(value)}</span>;
  }
  if (/^phone_\d$/.test(column)) {
    return <span className="mono">{String(value)}</span>;
  }
  if (column === "website" || column.endsWith("_url") || column === "maps_url") {
    return (
      <a href={String(value)} target="_blank" rel="noreferrer">
        {String(value).replace(/^https?:\/\//, "").slice(0, 40)}
      </a>
    );
  }
  return String(value);
}

/** §13: "row expand → evidence panel with source URLs". §1: provenance survives. */
function EvidenceDrawer({ row, onClose }: { row: LeadRow; onClose: () => void }) {
  const sources = String(row.sources ?? "").split("|").filter(Boolean);
  const urls = String(row.evidence_urls ?? "").split("|").filter(Boolean);
  return (
    <div className="drawer">
      <button className="drawer-close" onClick={onClose} aria-label="Close">
        ×
      </button>
      <b>{row.business_name}</b>
      <div className="small muted" style={{ margin: "6px 0" }}>
        {String(row.address ?? "")} · scraped {String(row.scraped_at ?? "—")}
      </div>
      <div style={{ marginBottom: 8 }}>
        {sources.map((s) => (
          <span key={s} className="badge likely" style={{ marginRight: 6 }}>
            {s}
          </span>
        ))}
      </div>
      <div className="small">
        {urls.map((u) => (
          <div key={u}>
            <a href={u} target="_blank" rel="noreferrer">
              {u}
            </a>
          </div>
        ))}
        {urls.length === 0 && <span className="muted">No recorded evidence URLs.</span>}
      </div>
      {row.phone_count != null && Number(row.phone_count) > 4 && (
        <div className="notice small">
          This business has <b>{String(row.phone_count)}</b> numbers; §12.1 shows the
          top 4 by rank. The others are kept, not discarded (§10.1).
        </div>
      )}
    </div>
  );
}

/**
 * The empty table.
 *
 * §10.2 measured that a discovery-only run has **0 qualified leads by
 * construction** — every Maps number is a §9.3 `likely` at 0.60 and nothing
 * lifts it over 60. Four such runs are in the database. Showing "no results"
 * there would read as a broken filter rather than as a missing stage.
 */
function EmptyState({
  filters,
  catalogue,
}: {
  filters: TableFilters;
  catalogue: BatchCatalogue | null;
}) {
  if (filters.batches.length && catalogue) {
    // The likeliest reason a batch is empty is that this run is not food, and
    // "no rows match" would send the operator hunting through the filter bar
    // for a mistake they did not make.
    return (
      <div>
        <b>Nothing in this batch.</b>
        <div className="small muted" style={{ marginTop: 6 }}>
          The cascade is defined for <b>{catalogue.categories.join(", ")}</b> only —
          every business in another category is in no batch at all, so on a
          non-food run each of the seven reads 0. Pick <i>no batch defined</i> to
          see those rows, or <i>every batch</i> to clear the filter.
        </div>
      </div>
    );
  }
  if (filters.minScore !== null && filters.minScore >= 60) {
    return (
      <div>
        <b>No leads at or above {filters.minScore}.</b>
        <div className="small muted" style={{ marginTop: 6 }}>
          If this run was discovery-only, that is expected rather than a fault: a
          Maps listing alone is a §9.3 <i>likely</i> at 0.60, and nothing in the
          record lifts a business over 60 until the §5.2 website pass runs. Re-run
          stage 2 (contact enrichment) from the run&apos;s progress screen.
        </div>
      </div>
    );
  }
  if (filters.whatsapp.includes("confirmed")) {
    return (
      <div>
        <b>No confirmed WhatsApp numbers.</b>
        <div className="small muted" style={{ marginTop: 6 }}>
          Only a business&apos;s own site can prove a number takes WhatsApp (§5.2), and
          confirmation rates vary enormously by slice — 45% of crawled Islamabad
          domains against 13% in Lahore, on identical code.
        </div>
      </div>
    );
  }
  return <span className="muted">No rows match these filters.</span>;
}
