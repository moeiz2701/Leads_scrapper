/**
 * The backend, as types.
 *
 * Every vocabulary here — cities, categories, synonyms, number preferences —
 * is *fetched*, never hard-coded. implementation.md §4.2 calls the synonym
 * dictionary "the highest-leverage config", and a second copy of it in
 * TypeScript would be the fastest way to have the UI and the scraper disagree
 * about what a run does.
 */

export type LeadRow = Record<string, unknown> & {
  business_name: string;
  lead_score: number | null;
  _business_id: string;
  _run_id: string;
  /** On the extraction ledger. A decoration — never a filter (see services/results.py). */
  _extracted: boolean;
  /** _BATCH_SPEC slug, or `unbatched` for a category the cascade does not cover. */
  _batch: string;
  /** §2's derived number — the one this business would be messaged on. */
  _wa_number: string | null;
  _wa_confidence: string | null;
  /** §6 — position within its batch by review_count desc, in *this* view. */
  _send_rank: number | null;
};

export interface ResultsResponse {
  rows: LeadRow[];
  total: number;
  columns: string[];
  compact_columns: string[];
  numeric_columns: string[];
  suppressed_contacts: number;
  suppressed_businesses: number;
  collapsed: number;
  cities: string[];
  categories: string[];
  /** Slug → size across the whole view, *before* the batch filter narrowed it. */
  batch_counts: Record<string, number>;
}

/** One outreach batch of _BATCH_SPEC.md §4. */
export interface BatchInfo {
  id: string;
  slug: string;
  name: string;
  definition: string;
  send_priority: number | null;
  sendable: boolean;
  note: string;
}

/**
 * The batch vocabulary, fetched rather than hard-coded.
 *
 * `categories` is the honest limit of the layer: the cascade's thresholds and
 * its dine-in list were calibrated on one Lahore × food scrape, so it covers
 * `food` and nothing else. Every other category's rows carry `unbatched_slug`,
 * and the UI has to say why rather than showing seven empty batches.
 */
export interface BatchCatalogue {
  batches: BatchInfo[];
  unbatched_slug: string;
  categories: string[];
  note: string;
}

export interface RunSummary {
  id: string;
  city: string | null;
  category: string | null;
  number_preference: string;
  status: string;
  mode: string;
  businesses: number;
  qualified: number;
  created_at: string | null;
  started_at: string | null;
  finished_at: string | null;
  error: string | null;
}

export interface StageProgress {
  stage: string;
  state: "pending" | "running" | "done" | "unimplemented" | "failed" | "skipped";
  processed: number | null;
  produced: number | null;
  elapsed_seconds: number | null;
  detail: Record<string, unknown>;
  note: string | null;
}

export interface SourcePill {
  source: string;
  status: string;
  detail: string | null;
  updated_at: string | null;
}

export interface RunDetail extends RunSummary {
  subcategories: string[];
  sources_enabled: Record<string, unknown>;
  stats: Record<string, any>;
  stages: StageProgress[];
  sources: SourcePill[];
  queue_depths: Record<string, number>;
  /** §10.2 — 85 until Phase 9 lands §8's attribution engine. */
  unattributed_ceiling: number | null;
}

export interface City {
  name: string;
  tier: number;
  area_code: string;
  tiles: string[];
  tile_count: number;
}

export interface CategoryInfo {
  name: string;
  synonyms: string[];
  synonym_count: number;
  vertical_sources: string[];
  vertical_strength: "strong" | "weak" | "none";
  volume_drivers: string[];
}

export interface PreferenceInfo {
  value: string;
  label: string;
  filters: boolean;
  note: string;
}

export interface Range {
  low: number;
  high: number;
}

export interface PriorRun {
  run_id: string;
  status: string;
  queries: number;
  businesses: number;
  with_phone: number;
  qualified: number;
  enriched: boolean;
}

