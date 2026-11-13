// The dashboard's shared state, in one place instead of module-level variables in main.js.
//   data          last /api/dashboard response painted (null until the first success)
//   dataReceivedAt milliseconds since epoch of the last successful dashboard paint
//   health        last /health response (null while the service is unreachable)
//   busy          a refresh is in flight; pendingRefresh asks for one more after it
//   zoneChoice    the chosen time zone setting ('auto' = this device); zone is its resolved name
//   motion        whether the body model turns on its own (Sistem › Görünüm)
//   selectedDate  the calendar day shown (null = today)
//   activePage    the visible page: overview, measurements, system or sports
/**
 * @typedef {{date: string, partial?: boolean, status?: string, steps?: number|null, sleep_seconds?: number|null,
 * resting_bpm?: number|null, hrv_ms?: number|null, spo2?: number|null, sources?: Record<string, {source: string, label?: string, alternatives?: {source: string,
 * label?: string, value: number | null}[]}>}} DayRow
 * @typedef {{value: number, measured_at: string}} Measurement
 * @typedef {{days: DayRow[], charts: {steps: number[], heart_rate: number[]}, profile: Record<string, Measurement | undefined>,
 *   start_date: string, end_date: string, timezone: string}} DashboardData
 * @typedef {{status: string, failed_syncs?: number, failed_notifications?: number, pending_notifications?: number,
 *   latest_health_write?: string | null, last_sync_success?: string | null, last_backup_success?: string | null, reasons?: string[], checked_at?: string | null, database?: string,
 *   scheduler?: string, data_freshness?: Record<string, string | null>, stale_streams?: string[],
 *   active_sources?: string[], configuration?: string, ui_login_disabled?: boolean,
 *   garmin?: {last_success_at?: string | null, last_error_at?: string | null} | null}} Health
 */

export const state = {
  /** @type {DashboardData | null} */
  data: null,
  dataReceivedAt: 0,
  /** @type {Health | null} */
  health: null,
  busy: false,
  pendingRefresh: false,
  zoneChoice: 'auto',
  zone: 'UTC',
  motion: true,
  /** @type {string | null} */
  selectedDate: null,
  activePage: 'overview',
};
