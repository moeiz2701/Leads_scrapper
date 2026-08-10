"use client";

/**
 * §13 Settings.
 *
 * Read-only, deliberately. §13 lists proxy credentials, per-source rate limits,
 * API keys, cache TTL, the dedupe threshold and concurrency — all of which
 * already resolve through `config.Settings`, the codebase's single reader of
 * `os.environ`. A settings *write* path would introduce a second source of truth
 * that could disagree with the `.env` file on disk, and for a single-operator
 * tool "edit .env and restart" is both honest and shorter than the code that
 * would replace it.
 *
 * It also carries the two things the operator most needs to *see*: whether
 * anything is consuming the queues, and the per-slice confirmation spread that
 * makes §13 Screen 1 refuse to estimate availability.
 */

import { useEffect, useState } from "react";
import { api, type SuppressionEntry } from "@/lib/api";

export default function SettingsScreen() {
  const [settings, setSettings] = useState<Record<string, any> | null>(null);
  const [suppressions, setSuppressions] = useState<SuppressionEntry[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [form, setForm] = useState({ value_e164: "", domain: "", reason: "" });

  function refresh() {
    api.settings().then(setSettings).catch((e) => setError(String(e)));
    api.suppressions().then(setSuppressions).catch((e) => setError(String(e)));
  }

  useEffect(refresh, []);

  async function addSuppression(event: React.FormEvent) {
    event.preventDefault();
    try {
      await api.addSuppression({
        value_e164: form.value_e164 || null,
        domain: form.domain || null,
        reason: form.reason || "added from Settings",
      });
      setForm({ value_e164: "", domain: "", reason: "" });
      refresh();
    } catch (e) {
      setError(String(e));
    }
  }

  if (!settings) return <p className="muted">{error ?? "Loading…"}</p>;

  const queue = settings.queue ?? {};
  const noConsumer = queue.redis && !queue.workers && !queue.sync_mode;

  return (
    <>
      <h1>Settings</h1>
      <p className="subtitle">
        Read-only. Everything here resolves through <span className="mono">config.Settings</span> —
        edit <span className="mono">.env</span> and restart.
      </p>

      {error && <div className="notice bad">{error}</div>}

      {!queue.redis && (
        <div className="notice bad">
          <b>Redis is unreachable.</b> No run can be queued or observed.
        </div>
      )}
      {noConsumer && (
        <div className="notice warn">
          <b>No worker is running.</b> A created run will sit at{" "}
          <span className="mono">queued</span> for ever, which looks like a hang
          rather than a missing process. Start one with{" "}
          <span className="mono">uv run python scripts/worker.py</span>, or set{" "}
          <span className="mono">QUEUE_SYNC=true</span> to run stages inline.
        </div>
      )}
      {settings.proxy?.caveat && (
        <div className="notice warn">{settings.proxy.caveat}</div>
      )}

      <div className="grid-2">
        <Section title="Queue (§2)" data={queue} />
        <Section title="Pacing (§7)" data={settings.pacing} />
        <Section title="Cache (§7)" data={settings.cache} />
        <Section title="Dedupe (§10.1)" data={settings.dedupe} />
        <Section title="Proxy (§7.1)" data={settings.proxy} />
        <Section title="API keys" data={settings.api_keys} />
      </div>

      <div className="panel">
        <h2 style={{ marginTop: 0 }}>WhatsApp confirmation rate, by slice (§5.2)</h2>
        <p className="small muted">
          This spread is why §13 Screen 1 will not estimate available leads for a
          slice it has never run. Identical code, same category, and the rates
          differ by more than 3×.
        </p>
        {Object.entries(settings.confirmation_rates_by_slice ?? {}).length === 0 ? (
          <p className="muted small">No enriched runs yet.</p>
        ) : (
          <table>
            <thead>
              <tr>
                <th>Slice</th>
                <th className="num">Confirmed / domain crawled</th>
              </tr>
            </thead>
            <tbody>
              {Object.entries(settings.confirmation_rates_by_slice as Record<string, number>).map(
                ([slice, rate]) => (
                  <tr key={slice}>
                    <td>{slice}</td>
                    <td className="num mono">{(rate * 100).toFixed(0)}%</td>
                  </tr>
                ),
              )}
            </tbody>
          </table>
        )}
      </div>

      <div className="panel">
        <h2 style={{ marginTop: 0 }}>Suppression list (§15)</h2>
        <p className="small muted">
          {settings.compliance?.suppression_entries ?? 0} entries, currently hiding{" "}
          {settings.compliance?.contacts_currently_hidden ?? 0} contact(s). Checked on
          every read — the table and the export both — so a suppressed number can
          never be dialled off a screen. Entries survive re-runs; that is the point.
        </p>

        <form onSubmit={addSuppression} className="row" style={{ marginBottom: 16 }}>
          <div className="field">
            <label>Number (E.164)</label>
            <input
              type="text"
              placeholder="+923001234567"
              value={form.value_e164}
              onChange={(e) => setForm({ ...form, value_e164: e.target.value })}
            />
          </div>
          <div className="field">
            <label>or Domain</label>
            <input
              type="text"
              placeholder="example.pk"
              value={form.domain}
              onChange={(e) => setForm({ ...form, domain: e.target.value })}
            />
          </div>
          <div className="field">
            <label>Reason</label>
            <input
              type="text"
              value={form.reason}
              onChange={(e) => setForm({ ...form, reason: e.target.value })}
            />
          </div>
          <button type="submit">Add</button>
        </form>

        <div className="table-wrap" style={{ maxHeight: 300 }}>
          <table>
            <thead>
              <tr>
                <th>Number</th>
                <th>Domain</th>
                <th>Email</th>
                <th>Reason</th>
                <th>Added</th>
              </tr>
            </thead>
            <tbody>
              {suppressions.map((s) => (
                <tr key={s.id}>
                  <td className="mono">{s.value_e164 ?? "—"}</td>
                  <td>{s.domain ?? "—"}</td>
                  <td>{s.email ?? "—"}</td>
                  <td className="small muted">{s.reason ?? "—"}</td>
                  <td className="small muted">
                    {s.created_at ? new Date(s.created_at).toLocaleDateString() : "—"}
                  </td>
                </tr>
              ))}
              {suppressions.length === 0 && (
                <tr>
                  <td colSpan={5} className="muted">
                    Empty.
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      </div>

      <div className="panel">
        <h2 style={{ marginTop: 0 }}>Deliberately excluded sources (§4.1)</h2>
        <p className="small muted">
          Recorded in code so none of these can be quietly re-added by someone who
          has not read why it was rejected.
        </p>
        <table>
          <tbody>
            {Object.entries(settings.excluded_sources ?? {}).map(([name, reason]) => (
              <tr key={name}>
                <td className="mono" style={{ verticalAlign: "top" }}>
                  {name}
                </td>
                <td className="small muted" style={{ whiteSpace: "normal" }}>
                  {String(reason)}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </>
  );
}

function Section({ title, data }: { title: string; data: Record<string, unknown> }) {
  return (
    <div className="panel">
      <h2 style={{ marginTop: 0 }}>{title}</h2>
      {Object.entries(data ?? {}).map(([key, value]) => {
        if (value === null || key === "caveat") return null;
        return (
          <div
            key={key}
            style={{ display: "flex", justifyContent: "space-between", padding: "3px 0" }}
          >
            <span className="muted small mono">{key}</span>
            <span className="mono small">
              {typeof value === "object" ? JSON.stringify(value) : String(value)}
            </span>
          </div>
        );
      })}
    </div>
  );
}
