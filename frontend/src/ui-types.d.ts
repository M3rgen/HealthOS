// Type-only contracts for stored uploads, unvalidated manifest fields and chat receipts.
export interface UploadRecord {
  kind: string; name: string; type: string; width: number; height: number;
  mode: string; files: Record<string, Blob | undefined>; createdAt: number;
}
export interface RawPreset {
  id?: unknown; label?: unknown; labelEn?: unknown; builtin?: unknown;
  files?: Record<string, unknown>;
}
export interface TransferRange { first?: string | null; last?: string | null }
export interface TransferCall {
  action: string; outcome: string; counts: Record<string, number>;
  requested: TransferRange | null; timezone: string; data_dates: TransferRange | null;
  profile_dates?: TransferRange | null; metadata_dates?: TransferRange | null;
  source_timezones: string[]; fields: string[];
}
export interface TransferReceipt {
  turn: string; status: string; counts: Record<string, number>;
  calls: TransferCall[]; failed_sends: number;
}
