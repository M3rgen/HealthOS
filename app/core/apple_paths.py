"""Paths of the Apple Health endpoints, shared by the router and the middlewares."""

# Health Auto Export (iOS) posts here; the only Apple path a non-loopback host may reach.
APPLE_RECEIVER_PATH = "/webhooks/apple-health"
# The local UI uploads export.zip/export.xml here (loopback and the UI token, like /api/*).
APPLE_IMPORT_PATH = "/api/apple/import"
