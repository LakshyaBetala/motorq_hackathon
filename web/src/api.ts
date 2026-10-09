/**
 * API client.
 *
 * The tenant header is a local-profile convenience that mirrors `ALLOW_HEADER_AUTH` on the
 * server. In any other profile the API refuses it and requires an OIDC bearer token, so this
 * must never become the only way the UI can authenticate.
 */

export interface Health {
  status: "ok" | "degraded";
  uptime_s: number;
  postgres: boolean;
  clickhouse: boolean;
}

export interface PlanVehicleOut {
  vin: string;
  arm: "control" | "treatment";
  power_by_slot: number[];
  final_soc_pct: number;
  feasible: boolean;
  cost: number;
}

export interface PlanOut {
  total_cost: number;
  unmanaged_cost: number;
  saving: number;
  peak_kw: number;
  cap_kw: number;
  cap_respected: boolean;
  lower_bound: number;
  /** null when load had to be shed: the bound assumes every vehicle reaches target. */
  duality_gap: number | null;
  iterations: number;
  site_load_by_slot: number[];
  infeasible_vins: string[];
  vehicles: PlanVehicleOut[];
}

export interface ExperimentOut {
  metric: string;
  n_treatment: number;
  n_control: number;
  did_estimate: number;
  saving_per_100km: number;
  standard_error: number;
  t_statistic: number;
  degrees_of_freedom: number;
  p_value: number;
  ci_low: number;
  ci_high: number;
  significant_at_05: boolean;
  interpretation: string;
}

export interface PlanVehicleIn {
  vin: string;
  soc_start_pct: number;
  soc_target_pct: number;
  battery_kwh: number;
  max_charge_kw: number;
  last_slot: number;
  depot_id: number;
}

export class ApiError extends Error {
  constructor(readonly status: number, message: string) {
    super(message);
  }
}

function headers(tenantId: number): HeadersInit {
  return {
    "Content-Type": "application/json",
    "X-Tenant-Id": String(tenantId),
    "X-Roles": "DISPATCHER",
  };
}

async function unwrap<T>(res: Response): Promise<T> {
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const body = await res.json();
      detail = body.detail ?? detail;
    } catch {
      /* non-JSON error body; keep the status text */
    }
    throw new ApiError(res.status, detail);
  }
  return res.json() as Promise<T>;
}

export async function getHealth(): Promise<Health> {
  return unwrap<Health>(await fetch("/healthz"));
}

export async function postDepotPlan(
  tenantId: number,
  tariff: number[],
  capKw: number,
  vehicles: PlanVehicleIn[],
  slotMinutes = 60,
): Promise<PlanOut> {
  const res = await fetch("/api/v1/depot/plan", {
    method: "POST",
    headers: headers(tenantId),
    body: JSON.stringify({
      tariff_per_slot: tariff,
      site_cap_kw: capKw,
      slot_minutes: slotMinutes,
      vehicles,
    }),
  });
  return unwrap<PlanOut>(res);
}

export async function getExperiment(tenantId: number): Promise<ExperimentOut> {
  return unwrap<ExperimentOut>(
    await fetch("/api/v1/experiments/charging-arbitrage", { headers: headers(tenantId) }),
  );
}

/** An overnight tariff with a cheap window, used as the demo default. */
export const DEMO_TARIFF = [
  9.2, 9.2, 8.4, 6.1, 3.2, 2.8, 2.8, 2.8, 3.1, 4.6, 6.8, 9.4,
];

/** A depot of mixed EVs with varied arrival SoC and departure deadlines. */
export function demoFleet(n: number): PlanVehicleIn[] {
  const models = [
    { kwh: 40.5, kw: 22 },
    { kwh: 61.0, kw: 30 },
    { kwh: 21.3, kw: 15 },
    { kwh: 39.4, kw: 22 },
  ];
  return Array.from({ length: n }, (_, i) => {
    const m = models[i % models.length];
    return {
      vin: `DEMOEV${String(i).padStart(4, "0")}`,
      // Deterministic spread so the demo is reproducible between runs.
      soc_start_pct: 12 + ((i * 37) % 48),
      soc_target_pct: 85,
      battery_kwh: m.kwh,
      max_charge_kw: m.kw,
      last_slot: i % 7 === 0 ? 7 : DEMO_TARIFF.length - 1,
      depot_id: 1,
    };
  });
}
