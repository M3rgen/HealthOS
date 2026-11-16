// Schema 1 content is private at runtime; previews use only the fictional sample.
export interface Meal {
  label: string; name: string; content: string; kcal?: number | null; protein_g?: number | null;
  ingredients?: string[]; preparation?: string | null;
}
export interface DayType { id: string; name: string; weekdays?: number[]; meals?: Meal[] }
export interface ShoppingItem {
  id: string; name: string; quantity: string; estimated_price?: number | null; currency?: string | null; store?: string | null;
}
export interface Session { id: string; name: string; exercises: { movement: string; sets_reps: string; note?: string | null }[] }
export interface ProgressionRow { week_start: number; week_end: number; content: string; duration: string }
export interface Training {
  weekly_schedule?: { weekday: number; content: string; session_ids?: string[] }[];
  sessions?: Session[]; progressions?: { name: string; rows: ProgressionRow[] }[]; rules?: string[];
}
export interface Plan {
  schema: number;
  meta?: { title?: string | null; subtitle?: string | null; warning?: string | null; date?: string | null; language?: string | null };
  targets?: { cards?: { label: string; value: string; description: string }[]; adjustment_rules?: string[] };
  day_types?: DayType[]; notes?: { day_type_id: string; items: string[] }[];
  batch_cooking?: {
    recipes?: { name: string; summary: string; ingredients: string[]; preparation: string; portions: string }[];
    storage_rules?: string[]; substitution_rules?: string[];
  };
  shopping?: { groups?: { name: string; items: ShoppingItem[] }[]; one_off?: ShoppingItem[] };
  training?: Training; measurements?: string[]; assumptions?: { title: string; text: string }[];
}
export interface PlanState { start_date: string | null; checked_item_ids: string[] }
export interface PlanView { plan: Plan | null; version?: number; state?: PlanState }
export interface PlanActions {
  today: string; date: string; state: PlanState; selectDate: (date: string) => void;
  changeState: (patch: Partial<PlanState>) => void; measure: () => void;
}
