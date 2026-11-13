// The JSON the app's own endpoints answer with, as the interface reads it. Fields the interface
// does not use are left out. Keep in step with frontend/preview/fixtures.cjs and app/api/.
import type { CoverageDay, CoverageSource, CoverageTotals, CoverageType } from './coverage.js';
import type { DisplayChoice } from './display-preferences.js';

export type { CoverageDay, CoverageSource, CoverageTotals, CoverageType };

export interface SyncStatusRow {
  data_type: string;
  status: string;
  last_successful_sync?: string | null;
  last_error?: string | null;
  records_downloaded?: number;
  last_run?: { sequence?: number; inserted?: number; updated?: number; failed?: boolean } | null;
}

export interface CoveragePage {
  supported_data_types: string[];
  types: CoverageType[];
  summary: Partial<CoverageTotals>;
  next_offset: number | null;
}

export interface StoredPreferences {
  schema_version?: number;
  updated_at?: string | null;
  preferences?: DisplayChoice;
}

export interface StoredAppearance {
  selection?: Record<string, string>;
  updated_at?: string | null;
}

export interface SyncStarted {
  data_types: string[];
  sequence: number;
}
