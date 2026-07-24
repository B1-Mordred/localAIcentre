SHELL := /bin/bash

ifneq (,$(wildcard .env))
include .env
export
endif

B1_DATA_ROOT ?= /srv/b1-ai-hub
B1_BACKUP_ROOT ?= $(B1_DATA_ROOT)/backups
B1_BACKUP_ENCRYPTION_MODE ?= none
B1_BACKUP_ENCRYPTION_KEY_FILE ?= $(B1_DATA_ROOT)/secrets/master_encryption_key
B1_BACKUP_MIGRATION_ROLLBACK_EVIDENCE ?= $(B1_BACKUP_ROOT)/acceptance/backup-migration-rollback.json
CADDY_IMAGE ?= caddy:2.10.2-alpine

.PHONY: bootstrap validate compose-config legacy-compose-config production-localai-compose-config production-comfyui-compose-config production-voicebox-compose-config production-env-compose-config caddy-config unit smoke integration localai-acceptance gpu-acceptance restart-reconciliation-acceptance compatibility security security-acceptance openapi openapi-check sbom secret-scan db-migrate db-current inventory old-stack-scope old-stack-backup old-stack-backup-verify open-webui-migration-plan cutover-plan backup restore backup-migration-rollback-evidence up down logs

bootstrap:
	python3 deploy/scripts/bootstrap.py --root "$(B1_DATA_ROOT)"

validate: compose-config legacy-compose-config production-localai-compose-config production-comfyui-compose-config production-voicebox-compose-config production-env-compose-config caddy-config unit

compose-config:
	docker compose config --quiet

legacy-compose-config:
	docker compose -f compose.yaml -f compose.legacy-comfy.yaml --profile legacy-comfy config --quiet

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
