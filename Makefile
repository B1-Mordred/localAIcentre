SHELL := /bin/bash

ifneq (,$(wildcard .env))
include .env
export
endif

B1_DATA_ROOT ?= /srv/b1-ai-hub
B1_BACKUP_ROOT ?= $(B1_DATA_ROOT)/backups
B1_BACKUP_ENCRYPTION_MODE ?= none
B1_BACKUP_ENCRYPTION_KEY_FILE ?= $(B1_DATA_ROOT)/secrets/master_encryption_key
B1_ROLLBACK_REHEARSAL_REPORT ?= $(B1_BACKUP_ROOT)/rollback-rehearsal.json
B1_BACKUP_MIGRATION_ROLLBACK_EVIDENCE ?= $(B1_BACKUP_ROOT)/acceptance/backup-migration-rollback.json
B1_VOICEBOX_AUDIT_REPORT ?= artifacts/pip-audit/voicebox-constraints.json
ROLLBACK_REPORT ?= $(B1_ROLLBACK_REHEARSAL_REPORT)
CADDY_IMAGE ?= caddy:2.10.2-alpine@sha256:4c6e91c6ed0e2fa03efd5b44747b625fec79bc9cd06ac5235a779726618e530d
B1_QUALITY_PYTHON ?= python3.12
B1_QUALITY_PYTHON_IMAGE ?= python:3.12.11-slim-bookworm@sha256:519591d6871b7bc437060736b9f7456b8731f1499a57e22e6c285135ae657bf7
B1_SERVICE_REQUIREMENTS := services/control-plane/requirements.txt services/runtime-agent/requirements.txt services/artifact-server/requirements.txt services/audio-cpu/requirements.txt services/mock-runtime/requirements.txt

.PHONY: prepare-production-env bootstrap validate quality quality-local quality-container backend-python-quality-container compose-config legacy-compose-config monitoring-compose-config production-localai-compose-config production-comfyui-compose-config production-voicebox-compose-config production-env-compose-config caddy-config python-check frontend frontend-control-center frontend-media-studio unit smoke integration localai-acceptance gpu-acceptance restart-reconciliation-acceptance compatibility security security-acceptance openapi openapi-check sbom secret-scan voicebox-audit-inventory db-migrate db-current inventory old-stack-scope old-stack-backup old-stack-backup-verify open-webui-migration-plan cutover-plan rollback-rehearsal-report backup restore backup-migration-rollback-evidence up down logs

prepare-production-env:
	python3 deploy/scripts/prepare_env.py --template .env.production.example --output .env --docker-socket /var/run/docker.sock --update-existing

bootstrap:
	python3 deploy/scripts/bootstrap.py --root "$(B1_DATA_ROOT)"

validate: compose-config legacy-compose-config monitoring-compose-config production-localai-compose-config production-comfyui-compose-config production-voicebox-compose-config production-env-compose-config caddy-config python-check unit compatibility security

quality:
	set -e; \
	if command -v "$(B1_QUALITY_PYTHON)" >/dev/null && "$(B1_QUALITY_PYTHON)" -c 'import sys; raise SystemExit(0 if sys.version_info[:2] == (3, 12) else 1)' >/dev/null 2>&1; then \
		$(MAKE) quality-local; \
	else \
		echo "B1_QUALITY_PYTHON=$(B1_QUALITY_PYTHON) is not Python 3.12; using $(B1_QUALITY_PYTHON_IMAGE) for backend Python quality checks" >&2; \
		$(MAKE) quality-container; \
	fi

quality-local:
	set -e; \
	"$(B1_QUALITY_PYTHON)" -c 'import sys; raise SystemExit(0 if sys.version_info[:2] == (3, 12) else "B1_QUALITY_PYTHON must be Python 3.12")'; \
	tmpdir="$$(mktemp -d)"; \
	trap 'rm -rf "$$tmpdir"' EXIT; \
	"$(B1_QUALITY_PYTHON)" -m venv "$$tmpdir/venv"; \
	"$$tmpdir/venv/bin/python" -m pip install PyYAML==6.0.2 $(foreach requirement,$(B1_SERVICE_REQUIREMENTS),-r $(requirement)); \
	PATH="$$tmpdir/venv/bin:$$PATH" $(MAKE) validate openapi-check; \
	$(MAKE) frontend

quality-container: compose-config legacy-compose-config monitoring-compose-config production-localai-compose-config production-comfyui-compose-config production-voicebox-compose-config production-env-compose-config caddy-config backend-python-quality-container compatibility security frontend

backend-python-quality-container:
	docker run --rm -e PYTHONPYCACHEPREFIX=/tmp/pycache -v "$(CURDIR):/repo" -w /repo "$(B1_QUALITY_PYTHON_IMAGE)" sh -c 'python -m pip install PyYAML==6.0.2 $(foreach requirement,$(B1_SERVICE_REQUIREMENTS),-r $(requirement)) && python -m compileall -q services deploy integrations tests && python -m unittest discover -s tests/unit -v && python deploy/scripts/generate_openapi.py --output docs/openapi.json --check'

compose-config:
	docker compose config --quiet

