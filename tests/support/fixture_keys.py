"""Synthetic secrets for tests, derived at import time so no literal key sits in the tree."""

import base64

# 32 ASCII bytes -> a syntactically valid Fernet key; never used outside tests.
SYNTHETIC_FERNET_KEY = base64.urlsafe_b64encode(b"synthetic-fernet-key-32-bytes-!!").decode("ascii")
