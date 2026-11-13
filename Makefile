UV_RUN = uv run --frozen --extra dev

.PHONY: env install hooks lint test test-order test-browser test-e2e test-clock test-clock-preflight test-postgres typecheck verify-config verify coverage \
	coverage-security migrate run compose-up compose-down compose-smoke backup restore test-backup-unit bump-interim bump-target
install:
	uv sync --frozen --extra dev
env:
	python3 scripts/init_env.py
bump-interim:
	python3 scripts/bump_version.py interim
bump-target:
	python3 scripts/bump_version.py target $(VERSION)
hooks:
	git config --local core.hooksPath scripts/hooks
lint:
	$(UV_RUN) ruff check .
	$(UV_RUN) ruff format --check .
# One worker per CPU (pytest-xdist); `pytest -q -p no:xdist` runs it in one process.
test:
	$(UV_RUN) pytest -q -n auto
# The unit suite in a random order and the given (default: this run's) seed: order dependence,
# which `test` cannot show. `make test-order SEED=123` repeats a run.
test-order:
	@seed=$${SEED:-$$(date +%s)}; echo "test-order: seed $$seed"; \
	PYTHONPATH=tests/support HEALTHOS_TEST_ORDER_SEED=$$seed $(UV_RUN) pytest -q -n auto -p shuffled_order
# The unit suite at clock settings that break date arithmetic and on non-UTC hosts (slow; see
# tests/support/clock_matrix.sh for what is covered).
test-clock:
	bash tests/support/clock_matrix.sh
# Under a minute: proves only that the clock harness can shift time; runs on every change (ci.yml).
test-clock-preflight:
	bash tests/support/clock_matrix.sh --preflight
# PostgreSQL decides what SQLite cannot: locking (skip-locked, advisory locks), triggers, numeric
# limits and time zones. DATABASE_URL must point at a disposable server: without it this fails,
# it does not skip. tests/integration, then the whole unit suite against a migrated database;
# the few tests that are about SQLite itself are named in tests/support/pg_backend.py.
test-postgres:
	@test -n "$(DATABASE_URL)" || { echo "make test-postgres needs DATABASE_URL=postgresql+asyncpg://..." >&2; exit 2; }
	HEALTHOS_REQUIRE_POSTGRES=1 APP_ENV=test $(UV_RUN) pytest -q -n auto -m integration tests/integration
	HEALTHOS_REQUIRE_POSTGRES=1 HEALTHOS_TEST_BACKEND=postgres APP_ENV=test $(UV_RUN) pytest -q -n auto -m "not integration" --ignore=tests/integration
# Unit suite with line and branch coverage of app/; fails under fail_under (pyproject.toml).
# PostgreSQL integration tests stay out even when DATABASE_URL points at a server: they belong
# to make test-postgres, which CI runs as its own step.
coverage:
	$(UV_RUN) pytest -q -n auto -m "not integration" --cov=app --cov-report=term
# Authentication, credentials, login and webhook verification: a floor of their own (IP-6),
# read from the data `make coverage` left behind.
SECURITY_MODULES = app/core/auth.py,app/services/providers/credentials.py,app/api/auth.py,app/api/login.py,app/api/webhooks.py,app/google_health/webhook.py
SECURITY_FLOOR = 90
coverage-security: coverage
	$(UV_RUN) coverage report --include=$(SECURITY_MODULES) --fail-under=$(SECURITY_FLOOR) \
		--show-missing
# CI keeps the SQLite baseline and PostgreSQL measurements before enforcing the same floors.
.PHONY: ci-coverage-report
ci-coverage-report:
	$(UV_RUN) coverage combine --keep .test-tmp-ci-coverage/.coverage.sqlite .test-tmp-ci-coverage/.coverage.postgres
	$(UV_RUN) coverage report
	$(UV_RUN) coverage report --include=$(SECURITY_MODULES) --fail-under=$(SECURITY_FLOOR) \
		--show-missing
test-browser:
	cd frontend && npm run test:browser
# One browser journey through the built frontend, the real app and PostgreSQL (login, panel, a
# measurement, a reload, the one-screen rule on a synthetic archive). DATABASE_URL must point at a
# disposable local server with a password; without one this fails, it does not skip. Needs Node
# and Playwright's Chromium.
test-e2e:
	$(UV_RUN) python tests/e2e/run_journey.py
typecheck:
	$(UV_RUN) pyright app
verify-config:
	$(UV_RUN) python scripts/verify_configuration.py
# Run .github/workflows/ci.yml for one commit under act and Docker, and record the receipt the
# pre-push hook requires. Default: the GitHub snapshot branch. `make ci-gate REV=<rev>`.
.PHONY: ci-gate
ci-gate:
	bash scripts/ci_gate.sh $(REV)
# Lint + format check, configuration/link verifier, unit tests and pyright, in that
# order; stops at the first failure. Browser tests are separate (make test-browser).
verify: lint verify-config test typecheck
migrate:
	$(UV_RUN) alembic upgrade head
run:
	$(UV_RUN) uvicorn app.main:app --reload --host 127.0.0.1 --port 8000 --no-access-log --no-proxy-headers
compose-up:
	docker compose up --build -d
compose-down:
	docker compose down
compose-smoke:
	$(UV_RUN) python scripts/verify_compose.py
backup:
	bash scripts/backup.sh
# The real healthos-backup.service under systemd (Linux, or the Colima VM on macOS), with the
# ownership DEPLOYMENT.md leaves behind; see scripts/verify_backup_unit.sh.
test-backup-unit:
	bash scripts/verify_backup_unit.sh
restore:
	@test -n "$(FILE)" || { echo "usage: make restore FILE=backups/health-<stamp>.dump.age [DB=health_restore]" >&2; exit 2; }
	bash scripts/restore.sh "$(FILE)" $(DB)