legacy-compose-config:
	docker compose -f compose.yaml -f compose.legacy-comfy.yaml --profile legacy-comfy config --quiet

monitoring-compose-config:
	docker compose -f compose.yaml -f compose.monitoring.yaml --profile monitoring config --quiet

production-localai-compose-config:
	docker compose -f compose.yaml -f compose.production-localai.yaml config --quiet

production-comfyui-compose-config:
	docker compose -f compose.yaml -f compose.production-comfyui.yaml config --quiet

production-voicebox-compose-config:
	docker compose -f compose.yaml -f compose.production-voicebox.yaml --profile voicebox config --quiet

production-env-compose-config:
	COMPOSE_FILE="$$(sed -n 's/^COMPOSE_FILE=//p' .env.production.example)" COMPOSE_PROFILES="$$(sed -n 's/^COMPOSE_PROFILES=//p' .env.production.example)" docker compose --env-file .env.production.example config --quiet

caddy-config:
	docker run --rm -v "$(CURDIR)/deploy/caddy:/etc/caddy:ro" "$(CADDY_IMAGE)" caddy adapt --config /etc/caddy/Caddyfile >/dev/null
	docker run --rm -v "$(CURDIR)/deploy/caddy:/etc/caddy:ro" "$(CADDY_IMAGE)" caddy adapt --config /etc/caddy/Caddyfile.legacy-comfy >/dev/null

python-check:
	tmpdir="$$(mktemp -d)"; trap 'rm -rf "$$tmpdir"' EXIT; PYTHONPYCACHEPREFIX="$$tmpdir/pycache" python3 -m compileall -q services deploy integrations tests

frontend: frontend-control-center frontend-media-studio

frontend-control-center:
	npm --prefix web/control-center ci
	npm --prefix web/control-center run build
	npm --prefix web/control-center audit --omit=dev --audit-level=high

frontend-media-studio:
	npm --prefix web/media-studio ci
	npm --prefix web/media-studio run build
	npm --prefix web/media-studio audit --omit=dev --audit-level=high

unit:
	python3 -m unittest discover -s tests/unit -v

smoke:
	python3 -m unittest discover -s tests/smoke -v

integration:
	python3 -m unittest discover -s tests/integration -v

localai-acceptance:
	B1_LOCALAI_ACCEPTANCE_LIVE_TEST=1 python3 -m unittest tests.integration.test_live_localai_runtime -v

gpu-acceptance:
	B1_GPU_ACCEPTANCE_LIVE_TEST=1 python3 -m unittest tests.integration.test_live_cross_runtime_gpu -v

restart-reconciliation-acceptance:
	B1_RESTART_RECONCILIATION_LIVE_TEST=1 python3 -m unittest tests.integration.test_live_restart_reconciliation -v

compatibility:
	python3 -m unittest discover -s tests/compatibility -v

security:
	python3 -m unittest discover -s tests/security -v

security-acceptance:
	B1_SECURITY_LIVE_TEST=1 python3 -m unittest tests.security.test_live_security_acceptance -v

openapi:
	python3 deploy/scripts/generate_openapi.py --output docs/openapi.json

openapi-check:
	python3 deploy/scripts/generate_openapi.py --output docs/openapi.json --check

sbom:
	python3 deploy/scripts/generate_sbom.py --output artifacts/sbom/b1-ai-hub.cdx.json

secret-scan:
	python3 deploy/scripts/secret_scan.py

voicebox-audit-inventory:
	mkdir -p "$(dir $(B1_VOICEBOX_AUDIT_REPORT))"
	pip-audit --no-deps --disable-pip -r deploy/voicebox/constraints.txt --format json --output "$(B1_VOICEBOX_AUDIT_REPORT)" || true
	test -s "$(B1_VOICEBOX_AUDIT_REPORT)"

db-migrate:
	docker compose run --rm control-plane python -m app.migrate upgrade head

db-current:
	docker compose run --rm control-plane python -m app.migrate current --verbose

inventory:
	python3 deploy/scripts/inventory.py --output "$(B1_BACKUP_ROOT)/inventory-$$(date +%Y%m%d-%H%M%S).json"

old-stack-scope:
	@test -n "$(INVENTORY)" || (echo "INVENTORY=/path/to/inventory.json is required" >&2; exit 2)
	python3 deploy/scripts/old_stack_backup.py plan --inventory "$(INVENTORY)" --output "$(B1_BACKUP_ROOT)/old-stack-scope.json"

old-stack-backup:
	@test -n "$(SCOPE)" || (echo "SCOPE=/path/to/old-stack-scope.json is required" >&2; exit 2)
	python3 deploy/scripts/old_stack_backup.py backup --scope "$(SCOPE)" --output-root "$(B1_BACKUP_ROOT)"

old-stack-backup-verify:
	@test -n "$(BACKUP)" || (echo "BACKUP=/path/to/old-stack-backup is required" >&2; exit 2)
	python3 deploy/scripts/old_stack_backup.py verify --backup "$(BACKUP)"

