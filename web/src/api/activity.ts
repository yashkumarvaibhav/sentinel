export interface ScenarioActivity {
  in_flight: boolean;
  run_id: string | null;
  scenario_id: string | null;
  mode: string | null;
  state: string | null;
  started_at: string | null;
  evidence_start_at: string | null;
  evidence_end_at: string | null;
  evidence_cursor_at: string | null;
  progress: number | null;
  note: string;
}

export async function fetchScenarioActivity(signal?: AbortSignal): Promise<ScenarioActivity> {
  const response = await fetch('/api/activity', signal ? { signal } : {});
  if (!response.ok) throw new Error(`scenario activity request failed (${response.status})`);
  return (await response.json()) as ScenarioActivity;
}
