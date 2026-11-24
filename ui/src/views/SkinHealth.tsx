import { useEffect, useMemo, useState } from "react";
import { api, type SkinPreflightResponse, type SkinWarning, type SkinVerdictKind } from "../api";
import { useI18n } from "../i18n";

// Every installed skin that will disappoint, in one place.
//
// Two different failures share this list because the player does not
// distinguish them: a skin that never appears and a skin that appears mute
// are both "this skin doesn't work".
//
//   blocked  - LTK Manager refuses the overlay (c0000229), so the skin never
//              shows. Nothing can be done about it here; the picker hides it.
//   no_audio - the archive asks for a skill-sound bank it never stored, so
//              the skin plays silently. Also not fixable without the missing
//              bank, which is why this screen reports instead of repairing.
//
// The engine answers from the archive alone, so this reads before a game is
// queued rather than after an overlay silently fails.

type Filter = "all" | "blocked" | "no_audio";

const EMPTY: SkinPreflightResponse = { warnings: [], count: 0, blocked: 0, no_audio: 0 };

function kindOf(w: SkinWarning): SkinVerdictKind {
  return w.kind ?? "unknown";
}

function labelOf(w: SkinWarning): string {
  if (w.skinId) return w.skinId;
  const champ = w.champion ?? "?";
  const nums = Array.isArray(w.skin_numbers) ? w.skin_numbers.join(", ") : "";
  return nums ? `${champ} / ${nums}` : champ;
}

export function SkinHealth() {
  const { t } = useI18n();
  const [data, setData] = useState<SkinPreflightResponse>(EMPTY);
  const [error, setError] = useState<string | null>(null);
  const [champion, setChampion] = useState("");
  const [filter, setFilter] = useState<Filter>("all");
  const [busy, setBusy] = useState(false);

  const refresh = async (champId: string) => {
    setBusy(true);
    try {
      setData(await api.skinPreflight(champId));
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  useEffect(() => {
    refresh("");
  }, []);

  const shown = useMemo(() => {
    if (filter === "all") return data.warnings;
    return data.warnings.filter((w) => kindOf(w) === filter);
  }, [data.warnings, filter]);

  const segments: { id: Filter; label: string; count: number }[] = [
    { id: "all", label: t("All"), count: data.count },
    { id: "blocked", label: t("Blocked"), count: data.blocked },
    { id: "no_audio", label: t("No audio"), count: data.no_audio },
  ];

  return (
    <section>
      <h2>{t("Skin health")}</h2>
      <p className="subtitle">
        {t("Installed skins that will not work, detected from the archive before you queue.")}
      </p>

      <div className="card">
        <div className="logbar">
          <input
            value={champion}
            placeholder={t("Champion ID (optional)")}
            onChange={(e) => setChampion(e.target.value)}
          />
          <button onClick={() => refresh(champion)} disabled={busy}>
            {busy ? t("Scanning...") : t("Scan")}
          </button>
        </div>

        <div className="segments">
          {segments.map((s) => (
            <button
              key={s.id}
              className={filter === s.id ? "segment active" : "segment"}
              onClick={() => setFilter(s.id)}
            >
              {s.label}
              <span className="segment-count">{s.count}</span>
            </button>
          ))}
        </div>

        {error && <p className="bad">{error}</p>}

        {shown.length ? (
          <ul className="profiles">
            {shown.map((w) => {
              const kind = kindOf(w);
              return (
                <li key={w.archive}>
                  <span className="name">
                    {labelOf(w)}
                    <span className={kind === "blocked" ? "bad" : "warn"}>
                      {" "}
                      {kind === "blocked" ? t("Blocked") : kind === "no_audio" ? t("No audio") : t("Unknown")}
                    </span>
                  </span>
                  <span className="actions">
                    <span className="muted">{w.code}</span>
                  </span>
                </li>
              );
            })}
          </ul>
        ) : (
          <p className="muted">
            {data.count === 0 ? t("No problem skins found.") : t("Nothing matches this filter.")}
          </p>
        )}
      </div>
    </section>
  );
}
