import { useEffect, useState } from "react";
import { api, type SkinWarning } from "../api";
import { useI18n } from "../i18n";

// Skins LTK Manager is expected to refuse (c0000229 / SkinhackDetected).
// The engine answers from the archive alone, so this lists them before a
// game is queued instead of after the overlay silently fails to apply.
export function BrokenSkins() {
  const { t } = useI18n();
  const [warnings, setWarnings] = useState<SkinWarning[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [champion, setChampion] = useState("");

  const refresh = async (champId: string) => {
    try {
      const data = await api.skinPreflight(champId);
      setWarnings(data.warnings);
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  };

  useEffect(() => {
    refresh("");
  }, []);

  return (
    <section>
      <h2>{t("Broken skins")}</h2>
      <p className="subtitle">{t("Skins LTK Manager will refuse, detected before you queue.")}</p>
      <div className="card">
        <div className="logbar">
          <input
            value={champion}
            placeholder={t("Champion ID (optional)")}
            onChange={(e) => setChampion(e.target.value)}
          />
          <button onClick={() => refresh(champion)}>{t("Scan")}</button>
        </div>
        {error && <p className="bad">{error}</p>}
        {warnings.length ? (
          <ul className="profiles">
            {warnings.map((w) => (
              <li key={w.archive}>
                <span className="name">
                  {w.champion ?? "?"} / {w.skin_numbers.join(", ")}
                  <span className="muted"> {" =>? "}{w.code}</span>
                </span>
                <span className="actions">
                  <span className="bad">{w.status}</span>
                </span>
              </li>
            ))}
          </ul>
        ) : (
          <p className="muted">{t("No broken skins found.")}</p>
        )}
      </div>
    </section>
  );
}
