# Umay transfer receipts

Each accepted Umay turn has a live, expandable transfer receipt beside its question
and answers. It describes the exact serialized tool results successfully handed to
the local bridge, **not confirmation of receipt by OpenAI**. Earlier turns' health
data may remain in the same model conversation's context. New chats/reconnections
start separate model conversations; older visible receipts are never carried forward.

Receipt counts are representations, with no combined "unique source records" total:
- `records`: only the transmitted page's raw rows; repeated pages count again.
- `daily`: days with at least one non-null legacy metric, profile entries, non-null
  hourly chart points, derived stream/profile summaries, and their observation entries
  are counted separately. Zero is a value; empty day placeholders are not summaries.
  Null-valued observations still transmit source evidence; sleep timelines belong to
  their derived summary. Repeated HRV definitions/profile observations are not unique rows.
- `inventory`/`coverage`: stream/metric metadata rows only. Archive/sample counts inside
  those results are never counted as transmitted raw records. Refusals send error metadata.

Requested bounds, actual data dates, profile dates (including before the requested
window), and dates described by metadata are separate. Timestamps are expressed as
days in the query timezone; civil dates stay civil dates. Empty results have no actual
data range. Field paths include null/empty fields; `[]` merges array indices, `*` masks
unknown map keys, and `**` covers arbitrary JSON deeper than twenty levels.
Only count/date/field metadata, delivery/turn status and a random temporary turn id
are logged locally. Payload/measurement values, source/device identifiers, conversation
text, credentials and exception details are excluded. Receipts stay in the relay/browser
memory; no chat storage or database archive is added. Failed bridge sends do not increase
counts; completion, cancellation, failure, timeout and disconnect finalize the receipt.


Version 2 interpretation actions go through the same receipt. Every interpretation summary and
suggestion candidate sent counts as `türetilmiş özet`, every open evidence row or calendar pair as
`türetilmiş gözlem`. The `absent` placeholder is metadata; a summary carrying a value or a health
computation is not metadata. The date range covers the historical baseline evidence actually sent
and the candidate's date references. Page repeats count again; beyond field names, no method,
source, cursor or measurement value is logged. This health data goes to OpenAI over the existing
bridge.

On every turn an additional context block also goes to OpenAI: at most 8 KiB, only quotations of
what the user wrote on this connection (topic, explicit presentation preference, recent requests
and unanswered requests; Umay's replies and tool data are not included). The block lives only in
the chat connection's memory and is dropped when the connection closes.