/**
 * §13 Screen 1's estimate.
 *
 * `available` and `qualified` are deliberately nullable. §5.2 forbids
 * extrapolating one slice's rate into this figure — Islamabad and Lahore ran
 * identical code and confirmed WhatsApp at 45% against 13% — so the backend
 * returns `null` with a basis of `no_prior_run` rather than a number it cannot
 * justify. The UI must render that absence, not paper over it with a zero.
 */
export interface RunEstimate {
  queries: number;
  runtime_minutes: Range | null;
  runtime_basis: string;
  prior_runs: PriorRun[];
  available: Range | null;
  available_basis: string;
  qualified: Range | null;
  caveats: string[];
}

/** The three sizes the Extract control offers — services/extraction.BATCH_SIZES. */
export const BATCH_SIZES = [30, 50, 100] as const;
export type BatchSize = (typeof BATCH_SIZES)[number];

/**
 * Drain the filtered view — every un-extracted row in it, however many.
 *
 * Spelled out rather than expressed as an absent limit, matching the backend:
 * "no limit" arrived at by omission is one dropped field between pulling a batch
 * and emptying the queue.
 */
export const EXTRACT_ALL = "all" as const;
export type ExtractLimit = BatchSize | typeof EXTRACT_ALL;

export interface ExtractedBusiness {
  business_id: string;
  run_id: string;
  name: string;
  city: string | null;
  lead_score: number | null;
  website: string | null;
  numbers: string[];
  batch: string | null;
}

/**
 * One Extract click, and what it could not do.
 *
 * `clipboard` is built server-side so the "one number per line" format has a
 * single definition with a test on it, rather than being re-decided here.
 */
export interface ExtractionResult {
  batch_id: string | null;
  clipboard: string;
  numbers: string[];
  businesses: ExtractedBusiness[];
  /** `null` = the pull was asked to drain the view rather than take a count. */
  requested: number | null;
  marked: number;
  skipped_already_extracted: number;
  without_numbers: number;
  remaining: number;
  warnings: string[];
}

export interface ExtractionEntry {
  id: string;
  business_id: string;
  run_id: string;
  batch_id: string;
  /** `null` = an "extract all" pull rather than a fixed top-N. */
  batch_size: number | null;
  /** Recorded at pull time. `null` on rows pulled before the column existed. */
  batch: string | null;
  /** The cascade against the business as it stands now — may differ from `batch`. */
  current_batch: string | null;
  /** As sent, not as the business stands now — see services/extraction.py. */
  numbers: string[];
  extracted_at: string | null;
  business_name: string | null;
  city: string | null;
  category: string | null;
  website: string | null;
  lead_score: number | null;
  run_city: string | null;
  run_category: string | null;
}

export interface SuppressionEntry {
  id: string;
  value_e164: string | null;
  email: string | null;
  domain: string | null;
  reason: string | null;
  added_by: string | null;
  created_at: string | null;
}

export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly detail: unknown,
  ) {
    super(typeof detail === "string" ? detail : JSON.stringify(detail));
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    ...init,
    headers: { "Content-Type": "application/json", ...(init?.headers ?? {}) },
    cache: "no-store",
  });
  if (!response.ok) {
    let detail: unknown = await response.text();
    try {
      detail = JSON.parse(detail as string).detail ?? detail;
    } catch {
      /* a non-JSON body is the message */
    }
    throw new ApiError(response.status, detail);
  }
  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