open-webui-migration-plan:
	@test -n "$(INVENTORY)" || (echo "INVENTORY=/path/to/inventory.json is required" >&2; exit 2)
	@test -n "$(BACKUP)" || (echo "BACKUP=/path/to/old-stack-backup is required" >&2; exit 2)
	python3 deploy/scripts/open_webui_migration.py --inventory "$(INVENTORY)" --backup "$(BACKUP)" --output "$(B1_BACKUP_ROOT)/open-webui-migration-plan.json"

cutover-plan:
	@test -n "$(INVENTORY)" || (echo "INVENTORY=/path/to/inventory.json is required" >&2; exit 2)
	@test -n "$(SCOPE)" || (echo "SCOPE=/path/to/old-stack-scope.json is required" >&2; exit 2)
	@test -n "$(BACKUP)" || (echo "BACKUP=/path/to/old-stack-backup is required" >&2; exit 2)
	@test -n "$(OPEN_WEBUI_PLAN)" || (echo "OPEN_WEBUI_PLAN=/path/to/open-webui-migration-plan.json is required" >&2; exit 2)
	python3 deploy/scripts/cutover.py --inventory "$(INVENTORY)" --scope "$(SCOPE)" --backup "$(BACKUP)" --open-webui-plan "$(OPEN_WEBUI_PLAN)" --output "$(B1_BACKUP_ROOT)/cutover-plan.json"

rollback-rehearsal-report:
	@test -n "$(CUTOVER_PLAN)" || (echo "CUTOVER_PLAN=/path/to/cutover-plan.json is required" >&2; exit 2)
	@test -n "$(REHEARSED_BY)" || (echo "REHEARSED_BY=operator-name is required" >&2; exit 2)
	@test "$(ROLLBACK_COMMANDS_TESTED)" = "1" || (echo "ROLLBACK_COMMANDS_TESTED=1 is required after a non-destructive rollback rehearsal" >&2; exit 2)
	@test "$(OLD_RESOURCES_PRESERVED)" = "1" || (echo "OLD_RESOURCES_PRESERVED=1 is required after verifying old resources remain preserved" >&2; exit 2)
	python3 deploy/scripts/rollback_rehearsal.py --cutover-plan "$(CUTOVER_PLAN)" --rehearsed-by "$(REHEARSED_BY)" --rollback-commands-tested --old-resources-preserved --output "$(B1_ROLLBACK_REHEARSAL_REPORT)"

backup:
	python3 deploy/scripts/backup.py --root "$(B1_DATA_ROOT)" --backup-root "$(B1_BACKUP_ROOT)" --backup-encryption-mode "$(B1_BACKUP_ENCRYPTION_MODE)" --backup-encryption-key-file "$(B1_BACKUP_ENCRYPTION_KEY_FILE)"

restore:
	@test -n "$(BACKUP)" || (echo "BACKUP=/path/to/backup is required" >&2; exit 2)
	@test -n "$(RESTORE_ROOT)" || (echo "RESTORE_ROOT=/path/to/alternate/root is required" >&2; exit 2)
	python3 deploy/scripts/restore.py --backup "$(BACKUP)" --target "$(RESTORE_ROOT)" --backup-encryption-key-file "$(B1_BACKUP_ENCRYPTION_KEY_FILE)"

backup-migration-rollback-evidence:
	@test -n "$(B1_BACKUP_DIR)" || (echo "B1_BACKUP_DIR=/path/to/b1-backup is required" >&2; exit 2)
	@test -n "$(RESTORE_REPORT)" || (echo "RESTORE_REPORT=/path/to/restore-report.json is required" >&2; exit 2)
	@test -n "$(INVENTORY)" || (echo "INVENTORY=/path/to/inventory.json is required" >&2; exit 2)
	@test -n "$(OLD_STACK_BACKUP)" || (echo "OLD_STACK_BACKUP=/path/to/old-stack-backup is required" >&2; exit 2)
	@test -n "$(OPEN_WEBUI_PLAN)" || (echo "OPEN_WEBUI_PLAN=/path/to/open-webui-migration-plan.json is required" >&2; exit 2)
	@test -n "$(CUTOVER_PLAN)" || (echo "CUTOVER_PLAN=/path/to/cutover-plan.json is required" >&2; exit 2)
	@test -n "$(ROLLBACK_REPORT)" || (echo "ROLLBACK_REPORT=/path/to/rollback-rehearsal.json is required" >&2; exit 2)
	python3 deploy/scripts/backup_migration_rollback_evidence.py --b1-backup "$(B1_BACKUP_DIR)" --restore-report "$(RESTORE_REPORT)" --inventory "$(INVENTORY)" --old-stack-backup "$(OLD_STACK_BACKUP)" --open-webui-plan "$(OPEN_WEBUI_PLAN)" --cutover-plan "$(CUTOVER_PLAN)" --rollback-report "$(ROLLBACK_REPORT)" --output "$(B1_BACKUP_MIGRATION_ROLLBACK_EVIDENCE)" --backup-encryption-key-file "$(B1_BACKUP_ENCRYPTION_KEY_FILE)"

up:
	docker compose up -d

down:
	docker compose down

logs:
	docker compose logs -f --tail=200
