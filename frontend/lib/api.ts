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
