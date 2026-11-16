# Plan document, schema 1

The backend reads content; it calculates no nutrition, prices or training loads. The private
plan lives only in the database and an operator's private JSON file. Never commit a real plan.
[plan.sample.json](../examples/plan.sample.json) is entirely fictional, including Deniz and all
round numbers. The Plan tab reads this document; it adds no editing UI or Umay access.

## Shape and limits

`schema` is the required integer `1`. Every other top-level section is optional; an empty plan
`{"schema":1}` is valid. JSON object order preserves section order; lists preserve row order.
All nested fields below are required unless marked `?`. Lists default to empty when omitted
except fields without `?` inside a row. Unknown fields and coercion from other types are refused.

| Section | Fields (nested objects in braces) |
| --- | --- |
| `meta` | `title?`, `subtitle?`, `date?`, `language?`, `warning?` |
| `targets` | `cards?` [{`label`, `value`, `description`}], `adjustment_rules?` [text] |
| `day_types` | [{`id`, `name`, `weekdays?` [1–7], `meals?` [{`label`, `name`, `content`, `kcal?`, `protein_g?`, `ingredients?` [short text], `preparation?`}]}] |
| `notes` | [{`day_type_id`, `items` [text]}] |
| `batch_cooking` | `recipes?` [{`name`, `summary`, `ingredients` [short text], `preparation`, `portions`}], `storage_rules?`, `substitution_rules?` [text] |
| `shopping` | `groups?` [{`name`, `items` [{`id`, `name`, `quantity`, `estimated_price?`, `currency?`, `store?`}]}], `one_off?` [same item] |
| `training` | `weekly_schedule?` [{`weekday`, `content`, `session_ids?`}], `sessions?` [{`id`, `name`, `exercises` [{`movement`, `sets_reps`, `note?`}]}], `progressions?` [{`name`, `rows` [{`week_start`, `week_end`, `content`, `duration`}]}], `rules?` [text] |
| `measurements` | [text describing when/what to measure; records use the existing manual flow] |
| `assumptions` | [{`title`, `text`}] |

- Maximum UTF-8 file size: 1 MiB. Every list: at most 32 elements, except `weekdays` and
  `weekly_schedule`: 7. Empty lists are allowed. Deep/malformed JSON and duplicate keys fail.
- Short text: 1–200 Unicode characters; longer text: 1–2000. Longer fields are `warning`,
  `description`, adjustment rules, meal/schedule/progression `content`, `preparation`, note
  items, recipe `summary`/`portions`, storage/substitution/training rules, exercise `note`,
  measurements and assumption `text`. All other free text is short. No HTML or Markdown is
  interpreted; even markup stays literal text. C0 control characters except tab/CR/LF and lone
  surrogates fail. Nullable text marked `?` may be null; whole sections may not be null.
- IDs match `[a-z][a-z0-9_-]{0,47}`. Day type and session IDs are unique in their own lists;
  shopping IDs are unique across all groups and one-off purchases (maximum 1056 items).
  Keep an item's ID stable across versions only while it denotes the same item.
- Weekdays are integers, Monday=1 to Sunday=7, without duplicates within a day type or the
  weekly schedule. Named day types may overlap. Notes reference existing day types; schedule
  session IDs reference existing sessions, without duplicates per row.
- Dates are valid `YYYY-MM-DD` calendar dates. Language matches
  `[a-z]{2,3}(-[A-Za-z0-9]{2,8}){0,3}`. Currency is three uppercase ASCII letters.
- kcal: finite number 0–100000; protein_g: finite number 0–10000; estimated_price: finite
  number 0–1000000000. Price and currency must appear together (or both be absent/null).
  Week boundaries are integers 1–520, inclusive, with start ≤ end. These bounds validate
  document size/types, not medical suitability. Values, quantity, sets/reps and duration are text.

The executable shape is `app.schemas.plan.PlanDocument.model_json_schema()`; semantic reference
and uniqueness checks live in `app/services/operations/plan_validation.py`.

## Import and API

```sh
python -m app.cli.plan_import docs/examples/plan.sample.json --check
python -m app.cli.plan_import /private/location/plan.json
```

`--check` opens no database. Import prints only a version ID, `added`, `active` and counts; a validation failure
prints only a schema path/rule on stderr, never the value or file name, and writes nothing.
Unknown field names are masked as `?`. JSON whitespace and nested object key order do not
change the SHA-256 digest; section/list order does. Reimporting any existing content returns
its original version without inserting or reactivating it; the summary's `active=0` says so. The
highest imported ID is active: to return to an older plan, change it (for example its date).
Imports/state writes share a transaction lock, so checked IDs are validated against the plan
active at that write. No archive version is updated or deleted, including after source edits.

`GET /api/plan` returns `{"plan":null}` when empty; otherwise `plan` (original document),
`version`, `schema_version`, `imported_at` and `state`. Default state is
`{"start_date":null,"checked_item_ids":[]}`. `PUT /api/plan/state` fully replaces those two
required state fields; date is nullable, checked IDs are unique and at most 1056. The bounded
request body is 128 KiB. Unknown/inactive IDs (`unknown_identifier`), or writing before a plan
exists (`no_plan`), return 422 with only `path` and `rule`, without changing state. Old inactive
checked IDs remain stored but are hidden on reads.
Both endpoints follow existing session authentication/read rules and return `Cache-Control:
no-store`; PUT requires `X-HealthOS-Request: 1` and existing same-origin/CSRF checks.
No plan text is logged. Import logs contain only version, insertion result and section counts.

## Plan tab

The Plan tab loads on first opening. Available sections are Today, Nutrition, Batch cooking,
Shopping, Training, Measurements and Assumptions; empty sections have no button. Today follows
the interface time zone. Overlapping day types are shown in document order; an unmapped day
shows no invented meals. Show another day selects a civil date. Program weeks run Monday to
Sunday: week one is the week that contains the saved start date, so a Wednesday start counts from
that Monday. The saved date stays as chosen; daylight-saving changes do not move a week. Before
that week the program has not started; without a date the week stays unset.

Meal totals sum only supplied kcal/protein values. Shopping totals include only priced items,
keep currencies separate and include checked items. Checks and the program date use the state
API, including after reload and on another device; a failed write rolls back its changes and
shows a notification. Changes made during that request are retained and sent next. Concurrent
devices use the API's last-write-wins semantics. Resetting the list requires confirmation.
Measurements open the existing manual-entry dialog. All document text stays literal plain text.

## Backup and rollback

Migration `20261115_0037` adds `plan_versions` and `plan_state`. Standard encrypted full-database
backups include both automatically; `scripts/restore.sh` counts the union of all restored/live
tables, including these. Keep the private import file under the same access controls as a
backup. The [runbook](../RUNBOOK.md#backup-verify-and-restore-drill) backup and restore steps apply unchanged.
The archive's ALWAYS triggers refuse UPDATE, DELETE and TRUNCATE, including replica sessions;
`healthos verify` checks their definitions after restore. State is replaceable UI preference.
A downgrade refuses if either table contains rows; neither is a disposable table. Do not delete
rows to downgrade: retain the current schema or restore a matching backup in isolation.
