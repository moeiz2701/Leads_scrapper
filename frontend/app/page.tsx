"use client";

/**
 * §13 Screen 1 — New Run.
 *
 * The estimate line is the part worth reading carefully. §13's mockup shows
 * "Est. runtime: ~55 min · Est. available: ~780", and §5.2 forbids the second
 * of those numbers: *"Measure per slice; do not extrapolate one run's
 * confirmation rate into the §13 estimated-available figure."* Islamabad and
 * Lahore ran identical code on the same category and confirmed WhatsApp at 45%
 * against 13%.
 *
 * So this screen splits the line in two. Runtime is a property of our own query
 * plan and §7 pacing, and is shown as a measured range. Availability is a
 * property of the market, and is shown **only where this exact city × category
 * has been run before** — otherwise the screen says there is no basis, which
 * §14 backs up: a narrow pair is honestly 30–50, not several hundred.
 */

import { useCallback, useEffect, useMemo, useState } from "react";
import { useRouter } from "next/navigation";
import {
  api,
  ApiError,
  type CategoryInfo,
  type City,
  type PreferenceInfo,
  type RunEstimate,
} from "@/lib/api";

const CORE_SOURCES = [
  { key: "google_maps", label: "Google Maps", note: "core · ~70% of all leads (§5.1)" },
  { key: "business_website", label: "Business websites", note: "core · the WhatsApp confirmation engine (§5.2)" },
  { key: "directories", label: "Directories", note: "Phase 6 — will not contribute yet (§5.3)" },
] as const;

const SOCIAL_SOURCES = [
  { key: "facebook", label: "Facebook Pages", note: "Phase 8 (§6)" },
  { key: "instagram", label: "Instagram", note: "Phase 8 (§6)" },
] as const;