export const api = {
  health: () => request<{ status: string; queue: Record<string, unknown> }>("/api/health"),

  cities: () => request<City[]>("/api/meta/cities"),
  categories: () => request<CategoryInfo[]>("/api/meta/categories"),
  batches: () => request<BatchCatalogue>("/api/meta/batches"),
  preferences: () => request<PreferenceInfo[]>("/api/meta/number-preferences"),
  stages: () =>
    request<{ implemented: string[]; missing: { stage: string; phase: string }[] }>(
      "/api/meta/stages",
    ),
  settings: () => request<Record<string, any>>("/api/meta/settings"),

  estimate: (body: {
    city: string;
    category: string;
    synonym_limit?: number | null;
    tile_limit?: number | null;
    // §6 Stage 3. Sent because it dominates the runtime when on — the social
    // pass renders a browser per business at §6.6's 8-20s pacing — and an
    // estimate that silently omits it is the one thing this screen must not do.
    social?: boolean;
  }) =>
    request<RunEstimate>("/api/meta/estimate", {
      method: "POST",
      body: JSON.stringify(body),
    }),

  createRun: (body: unknown) =>
    request<{
      run: RunSummary;
      stages_planned: string[];
      stages_unavailable: string[];
      warnings: string[];
    }>("/api/runs", { method: "POST", body: JSON.stringify(body) }),

  listRuns: () => request<RunSummary[]>("/api/runs"),
  getRun: (id: string) => request<RunDetail>(`/api/runs/${id}`),
  cancelRun: (id: string) =>
    request<RunDetail>(`/api/runs/${id}/cancel`, { method: "POST" }),
  rerunStage: (id: string, stage: string) =>
    request<RunDetail>(`/api/runs/${id}/stages/${stage}`, { method: "POST" }),
  setPreference: (id: string, preference: string) =>
    request<RunDetail>(`/api/runs/${id}/preference`, {
      method: "PATCH",
      body: JSON.stringify({ number_preference: preference }),
    }),

  results: (query: string) => request<ResultsResponse>(`/api/results?${query}`),

  /**
   * Extract the top N of the current view.
   *
   * `query` is the *same* string the table and the Export button use — the
   * backend reads it through the one `result_query` dependency, so "extract
   * whatever filter is applied" is guaranteed by construction. Only the batch
   * size travels in the body.
   */
  extract: (query: string, limit: ExtractLimit) =>
    request<ExtractionResult>(`/api/extractions?${query}`, {
      method: "POST",
      body: JSON.stringify({ limit }),
    }),

  extractions: (runIds: string[] = [], batches: string[] = []) => {
    const params = new URLSearchParams();
    runIds.forEach((id) => params.append("run", id));
    if (batches.length) params.set("batch", batches.join(","));
    return request<ExtractionEntry[]>(`/api/extractions?${params.toString()}`);
  },
  /** Ledger size per batch — deliberately *not* scoped to the batch filter. */
  extractionCounts: (runIds: string[] = []) => {
    const params = new URLSearchParams();
    runIds.forEach((id) => params.append("run", id));
    return request<Record<string, number>>(
      `/api/extractions/counts?${params.toString()}`,
    );
  },
  clearExtraction: (id: string) =>
    request<void>(`/api/extractions/${id}`, { method: "DELETE" }),
  /**
   * Clear the ledger — scoped to the same run *and batch* the list is showing.
   *
   * The batch scope is not optional politeness: a clear that ignored the filter
   * would delete a run's whole ledger from a screen displaying 22 rows of it.
   */
  clearExtractions: (runIds: string[] = [], batches: string[] = []) => {
    const params = new URLSearchParams();
    runIds.forEach((id) => params.append("run", id));
    if (batches.length) params.set("batch", batches.join(","));
    return request<{ cleared: number }>(`/api/extractions?${params.toString()}`, {
      method: "DELETE",
    });
  },

  suppressions: () => request<SuppressionEntry[]>("/api/do-not-contact"),
  addSuppression: (body: unknown) =>
    request<SuppressionEntry>("/api/do-not-contact", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  bulkDelete: (body: unknown) =>
    request<{
      businesses_deleted: number;
      contacts_deleted: number;
      suppressions_added: number;
      numbers_suppressed: string[];
      domains_suppressed: string[];
      warnings: string[];
    }>("/api/do-not-contact/bulk-delete", {
      method: "POST",
      body: JSON.stringify(body),
    }),
};

/**
 * §13 Screen 3's filter bar → the query string both the table and the CSV read.
 *
 * §12.2 requires the export to "respect the active table filters and sort
 * order". The backend guarantees that by giving both endpoints one filter
 * dependency; this function is the client-side half of the same promise —
 * the Export button reuses exactly this string.
 */
export interface TableFilters {
  runIds: string[];
  whatsapp: string[];
  hasOwnerName: boolean | null;
  /**
   * Three states: `null` any, `true` a site is on record, `false` none is.
   *
   * No longer the table's major split — the batch cascade is, and it carries the
   * website distinction inside it (`delivery-site` vs `delivery-nosite`). Kept
   * because Screen 2 deep-links into one half of a run with `?website=yes|no`,
   * and because it is the only control that means "sited, whatever the vertical"
   * on the six categories the cascade does not cover. Shown as a chip when set.
   */
  hasWebsite: boolean | null;
  /** _BATCH_SPEC slugs, or `unbatched`. Empty = every batch. */
  batches: string[];
  minScore: number | null;
  lineTypes: string[];
  sources: string[];
  search: string;
  sort: string;
  descending: boolean;
  collapse: boolean;
}

export const emptyFilters: TableFilters = {
  runIds: [],
  whatsapp: [],
  hasOwnerName: null,
  hasWebsite: null,
  batches: [],
  minScore: null,
  lineTypes: [],
  sources: [],
  search: "",
  sort: "lead_score",
  descending: true,
  collapse: false,
};

export function toQueryString(filters: TableFilters): string {
  const params = new URLSearchParams();
  filters.runIds.forEach((id) => params.append("run", id));
  if (filters.whatsapp.length) params.set("whatsapp", filters.whatsapp.join(","));
  if (filters.hasOwnerName !== null)
    params.set("has_owner_name", String(filters.hasOwnerName));
  if (filters.hasWebsite !== null) params.set("has_website", String(filters.hasWebsite));
  if (filters.batches.length) params.set("batch", filters.batches.join(","));
  if (filters.minScore !== null) params.set("min_score", String(filters.minScore));
  if (filters.lineTypes.length) params.set("line_type", filters.lineTypes.join(","));
  if (filters.sources.length) params.set("source", filters.sources.join(","));
  if (filters.search.trim()) params.set("q", filters.search.trim());
  params.set("sort", filters.sort);
  params.set("order", filters.descending ? "desc" : "asc");
  if (filters.collapse) params.set("collapse", "true");
  return params.toString();
}

/**
 * Put text on the clipboard, and say whether it worked.
 *
 * `navigator.clipboard` needs a secure context and is refused by some browsers
 * once an `await` has separated the write from the click that caused it — which
 * is exactly this flow, because the numbers come back from a POST. The
 * `execCommand` path is the fallback, and the boolean is the point: the caller
 * has to be able to show the numbers on screen rather than claim a copy that
 * never happened. Losing a pull to a silent clipboard failure would leave the
 * businesses marked extracted with nothing to show for it.
 */
export async function copyToClipboard(text: string): Promise<boolean> {
  if (!text) return false;
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    /* fall through to the legacy path */
  }
  try {
    const area = document.createElement("textarea");
    area.value = text;
    area.style.position = "fixed";
    area.style.opacity = "0";
    document.body.appendChild(area);
    area.select();
    const ok = document.execCommand("copy");
    area.remove();
    return ok;
  } catch {
    return false;
  }
}

/**
 * Download the CSV.
 *
 * Goes straight to the API rather than through the Next rewrite so the
 * `Content-Disposition` filename the server built (§12.2's
 * `{city}_{category}_{YYYYMMDD}_{n}leads.csv`) survives — a client-invented
 * filename would drift from the server's row count the moment a filter changed.
 */
export async function downloadCsv(path: string, query: string): Promise<void> {
  const response = await fetch(`${path}?${query}`);
  if (!response.ok) throw new ApiError(response.status, await response.text());

  const disposition = response.headers.get("content-disposition") ?? "";
  const match = /filename="([^"]+)"/.exec(disposition);
  const blob = await response.blob();
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = match?.[1] ?? "leads.csv";
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  URL.revokeObjectURL(url);
}
