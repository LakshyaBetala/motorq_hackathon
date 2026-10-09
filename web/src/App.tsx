import { useEffect, useMemo, useState } from "react";
import {
  ApiError,
  DEMO_TARIFF,
  demoFleet,
  getExperiment,
  getHealth,
  postDepotPlan,
  type ExperimentOut,
  type Health,
  type PlanOut,
} from "./api";

const TENANT_ID = 1;

export default function App() {
  const [health, setHealth] = useState<Health | null>(null);
  const [fleetSize, setFleetSize] = useState(40);
  const [capKw, setCapKw] = useState(260);
  const [plan, setPlan] = useState<PlanOut | null>(null);
  const [planning, setPlanning] = useState(false);
  const [planError, setPlanError] = useState<string | null>(null);
  const [experiment, setExperiment] = useState<ExperimentOut | null>(null);
  const [experimentNote, setExperimentNote] = useState<string | null>(null);

  useEffect(() => {
    getHealth().then(setHealth).catch(() => setHealth(null));
  }, []);

  useEffect(() => {
    getExperiment(TENANT_ID)
      .then((e) => {
        setExperiment(e);
        setExperimentNote(null);
      })
      .catch((err: ApiError) => {
        setExperiment(null);
        // A 409 here is correct behaviour, not a failure: the endpoint refuses to invent an
        // effect when there is no data. Say so plainly rather than showing a blank panel.
        setExperimentNote(
          err.status === 409
            ? `${err.message} — the endpoint refuses to report an effect it cannot measure.`
            : err.message,
        );
      });
  }, []);

  async function runPlan() {
    setPlanning(true);
    setPlanError(null);
    try {
      setPlan(await postDepotPlan(TENANT_ID, DEMO_TARIFF, capKw, demoFleet(fleetSize)));
    } catch (err) {
      setPlanError(err instanceof Error ? err.message : String(err));
      setPlan(null);
    } finally {
      setPlanning(false);
    }
  }

  useEffect(() => {
    void runPlan();
    // Intentionally run once on mount; the button drives re-planning after that.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const savingPct = plan && plan.unmanaged_cost > 0
    ? (plan.saving / plan.unmanaged_cost) * 100
    : 0;

  const controlCount = plan?.vehicles.filter((v) => v.arm === "control").length ?? 0;

  return (
    <div className="app">
      <header className="masthead">
        <h1>ReadyLine</h1>
        <span className="sub">Nightly depot charging commitment · mixed ICE/EV fleets</span>
        <span className="spacer" />
        {health ? (
          <span className={`pill ${health.status === "ok" ? "ok" : "bad"}`}>
            <i className="dot" />
            {health.status === "ok" ? "all systems" : "degraded"}
            <span className="muted" style={{ fontWeight: 400 }}>
              pg {health.postgres ? "✓" : "✗"} · ch {health.clickhouse ? "✓" : "✗"}
            </span>
          </span>
        ) : (
          <span className="pill bad"><i className="dot" />api unreachable</span>
        )}
      </header>

      <section className="card" style={{ marginBottom: 16 }}>
        <h2>Tonight&rsquo;s commitment</h2>
        <div style={{ display: "flex", gap: 16, alignItems: "flex-end", flexWrap: "wrap" }}>
          <label>
            <div className="muted" style={{ fontSize: 12, marginBottom: 4 }}>Vehicles</div>
            <select value={fleetSize} onChange={(e) => setFleetSize(Number(e.target.value))}>
              {[10, 20, 40, 80, 150].map((n) => <option key={n} value={n}>{n}</option>)}
            </select>
          </label>
          <label>
            <div className="muted" style={{ fontSize: 12, marginBottom: 4 }}>Site cap (kW)</div>
            <input
              type="number" value={capKw} min={20} step={20} style={{ width: 110 }}
              onChange={(e) => setCapKw(Number(e.target.value))}
            />
          </label>
          <button className="run" onClick={runPlan} disabled={planning}>
            {planning ? "Solving…" : "Commit plan"}
          </button>
          <span className="muted" style={{ fontSize: 12 }}>
            Lower the cap until it binds — the optimiser spreads load instead of overloading the site.
          </span>
        </div>
        {planError && <div className="err" style={{ marginTop: 12 }}>{planError}</div>}
      </section>

      {plan && (
        <>
          <div className="grid kpis" style={{ marginBottom: 16 }}>
            <div className="card kpi">
              <div className="value good">{savingPct.toFixed(1)}%</div>
              <div className="label">cheaper than charging on arrival</div>
            </div>
            <div className="card kpi">
              <div className={`value ${plan.cap_respected ? "good" : "warn"}`}>
                {plan.peak_kw.toFixed(0)}<span className="muted" style={{ fontSize: 15 }}> / {plan.cap_kw} kW</span>
              </div>
              <div className="label">site peak vs cap</div>
            </div>
            <div className="card kpi">
              <div className="value">
                {plan.duality_gap === null ? "n/a" : `${(plan.duality_gap * 100).toFixed(2)}%`}
              </div>
              <div className="label">
                {plan.duality_gap === null ? "undefined — load was shed" : "proven gap to optimal"}
              </div>
            </div>
            <div className="card kpi">
              <div className="value warn">{controlCount}</div>
              <div className="label">in unmanaged control arm</div>
            </div>
          </div>

          <div className="card" style={{ marginBottom: 16 }}>
            <h2>Site load against the cap</h2>
            <LoadChart
              load={plan.site_load_by_slot}
              cap={plan.cap_kw}
              tariff={DEMO_TARIFF}
            />
            <div className="legend">
              <span><i style={{ background: "var(--good)" }} />cheap tariff slot</span>
              <span><i style={{ background: "var(--accent)" }} />mid</span>
              <span><i style={{ background: "var(--warn)" }} />expensive</span>
              <span><i style={{ background: "var(--bad)" }} />site cap</span>
            </div>
            <p className="note">
              Load sits in the cheap window but never crosses the cap. Running one optimiser per
              vehicle would put every vehicle in the single cheapest hour and trip the main
              breaker; the shared-cap relaxation prices that hour until vehicles with slack move
              themselves. Solved in {plan.iterations} iterations.
              {plan.infeasible_vins.length > 0 && (
                <> <strong style={{ color: "var(--warn)" }}>
                  {plan.infeasible_vins.length} vehicle(s) could not reach target and are
                  reported rather than hidden.
                </strong></>
              )}
            </p>
          </div>

          <div className="grid two">
            <div className="card">
              <h2>Measured saving (randomised holdout)</h2>
              {experiment ? (
                <ExperimentPanel e={experiment} />
              ) : (
                <>
                  <div className="verdict null-result">
                    {experimentNote ?? "Loading…"}
                  </div>
                  <p className="note">
                    10% of vehicles are randomised into an unmanaged control arm at onboarding,
                    stratified by depot. The reported saving is a difference-in-differences
                    between arms with a Welch t-test, not a model output. Both arms run against
                    the same simulator, so any bias in the generator cancels in the difference —
                    which is the only reason a number measured on synthetic data means anything.
                  </p>
                </>
              )}
            </div>

            <div className="card">
              <h2>Per-vehicle plan</h2>
              <div style={{ maxHeight: 340, overflowY: "auto" }}>
                <table>
                  <thead>
                    <tr>
                      <th className="mono">VIN</th>
                      <th>Arm</th>
                      <th style={{ textAlign: "right" }}>End SoC</th>
                      <th style={{ textAlign: "right" }}>Cost</th>
                    </tr>
                  </thead>
                  <tbody>
                    {plan.vehicles.slice(0, 60).map((v) => (
                      <tr key={v.vin}>
                        <td className="mono">{v.vin.slice(-8)}</td>
                        <td><span className={`arm ${v.arm}`}>{v.arm}</span></td>
                        <td style={{
                          textAlign: "right",
                          color: v.feasible ? "var(--ink)" : "var(--bad)",
                        }}>
                          {v.final_soc_pct.toFixed(0)}%
                        </td>
                        <td style={{ textAlign: "right" }}>{v.cost.toFixed(2)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              {plan.vehicles.length > 60 && (
                <p className="note">Showing 60 of {plan.vehicles.length}.</p>
              )}
            </div>
          </div>
        </>
      )}

      <footer className="foot">
        All data is synthetic. Motorq is an industry reference only; this is an independent
        academic exercise with no affiliation. Pack-life and avoided-breakdown savings are
        modelled and are deliberately excluded from the measured figure above.
      </footer>
    </div>
  );
}

function LoadChart({ load, cap, tariff }: { load: number[]; cap: number; tariff: number[] }) {
  const W = 980;
  const H = 190;
  const padL = 38;
  const padB = 22;
  const padT = 10;

  const maxY = Math.max(cap * 1.15, ...load, 1);
  const bw = (W - padL - 10) / Math.max(load.length, 1);
  const y = (v: number) => padT + (H - padT - padB) * (1 - v / maxY);

  const { lo, hi } = useMemo(() => {
    const prices = tariff.filter((p) => p > 0);
    return { lo: Math.min(...prices), hi: Math.max(...prices) };
  }, [tariff]);

  const band = (price: number) => {
    if (hi === lo) return "bar";
    const t = (price - lo) / (hi - lo);
    if (t < 0.34) return "bar cheap";
    if (t > 0.66) return "bar pricey";
    return "bar";
  };

  return (
    <svg className="chart" viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="none" role="img"
         aria-label="Depot load per slot against the site power cap">
      <line className="axis" x1={padL} y1={H - padB} x2={W - 6} y2={H - padB} />
      {[0, 0.5, 1].map((f) => (
        <g key={f}>
          <line className="axis" x1={padL} y1={y(maxY * f)} x2={W - 6} y2={y(maxY * f)}
                opacity={f === 0 ? 1 : 0.35} />
          <text x={4} y={y(maxY * f) + 3}>{Math.round(maxY * f)}</text>
        </g>
      ))}
      {load.map((kw, i) => {
        const h = Math.max(0, H - padB - y(kw));
        return (
          <rect
            key={i}
            className={band(tariff[i] ?? 0)}
            x={padL + i * bw + 1.5}
            y={y(kw)}
            width={Math.max(1, bw - 3)}
            height={h}
            rx={2}
          />
        );
      })}
      <line className="cap" x1={padL} y1={y(cap)} x2={W - 6} y2={y(cap)} />
      <text x={W - 60} y={y(cap) - 5} style={{ fill: "var(--bad)" }}>cap {cap} kW</text>
      {load.map((_, i) =>
        i % 2 === 0 ? (
          <text key={i} x={padL + i * bw + bw / 2 - 5} y={H - 7}>{i}</text>
        ) : null,
      )}
    </svg>
  );
}

function ExperimentPanel({ e }: { e: ExperimentOut }) {
  // Scale the interval around zero so the reader can see whether it crosses.
  const span = Math.max(Math.abs(e.ci_low), Math.abs(e.ci_high)) * 1.25 || 1;
  const pct = (v: number) => ((v + span) / (2 * span)) * 100;

  return (
    <>
      <div className={`verdict ${e.significant_at_05 ? "significant" : "null-result"}`}>
        {e.interpretation}
      </div>

      <div className="ci-wrap">
        <div className="ci-track">
          <div className="ci-zero" style={{ left: `${pct(0)}%` }} />
          <div
            className="ci-line"
            style={{ left: `${pct(e.ci_low)}%`, width: `${pct(e.ci_high) - pct(e.ci_low)}%` }}
          />
          <div className="ci-point" style={{ left: `${pct(e.did_estimate)}%` }} />
        </div>
        <div className="ci-labels">
          <span>{e.ci_low.toFixed(2)}</span>
          <span>95% CI · red line = no effect</span>
          <span>{e.ci_high.toFixed(2)}</span>
        </div>
      </div>

      <table style={{ marginTop: 10 }}>
        <tbody>
          <tr><td className="muted">treatment / control</td>
              <td style={{ textAlign: "right" }}>{e.n_treatment} / {e.n_control}</td></tr>
          <tr><td className="muted">difference-in-differences</td>
              <td style={{ textAlign: "right" }}>{e.did_estimate.toFixed(3)}</td></tr>
          <tr><td className="muted">t ({e.degrees_of_freedom.toFixed(0)} dof)</td>
              <td style={{ textAlign: "right" }}>{e.t_statistic.toFixed(3)}</td></tr>
          <tr><td className="muted">p-value</td>
              <td style={{ textAlign: "right" }}>{e.p_value.toFixed(4)}</td></tr>
        </tbody>
      </table>
    </>
  );
}