export default function NewRunScreen() {
  const router = useRouter();
  const [cities, setCities] = useState<City[]>([]);
  const [categories, setCategories] = useState<CategoryInfo[]>([]);
  const [preferences, setPreferences] = useState<PreferenceInfo[]>([]);

  const [city, setCity] = useState("");
  const [category, setCategory] = useState("");
  const [subcategories, setSubcategories] = useState<string[]>([]);
  const [preference, setPreference] = useState("owner_first");
  const [sources, setSources] = useState<Record<string, boolean>>({
    google_maps: true,
    business_website: true,
    directories: false,
    facebook: false,
    instagram: false,
  });
  const [targetLeads, setTargetLeads] = useState<number | "">(500);
  const [synonymLimit, setSynonymLimit] = useState<number | "">("");
  const [tileLimit, setTileLimit] = useState<number | "">("");

  const [estimate, setEstimate] = useState<RunEstimate | null>(null);
  const [estimating, setEstimating] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  useEffect(() => {
    Promise.all([api.cities(), api.categories(), api.preferences()])
      .then(([c, cat, pref]) => {
        setCities(c);
        setCategories(cat);
        setPreferences(pref);
        setCity(c[0]?.name ?? "");
        setCategory(cat[0]?.name ?? "");
      })
      .catch((e) => setError(String(e)));
  }, []);

  const selectedCategory = useMemo(
    () => categories.find((c) => c.name === category),
    [categories, category],
  );

  const refreshEstimate = useCallback(() => {
    if (!city || !category) return;
    setEstimating(true);
    api
      .estimate({
        city,
        category,
        synonym_limit: synonymLimit === "" ? null : synonymLimit,
        tile_limit: tileLimit === "" ? null : tileLimit,
      })
      .then(setEstimate)
      .catch((e) => setError(String(e)))
      .finally(() => setEstimating(false));
  }, [city, category, synonymLimit, tileLimit]);

  useEffect(() => {
    refreshEstimate();
  }, [refreshEstimate]);

  useEffect(() => setSubcategories([]), [category]);

  async function start() {
    setSubmitting(true);
    setError(null);
    try {
      const created = await api.createRun({
        city,
        category,
        subcategories,
        number_preference: preference,
        sources,
        target_leads: targetLeads === "" ? null : targetLeads,
        synonym_limit: synonymLimit === "" ? null : synonymLimit,
        tile_limit: tileLimit === "" ? null : tileLimit,
      });
      router.push(`/runs/${created.run.id}`);
    } catch (e) {
      // The API refuses a run it cannot perform (an unimplemented source, a
      // missing §7.1 proxy) with a structured explanation. Surfacing the
      // message rather than a status code is the whole point of it being
      // structured.
      if (e instanceof ApiError && e.detail && typeof e.detail === "object") {
        const detail = e.detail as { error?: string; message?: string };
        setError([detail.error, detail.message].filter(Boolean).join(" — "));
      } else {
        setError(String(e));
      }
      setSubmitting(false);
    }
  }

  return (
    <>
      <h1>New run</h1>
      <p className="subtitle">§13 Screen 1 · city × category discovery</p>

      {error && <div className="notice bad">{error}</div>}

      <div className="grid-2">
        <div className="panel">
          <div className="row">
            <div className="field">
              <label htmlFor="city">City</label>
              <select id="city" value={city} onChange={(e) => setCity(e.target.value)}>
                {cities.map((c) => (
                  <option key={c.name} value={c.name}>
                    {c.name} · tier {c.tier} · {c.tile_count} tiles
                  </option>
                ))}
              </select>
            </div>
            <div className="field">
              <label htmlFor="category">Category</label>
              <select
                id="category"
                value={category}
                onChange={(e) => setCategory(e.target.value)}
              >
                {categories.map((c) => (
                  <option key={c.name} value={c.name}>
                    {c.name}
                  </option>
                ))}
              </select>
            </div>
          </div>

          {selectedCategory && (
            <>
              <label style={{ marginTop: 16 }}>
                Subcategory · §4.2 synonyms ({selectedCategory.synonym_count})
              </label>
              <div style={{ display: "flex", flexWrap: "wrap", gap: 10 }}>
                {selectedCategory.synonyms.map((s) => (
                  <label key={s} className="check">
                    <input
                      type="checkbox"
                      checked={subcategories.includes(s)}
                      onChange={(e) =>
                        setSubcategories((prev) =>
                          e.target.checked ? [...prev, s] : prev.filter((x) => x !== s),
                        )
                      }
                    />
                    {s}
                  </label>
                ))}
              </div>
              {selectedCategory.vertical_strength === "none" && (
                <div className="notice small">
                  §4: no vertical directory exists for <b>{selectedCategory.name}</b>.
                  Google Maps carries this entire run.
                </div>
              )}
            </>
          )}

          <h2>Number preference</h2>
          {preferences.map((p) => (
            <label key={p.value} className="check">
              <input
                type="radio"
                name="pref"
                checked={preference === p.value}
                onChange={() => setPreference(p.value)}
              />
              <span>
                {p.label}
                {p.filters && <span className="badge throttled" style={{ marginLeft: 8 }}>filters</span>}
                <div className="small muted">{p.note}</div>
              </span>
            </label>
          ))}

          <h2>Sources</h2>
          {CORE_SOURCES.map((s) => (
            <label key={s.key} className="check">
              <input
                type="checkbox"
                checked={sources[s.key]}
                onChange={(e) =>
                  setSources((prev) => ({ ...prev, [s.key]: e.target.checked }))
                }
              />
              <span>
                {s.label} <span className="small muted">{s.note}</span>
              </span>
            </label>
          ))}
          {SOCIAL_SOURCES.map((s) => (
            <label key={s.key} className="check">
              {/* Disabled rather than hidden: §6 is a real part of the design and
                  the operator should see it exists and is not ready, instead of
                  wondering why the doc mentions a source the UI does not. */}
              <input type="checkbox" disabled checked={false} readOnly />
              <span className="muted">
                {s.label} <span className="small">{s.note}</span>
              </span>
            </label>
          ))}

          <h2>Plan</h2>
          <div className="row">
            <div className="field">
              <label htmlFor="target">Target leads</label>
              <input
                id="target"
                type="number"
                value={targetLeads}
                onChange={(e) =>
                  setTargetLeads(e.target.value === "" ? "" : Number(e.target.value))
                }
              />
            </div>
            <div className="field">
              <label htmlFor="syn">Synonyms (blank = all)</label>
              <input
                id="syn"
                type="number"
                min={1}
                value={synonymLimit}
                onChange={(e) =>
                  setSynonymLimit(e.target.value === "" ? "" : Number(e.target.value))
                }
              />
            </div>
            <div className="field">
              <label htmlFor="tiles">Tiles (blank = all)</label>
              <input
                id="tiles"
                type="number"
                min={1}
                value={tileLimit}
                onChange={(e) =>
                  setTileLimit(e.target.value === "" ? "" : Number(e.target.value))
                }
              />
            </div>
          </div>
          <div className="notice small">
            §14, after Phase 2&apos;s measurements: <b>tune the plan down, not up</b>. 3–4
            synonyms × 6–8 tiles is likely enough for a broad category in a tier-1
            city; 8 queries over 4 near-synonyms measured a 67% duplicate rate.
          </div>
        </div>

        <div>
          <EstimatePanel estimate={estimate} loading={estimating} />
          <button
            className="primary"
            onClick={start}
            disabled={submitting || !city || !category}
            style={{ width: "100%", padding: 12, fontSize: 15 }}
          >
            {submitting ? "Starting…" : "Start run"}
          </button>
        </div>
      </div>
    </>
  );
}

