SHELL := /bin/bash

ifneq (,$(wildcard .env))
include .env
export
endif

B1_DATA_ROOT ?= /srv/b1-ai-hub
B1_BACKUP_ROOT ?= $(B1_DATA_ROOT)/backups
B1_BACKUP_ENCRYPTION_MODE ?= none
B1_BACKUP_ENCRYPTION_KEY_FILE ?= $(B1_DATA_ROOT)/secrets/master_encryption_key

.PHONY: bootstrap validate compose-config legacy-compose-config production-localai-compose-config production-comfyui-compose-config unit openapi openapi-check sbom secret-scan db-migrate db-current inventory old-stack-scope old-stack-backup old-stack-backup-verify open-webui-migration-plan cutover-plan backup restore up down logs

bootstrap:
	python3 deploy/scripts/bootstrap.py --root "$(B1_DATA_ROOT)"

validate: compose-config legacy-compose-config production-localai-compose-config production-comfyui-compose-config unit

compose-config:
	docker compose config --quiet

legacy-compose-config:
	docker compose -f compose.yaml -f compose.legacy-comfy.yaml --profile legacy-comfy config --quiet

production-localai-compose-config:
	docker compose -f compose.yaml -f compose.production-localai.yaml config --quiet

production-comfyui-compose-config:
	docker compose -f compose.yaml -f compose.production-comfyui.yaml config --quiet

unit:
	python3 -m unittest discover -s tests/unit -v

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
	python3 deploy/scripts/inventory.py --output "$(B1_DATA_ROOT)/backups/inventory-$$(date +%Y%m%d-%H%M%S).json"

old-stack-scope:
	@test -n "$(INVENTORY)" || (echo "INVENTORY=/path/to/inventory.json is required" >&2; exit 2)
	python3 deploy/scripts/old_stack_backup.py plan --inventory "$(INVENTORY)" --output "$(B1_DATA_ROOT)/backups/old-stack-scope.json"

old-stack-backup:
	@test -n "$(SCOPE)" || (echo "SCOPE=/path/to/old-stack-scope.json is required" >&2; exit 2)
	python3 deploy/scripts/old_stack_backup.py backup --scope "$(SCOPE)" --output-root "$(B1_DATA_ROOT)/backups"

old-stack-backup-verify:
	@test -n "$(BACKUP)" || (echo "BACKUP=/path/to/old-stack-backup is required" >&2; exit 2)
	python3 deploy/scripts/old_stack_backup.py verify --backup "$(BACKUP)"

open-webui-migration-plan:
	@test -n "$(INVENTORY)" || (echo "INVENTORY=/path/to/inventory.json is required" >&2; exit 2)
	@test -n "$(BACKUP)" || (echo "BACKUP=/path/to/old-stack-backup is required" >&2; exit 2)
	python3 deploy/scripts/open_webui_migration.py --inventory "$(INVENTORY)" --backup "$(BACKUP)" --output "$(B1_DATA_ROOT)/backups/open-webui-migration-plan.json"

cutover-plan:
	@test -n "$(INVENTORY)" || (echo "INVENTORY=/path/to/inventory.json is required" >&2; exit 2)
	@test -n "$(SCOPE)" || (echo "SCOPE=/path/to/old-stack-scope.json is required" >&2; exit 2)
	@test -n "$(BACKUP)" || (echo "BACKUP=/path/to/old-stack-backup is required" >&2; exit 2)
	@test -n "$(OPEN_WEBUI_PLAN)" || (echo "OPEN_WEBUI_PLAN=/path/to/open-webui-migration-plan.json is required" >&2; exit 2)
	python3 deploy/scripts/cutover.py --inventory "$(INVENTORY)" --scope "$(SCOPE)" --backup "$(BACKUP)" --open-webui-plan "$(OPEN_WEBUI_PLAN)" --output "$(B1_DATA_ROOT)/backups/cutover-plan.json"

backup:
	python3 deploy/scripts/backup.py --root "$(B1_DATA_ROOT)" --backup-root "$(B1_BACKUP_ROOT)" --backup-encryption-mode "$(B1_BACKUP_ENCRYPTION_MODE)" --backup-encryption-key-file "$(B1_BACKUP_ENCRYPTION_KEY_FILE)"

restore:
	@test -n "$(BACKUP)" || (echo "BACKUP=/path/to/backup is required" >&2; exit 2)
	@test -n "$(RESTORE_ROOT)" || (echo "RESTORE_ROOT=/path/to/alternate/root is required" >&2; exit 2)
	python3 deploy/scripts/restore.py --backup "$(BACKUP)" --target "$(RESTORE_ROOT)" --backup-encryption-key-file "$(B1_BACKUP_ENCRYPTION_KEY_FILE)"

up:
	docker compose up -d

down:
	docker compose down

logs:
	docker compose logs -f --tail=200
