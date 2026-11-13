"""Required CI commands, shared by the gate policy and its mutations."""

# The guided installer's gates (installer extension only): required by `summary` like the others,
# and, with codeql and smoke, the only jobs that skip their real work under `act`.
INSTALLER_GATES = (
    "installer_lint",
    "installer_hosts",
    "installer_linux_docker",
    "installer_windows_ps51",
)

# Each pattern must match the start of one shell command in the job, after UV_RUN is removed.
REQUIRED_COMMANDS = {
    "verify": (
        r"uv lock --check$",
        r"uv sync --frozen --extra dev$",
        r"make test-clock-preflight$",
        r"ruff check \.$",
        r"ruff format --check \.$",
        r"make coverage-security$",
        r"make test-postgres$",
        r"cp \.coverage \.test-tmp-ci-coverage/\.coverage\.sqlite$",
        r"cp \.coverage \.test-tmp-ci-coverage/\.coverage\.postgres$",
        r"make ci-coverage-report$",
        r"alembic upgrade head$",
        r"alembic check$",
        r"python scripts/verify_configuration\.py$",
        r"pyright$",
        r"docker compose --env-file \.env\.example config --quiet$",
    ),
    "bridge_boundary": (
        r"uv sync --frozen --extra dev$",
        r"pytest -q tests/test_codex_probe_decisions\.py tests/test_codex_runtime_rpc\.py$",
    ),
    "installer": (r"pytest -q tests/test_installer\*\.py$",),
    # The guided installer's gates (installer extension).
    "installer_lint": (
        r"shellcheck install\.sh$",
        r"shellcheck --severity=warning scripts/\*\.sh$",
    ),
    "installer_hosts": (
        r"--python '>=3\.12,<3\.14' pytest -q tests/test_installer_unix\.py "
        r"tests/test_installer_windows\.py ",
    ),
    "installer_linux_docker": (
        r"bash install\.sh --non-interactive --mode ",
        r"curl -fsS http://127\.0\.0\.1:8000/health/live ",
        r'BACKUP_AGE_RECIPIENT="\$recipient" bash scripts/backup\.sh$',
    ),
    "installer_windows_ps51": (
        r"\.\\install\.ps1 -DryRun -NonInteractive ",
        r"\[void\]\[System\.Management\.Automation\.Language\.Parser\]::ParseFile\(",
    ),
    "browser": (
        r"npm ci$",
        r"npm run lint$",
        r"npx vite build ",
        r"npm run test:browser$",
    ),
    "e2e": (
        r"uv sync --frozen --extra dev$",
        r"npm ci$",
        r"npx playwright install --with-deps chromium$",
        r"make test-e2e$",
    ),
    "security": (
        r"uvx .*pip-audit==\d+\.\d+\.\d+ -r /dev/stdin --strict$",
        r"npm audit --omit=dev --audit-level=high$",
        r'docker build --no-cache --build-arg SOURCE_DATE_EPOCH="\$epoch" --tag [\w.-]+:ci \.$',
        r'docker build --no-cache --build-arg SOURCE_DATE_EPOCH="\$epoch" --tag [\w.-]+:rebuild',
        r"python3 \.github/scripts/compare_images\.py [\w.:-]+ [\w.:-]+$",
    ),
    "workflows": (r"command -v shellcheck$", r"actionlint -color$"),
    "secrets": (
        r"bash tests/security/gitleaks\.sh$",
        r'gitleaks git \. --log-opts="--all" --redact$',
    ),
    "codeql": (r'python3 \.github/scripts/codeql_gate\.py "\$SARIF_DIR"$',),
    "smoke": (r"make compose-smoke$",),
}
