# Privacy and medical scope

HealthOS is self-hosted software for keeping **your own** health records on **your own**
hardware. This page says what it is not, where your data lives and exactly what leaves the
machine. A Turkish summary is in [PRIVACY.tr.md](PRIVACY.tr.md).

## Not a medical device

HealthOS is not a medical device. It is not intended for diagnosis, treatment, monitoring of a
disease or any other medical decision. It shows what your wearables and apps recorded:

- **HRV and SpO₂ are device estimates**, computed by the wearable's own algorithms, not clinical
  measurements. Different devices define them differently (for example RMSSD and SDNN for HRV);
  HealthOS keeps them apart and does not convert one into the other.
- Sleep stages, resting heart rate, stress and body battery are also device estimates.
- Missing measurements stay missing; they never become zero.

The optional companion, Umay, gives informational readings of your archive. It does not
diagnose and does not suggest medication or doses. Talk to a health professional about anything
that concerns your health.

## Where your data lives

- **On your server only.** The archive is a PostgreSQL database you operate. There is no
  hosted service, no account with the project, no application telemetry, and the authors of
  HealthOS receive nothing.
- **Private interface.** The dashboard and the API listen on `127.0.0.1` behind one access
  token. A public deployment exposes only the Google webhook route, with size and rate limits.
- **Secrets stay local.** Google refresh tokens are encrypted in the database; the Garmin key is
  read from a file and never stored in `.env`, the database or a log.
- **Backups** are encrypted (`age` or `gpg`) on your host and kept forever unless you opt into
  retention.
- **Nothing personal in the repository.** Real health records, secrets, logs, backups and local
  verification outputs are excluded from this repository; screenshots and fixtures are synthetic.

## What leaves your machine

| Destination | When | What is sent |
| --- | --- | --- |
| Google Health API | Synchronization, if you connect Google | OAuth tokens and read requests (data types, time windows, page tokens). Nothing is written back. |
| Google (webhook) | If you configure webhooks | Nothing: Google sends change notifications to your endpoint, and HealthOS answers with a status code and then reads the changed data as above. |
| Garmin Connect Personal API | If `HEALTHOS_SOURCES` includes `garmin` | Your Personal API key (to exchange it for a short-lived token) and read requests by data kind and date. Nothing is written back. |
| OpenAI, through the Umay bridge | Only while you chat with Umay | See below. |

Apple Health import is not in this source tree; in the published GitHub snapshot its data only
ever arrives, as a file you upload or a request you send. Nothing is fetched from Apple and nothing
is sent to it.

## Umay and OpenAI

Umay is off unless you run the Codex bridge on your host and connect it. It uses your own
ChatGPT account through the local Codex CLI, so what is sent is processed by OpenAI under that
account's terms. Nothing is sent in the background: data leaves only during a turn you start.

For each conversation and turn, the bridge sends to OpenAI:

- **Instructions:** Umay's persona (`app/personas/umay/soul.md`) and the tool and health rules
  (`app/services/umay/codex_chat.py`). They contain no personal data.
- **Your message**, prefixed with your time zone and the local date and time.
- **A short context block** (at most 8 KiB) quoting only what you wrote earlier in the same
  connection (topics, explicit preferences, recent requests). It is kept in memory and dropped
  when the connection closes.
- **Results of the read-only `health_archive` tool**, which the model calls as it needs:
  - `inventory`: which data types exist, with counts and first and last dates;
  - `daily`: per civil day steps, sleep, resting heart rate, HRV and SpO₂ (from whichever
    source the precedence policy selects, Garmin included), the latest body measurements,
    hourly charts and the source-separated daily streams, at most 366 days per call;
  - `records`: original Google records with their full payload, including the data source
    (device name and platform), at most 31 days and 200 records per call;
  - `coverage`: which days were retrieved and why a day has no value;
  - `devices`: your devices with their names (also earlier names), platforms, data types and
    when each first and last delivered data;
  - interpretation actions (`baseline`, `analyze`, `sleep`, `intraday`, `sessions`,
    `physiology`, `longitudinal`, `relationship`): summaries and evidence computed from the same
    records, for a requested range of at most 90 days (`relationship`: 180); `baseline` and
    `analyze` also read up to 90 earlier days as the personal baseline.

  Each call returns a bounded slice; Umay may make several calls per question, and earlier
  results stay in that conversation's context until you start a new chat.

**Never sent:** the database itself, whole tables, OAuth tokens, the Garmin key, the access
token, the bridge token, `.env`, backups or logs.

Every turn shows a **transfer receipt** with the record counts, date ranges and field names
that were handed to the bridge ([details](UMAY_TRANSFER.md)). The receipt describes what
HealthOS handed over, not a confirmation from OpenAI.

**Links in Umay's answers.** A model's answer can be steered by text that came from a data
source, and a link is a way out: one click on `https://elsewhere/?hrv=41` would hand that value to
another site. So the chat keeps a link only when it points to this HealthOS site itself (it opens
in a new tab, without a handle on this one); a link to any other site loses its target and stays
as plain text (`frontend/src/chat-format.js`).

## GDPR and KVKK

HealthOS gives you a copy of your own data under your own control; it does not make anyone a
processor of it. The person who runs an installation is responsible for the data in it. Health
data is a special category of personal data under the GDPR (Art. 9) and "özel nitelikli kişisel
veri" under the Turkish KVKK (Art. 6). Keeping your own records for personal use is the
intended case. If you store anyone else's data, or turn on Umay, you decide on a transfer of
health data to a third party (OpenAI, which may process it outside your country); make that
decision knowingly. This note is not legal advice.
