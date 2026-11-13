# HealthOS reference — Webhooks

Part of the [technical reference](../REFERENCE.md).

## Webhooks

Configure a Google Health subscriber with:

- `endpointUri`: `https://your-domain.example/webhooks/google-health`
- `endpointAuthorization.secret`: an unpredictable exact value also set as `GOOGLE_HEALTH_WEBHOOK_AUTHORIZATION`
- automatic subscription policies for the data types you consented to
- the **Google Cloud project number**, not its textual project ID

The endpoint rejects an unauthenticated verification challenge, accepts the authenticated
verification body, then requires both the authorization secret and Google's ECDSA signature for
notifications. Google may send either one notification object or an array containing many
notifications. The signed request is preserved as an immutable batch, future receipts retain the
exact raw request bytes, and every array element gets independent durable extraction, authorized
health-user validation, global content-hash deduplication, processing state, and retry history.
Returning HTTP 204 proves durable acceptance only; `processed_at` is set after every valid element
in the batch completes. A failed element remains retryable without re-running successful elements.
The documented deployment has one subscriber, registered using short-lived service-account
impersonation; no service-account key is stored. Its id is the default of
`GOOGLE_HEALTH_WEBHOOK_SUBSCRIBER_ID`, `personal-health-mirror`. The project number is
deployment-secret metadata and is not repeated in tracked documentation.

Run `healthos webhook-readiness` before subscriber registration. It reports only booleans and
never prints the endpoint authorization value. Registration is ready only when the project number
is numeric, the endpoint uses HTTPS, the authorization value includes a `Bearer` or `Basic` scheme,
and the subscriber ID matches Google's naming rules.

### Cloudflare Tunnel receiver (deployed)

The deployment uses a host-level `cloudflared` service on the server. It makes outbound-only
connections, so the router has no inbound port forwarding. The tunnel route must match
`${CLOUDFLARE_WEBHOOK_HOSTNAME}` and path `^/webhooks/google-health$`, forward to
`http://127.0.0.1:8000`, and use a final catch-all 404 rule. Only the tunnel-scoped credential
file lives on the server; no account-wide `cert.pem` or router fallback is used.

The app remains bound to loopback, PostgreSQL has no published port, and the path rule prevents
public access to `/health`, `/docs`, OAuth, sync, backfill, or Google Health metadata routes.
Setup and the public-surface checks are in [CLOUDFLARE_TUNNEL.md](../CLOUDFLARE_TUNNEL.md). Local
status:

**Verification-only command:**

```sh
systemctl status cloudflared
docker compose ps
curl --fail http://127.0.0.1:8000/health/live
docker compose exec -T app healthos status
docker compose exec -T app healthos webhook-readiness
docker compose exec -T app healthos webhook-status
```

After deploying a fix or recovering from an outage, retry stored `pending` and `failed` elements:

**Operates only on already verified stored webhook batches:**

```sh
docker compose exec -T app healthos webhook-replay-pending
```

Despite its name, this command retries `failed` elements too; completed elements are not re-fetched.
It reuses verified batches and read-only Google queries; no SQL reset or Google resend is needed.

A live re-delivery of a signed body that was first verified more than 24 hours earlier is accepted
(HTTP 204) but not processed again (`WEBHOOK_REPLAY_IGNORED`); the operator command above is not
limited.

Deliveries normally finish on their own. A delivery that finds every owner slot taken waits for
one, holding no database connection, and only another owner of the same element counts as
"running". With `google` selected the scheduler job `webhook-unfinished` runs every five minutes and
finishes what a delivery left behind, typically a shutdown that cancelled its task: valid elements still
`pending`, or `failed` with the interrupted-run error, untouched for at least five minutes. A real
processing failure stays `failed` for the replay command, so a poison element is not retried forever.
Recovery records such an element as `interrupted` in `webhook_notifications.status` (no schema
change); rows written before this keep `failed` with the interrupted-run error and are still read as
interrupted, unchanged. `healthos webhook-status` can therefore list an `interrupted` count under
`notification_states`, and the scan reads the archive in pages of 256 rows.

`cloudflared` runs with `--no-autoupdate`; update it deliberately (see the
[runbook](../RUNBOOK.md#update-images-and-cloudflared)). Google retries failed webhook notifications
for up to seven days; daily and weekly reconciliation remain the recovery layer for longer outages.
A diagnosis checklist is in the [runbook](../RUNBOOK.md#the-webhook-went-quiet).

### Direct Caddy receiver (unused alternative)

The backend validates `Host` for HTTP and WebSocket requests. Private routes accept only
`localhost`, `127.0.0.1`, or `[::1]`, with optional ports. The HTTPS hostname in
`GOOGLE_HEALTH_WEBHOOK_ENDPOINT_URI` additionally accepts only POST requests to
`/webhooks/google-health`; webhook authorization and signature checks still apply.
Proxies must preserve the original Host header. `APP_HOST` controls listening only and does
not grant access to additional hostnames.

Webhook authorization is checked before reading the body. Payloads exceeding
`GOOGLE_HEALTH_WEBHOOK_MAX_BODY_BYTES` (default 2 MiB, configurable from 1 KiB to 16 MiB)
return 413, including chunked requests and verification payloads.

Job-start POST routes (`/sync/all`, `/sync/{data_type}`, `/backfill/start`) require
`X-HealthOS-Request: 1`. Browser requests must also have a same-origin Origin when supplied;
cross-site requests are rejected with 403. The UI includes the header automatically.
Command-line clients can omit Origin but must send the explicit header. These routes also spend
the shared manual-write budget (`MANUAL_WRITE_REQUESTS_PER_MINUTE`, default 60 per fixed minute, `429`
with `Retry-After` when exhausted). Keep CORS disabled for these local routes; the custom header
prevents cross-site forms and forces a browser preflight for cross-origin fetches.

The optional `webhook` Compose profile runs Caddy on the same host and forwards only
`/webhooks/google-health` to the private app container; every other public path returns 404.
Caddy obtains and renews a public TLS certificate without a paid cloud service.

1. Choose a DNS hostname (an owned domain or a free dynamic-DNS hostname) and point it directly to
   this host's public IP.
2. Confirm the connection is not behind carrier-grade NAT, then forward router TCP ports 80 and 443
   to this host. UDP 443 is optional and enables HTTP/3.
3. Set `WEBHOOK_HOST` to the hostname and set
   `GOOGLE_HEALTH_WEBHOOK_ENDPOINT_URI=https://<hostname>/webhooks/google-health` in `.env`.
4. Start the profile with `docker compose --profile webhook up -d caddy` and verify both Google's
   authenticated-success and unauthenticated-rejection handshakes before registering the subscriber.

This alternative is direct self-hosting: health webhook traffic is not routed through a tunnel or hosted proxy.
If the internet connection uses carrier-grade NAT or blocks inbound TCP 80/443, obtain a public IP
or explicitly choose another privacy-compatible ingress method before registration.
