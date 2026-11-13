# Local UI session

With `APP_UI_TOKEN` empty, login can be turned off only outside production and only when `APP_HOST`
is exactly `127.0.0.1`, `::1` or `localhost`. Any other value, for example `0.0.0.0` or `::`, is a
startup error in settings validation. In production the token is always required. The UI and API
must stay reachable only over loopback; setting a token is not permission to expose them to a
network.

`APP_HOST` does not decide Uvicorn's actual bind address. Without a token, only a request whose
client address is loopback gets through; any other client gets 403 from the UI and API, and its
WebSocket is closed. The listener's address does not count: a port-forward or NAT rule can bring a
request from another host onto a loopback listener. `GET/HEAD /health/live` and
`POST /webhooks/google-health` keep their own rules. When starting Uvicorn by hand, also pass
`--host 127.0.0.1`.

The `ui_login_disabled` field of `GET /health` is `true` while login is off and `false` when a token
is set, including in the 503 response during a database failure. While login is off, the interface
shows a persistent warning on every tab.

## Session lifetime

By default, browser page requests on plain HTTP `localhost` or `127.0.0.1` move to
`healthos.localhost` before displaying the login form, so the key is entered once. A direct
`POST /login` on either shared host returns `409` with a link and `Location` pointing to
`http://healthos.localhost:<same-port>/login?next=<safe-path>`, without a cookie or a successful
login audit event. The access key is never placed in the URL. A login lasts
30 days on `healthos.localhost` or over HTTPS. When `APP_PUBLIC_URL` explicitly selects another
interface address and disables canonical redirects, HTTP shared-host logins keep their 24-hour
lifetime. A cookie cannot be scoped to a port, so every other server the browser reaches on `localhost` receives it too; `__Host-`, the cookie name and its Path do not change
that. `healthos.localhost` has its own cookie jar, which is why it is the interface's address. The
login page says which lifetime applies, and on plain `localhost`, `127.0.0.1` or `[::1]` it links to
the same login on `healthos.localhost`. Restarting the app logs every browser out, and logging out
also closes an open Umay chat at once.

A page opened on plain `localhost` or `127.0.0.1` (a browser navigation: `GET`/`HEAD` asking for HTML)
is sent with `307` to the same path, query and port on `healthos.localhost`, before displaying
the login page. API calls, static assets, the WebSocket, health checks and the Google OAuth paths
(`/auth/google/...`) stay where they are, and the public webhook host never gets this far. Setting
`APP_PUBLIC_URL` to another address, for example `http://localhost:8000`, is the explicit way to keep
the interface on it: then nothing moves and an existing `localhost` session keeps working.

The default `GOOGLE_REDIRECT_URI` is `http://healthos.localhost:8000/auth/google/callback`,
in both settings and `.env.example`. Register that exact URI (including the installation's port)
in the OAuth client's authorized redirect URIs in Google Cloud Console **before** using the new
default; an unregistered URI causes `redirect_uri_mismatch` and prevents connection. Existing
installations may keep their explicit `GOOGLE_REDIRECT_URI=http://localhost:8000/auth/google/callback`
in `.env`; the code default does not replace it. If the variable is absent, register the new URI
before upgrading or explicitly keep the old value.

For an explicitly configured `localhost` callback, starting the connection on `healthos.localhost`
still issues a single-use ticket (valid for two minutes) and moves to
`localhost:8000/auth/google/handoff`, which spends it and only then sets the OAuth binding cookie
(path `/auth/google`, SameSite=Lax) and redirects to Google. This handoff issues no UI session
on `localhost`.

## Login throttle

Password guessing is limited for the whole application, not per address or account, so a flood
cannot lock anyone out. Five attempts are allowed through at once and about two per second after
that, up to an 8-second queue; beyond it the answer is `429` with `Retry-After: 8`, and the token is
not read. Sessions already issued and API access with `Authorization: Bearer` do not pass through it.

## Cookies

UI and OAuth session cookies stay HttpOnly; the UI uses SameSite=Strict and OAuth SameSite=Lax. A
cookie carries `Secure` when the request is HTTPS or `APP_PUBLIC_URL` starts with `https://`.
`APP_PUBLIC_URL` is unset by default; plain HTTP loopback use adds no `Secure`. With an HTTPS address
set, the browser will not send the cookie over plain HTTP. The logout response uses the same
`Secure` decision, and the session is revoked on the server.

**Do not** put the Cloudflare webhook tunnel's address in `APP_PUBLIC_URL`; this setting is for the
address the interface is opened at. Leave it empty when you use the interface over local HTTP. The
`Secure` cookie forced by an HTTPS tunnel address can send Safari into a local-HTTP login loop, and
breaks login in every browser over a LAN HTTP connection. The webhook's tunnel address belongs in
`GOOGLE_HEALTH_WEBHOOK_ENDPOINT_URI`.

`APP_TRUST_PROXY_HEADERS` defaults to `false`. Set it to `true` only behind a trusted proxy with
controlled access that removes any incoming `X-Forwarded-Proto` and writes its own. A single
`X-Forwarded-Proto: https` header then counts as HTTPS; repeated headers and comma-separated chains
are not accepted. Host validation is unchanged, so the proxy must preserve the original Host header.
The Origin check on form and API write requests uses the trusted protocol as well. A direct TLS
request cannot be downgraded by an HTTP header.

`make run` and the container start command run Uvicorn with `--no-proxy-headers`, so the headers are
not interpreted before the application setting. When starting Uvicorn by hand, use this option too,
and let `APP_TRUST_PROXY_HEADERS` alone make the trust decision. These settings add no new CLI
option.