function EstimatePanel({
  estimate,
  loading,
}: {
  estimate: RunEstimate | null;
  loading: boolean;
}) {
  if (!estimate) {
    return (
      <div className="panel">
        <h2 style={{ marginTop: 0 }}>Estimate</h2>
        <p className="muted">{loading ? "Measuring…" : "Pick a city and category."}</p>
      </div>
    );
  }

  return (
    <div className="panel">
      <h2 style={{ marginTop: 0 }}>Estimate</h2>

      <div className="row" style={{ gap: 24 }}>
        <div>
          <label>Queries</label>
          <div style={{ fontSize: 22 }} className="mono">
            {estimate.queries}
          </div>
        </div>
        <div>
          <label>Est. runtime</label>
          <div style={{ fontSize: 22 }} className="mono">
            {estimate.runtime_minutes
              ? `${Math.round(estimate.runtime_minutes.low)}–${Math.round(
                  estimate.runtime_minutes.high,
                )} min`
              : "—"}
          </div>
          <div className="small muted">basis: {estimate.runtime_basis}</div>
        </div>
        <div>
          <label>Est. available</label>
          {estimate.available ? (
            <>
              <div style={{ fontSize: 22 }} className="mono">
                {estimate.available.low === estimate.available.high
                  ? estimate.available.low
                  : `${estimate.available.low}–${estimate.available.high}`}
              </div>
              <div className="small muted">measured, this slice</div>
            </>
          ) : (
            /* The refusal, rendered as a refusal. A dash here is the honest
               answer and §5.2 requires it; an invented number is the one thing
               this panel must never show. */
            <>
              <div style={{ fontSize: 22 }} className="muted">
                no basis
              </div>
              <div className="small muted">never run · {estimate.available_basis}</div>
            </>
          )}
        </div>
      </div>

      {estimate.qualified && (
        <div style={{ marginTop: 12 }}>
          <label>Qualified last time (≥ 60 + a mobile)</label>
          <div className="mono">
            {estimate.qualified.low === estimate.qualified.high
              ? estimate.qualified.low
              : `${estimate.qualified.low}–${estimate.qualified.high}`}
          </div>
        </div>
      )}

      {estimate.prior_runs.length > 0 && (
        <>
          <h2>Prior runs of this slice</h2>
          <table>
            <thead>
              <tr>
                <th>Status</th>
                <th className="num">Queries</th>
                <th className="num">Businesses</th>
                <th className="num">Qualified</th>
                <th>Enriched</th>
              </tr>
            </thead>
            <tbody>
              {estimate.prior_runs.slice(0, 6).map((p) => (
                <tr key={p.run_id}>
                  <td>
                    <span className={`badge ${p.status}`}>{p.status}</span>
                  </td>
                  <td className="num mono">{p.queries || "—"}</td>
                  <td className="num mono">{p.businesses}</td>
                  <td className="num mono">{p.qualified}</td>
                  <td>{p.enriched ? "yes" : <span className="muted">no</span>}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </>
      )}

      {estimate.caveats.map((c, i) => (
        <div key={i} className="notice small warn">
          {c}
        </div>
      ))}
    </div>
  );
}
