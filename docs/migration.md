# Migration and Cutover

Before touching the old deployment:

```bash
make inventory
```

The generated report is written under `$B1_BACKUP_ROOT`, which defaults to `$B1_DATA_ROOT/backups/`, and is read-only. Newly created inventory output directories are owner-only on POSIX systems, the report file is written mode `0600`, and an existing output symlink is refused where the platform supports no-follow opens; still treat the file as sensitive host migration evidence because it contains paths, Docker metadata, ports, DNS, mount, and storage summaries. It captures:

- all Docker containers, Compose projects, volumes, networks, Docker version/info, and NVIDIA runtime availability
- systemd service units relevant to AI, B1 AI Hub, or explicitly preserved local services, including redacted unit metadata and unit/drop-in paths for review
- listening TCP sockets and processes
- `nvidia-smi` GPU driver/VRAM/utilization data and NVIDIA Container Toolkit version when available
- hardware-profile readiness against the initial 12 GB VRAM / 32 GB RAM baseline
- host memory, disks, mount tree, DNS records for the core B1 AI Hub hostnames plus optional profile hostnames such as `monitoring.ai.b1.germering`, and resolver configuration
- candidate model directories, Open WebUI data/database locations, and Compose files under bounded scan roots
- bounded model-directory storage summaries, including model-like file counts, sizes, suffix counts, and sample relative model filenames
- read-only Open WebUI SQLite metadata, including table names and aggregate counts for known tables such as users and chats; row contents are not read
- port-review evidence for production gateway ports, common old AI service ports, Ollama, Open WebUI, and optional legacy ComfyUI listeners
- container, Compose, and systemd-service classifications: `candidate-old-ai-stack-review-required`, `preserve-unrelated`, `b1-ai-hub-current-preserve`, or `unknown-preserve-by-default`

Review the generated inventory and classify old-stack services explicitly. Treat `candidate-old-ai-stack-review-required` as a prompt for human review, not as permission to stop or modify anything. Treat `unknown-preserve-by-default` as out of scope until an operator marks it otherwise. Do not assume Hermes, Yggdrasil, Discord integrations, Technitium, n8n, databases, DNS services, or unrelated containers are in scope.

Optional bounded scans can be added when old Compose files are stored somewhere unusual:

```bash
python3 deploy/scripts/inventory.py \
  --output "$B1_BACKUP_ROOT/inventory-extra.json" \
  --b1-root "$B1_DATA_ROOT" \
  --scan-root /srv \
  --scan-root /opt \
  --scan-root /home/mordred
```

The inventory report redacts common bearer tokens, generated B1 keys, password-like fields, and token-like fields in captured command output. It does not read `.env` files or Open WebUI database row contents. Docker container and volume inspection is whitelisted to safe metadata such as image, state, labels, networks, and mounts; environment variables are not stored in the report. SQLite inspection is opened read-only and records schema/table counts only, so operators can judge whether an old Open WebUI database is likely worth preserving without exposing chats or prompts in the report. If a discovered Docker volume path is not readable by the current user, the inventory records it under `open_webui_data_roots.unreadable_or_unscannable` instead of treating the database as absent.

## Reviewed Old-Stack Backup

After reviewing the inventory, generate an explicit backup scope template:

```bash
make old-stack-scope INVENTORY=/srv/b1-ai-hub/backups/inventory-20260722-120000.json
```

This writes `$B1_BACKUP_ROOT/old-stack-scope.json`. It lists candidate old AI containers, candidate old AI systemd services such as a host-managed Ollama daemon, AI-hinted Docker volumes, Compose file paths, systemd unit/drop-in paths, Open WebUI data/database paths, discovered but unreadable Open WebUI Docker data roots, model directories, and the inventory's hardware/port/model/Open WebUI readiness summary, but it selects nothing automatically.

Edit the scope file only after operator review:

```json
{
  "operator_reviewed": true,
  "reviewed_by": "admin-name",
  "include_paths": [
    {
      "path": "/srv/old-ai-stack/docker-compose.yaml",
      "reason": "old AI Compose file"
    },
    {
      "path": "/srv/open-webui/data",
      "reason": "old Open WebUI data"
    }
  ],
  "include_docker_volumes": [
    {
      "name": "open-webui_data",
      "reason": "old Open WebUI volume"
    }
  ],
  "include_containers": [
    {
      "name": "open-webui",
      "reason": "container inspect metadata for rollback"
    }
  ],
  "include_systemd_services": [
    {
      "name": "ollama.service",
      "reason": "host Ollama service to stop during GPU cutover and restart during rollback"
    }
  ]
}
```

Create and verify the reviewed old-stack backup:

```bash
make old-stack-backup SCOPE=/srv/b1-ai-hub/backups/old-stack-scope.json
make old-stack-backup-verify BACKUP=/srv/b1-ai-hub/backups/old-stack-20260722-120000
```

The old-stack backup format is `b1-ai-hub-old-stack-backup/v1`. The archive includes only explicitly listed host paths, explicitly listed Docker volume contents read from their Docker mountpoints, explicitly listed container `docker inspect` metadata, and explicitly listed systemd service metadata plus the unit/drop-in files reported by `systemctl show`. Raw `docker inspect` and raw systemd metadata are preserved for rollback evidence and marked sensitive because they may store environment variables and credentials. Redacted companions are written under `docker-inspect-redacted/containers/` and `systemd/services-redacted/` for operator review. It refuses to run unless `operator_reviewed=true` and `reviewed_by` is set. It rejects symlinked sources, symlinks inside selected directories, special files, unsafe Docker or systemd service names, and pseudo-filesystem roots such as `/proc`, `/sys`, `/dev`, and `/run`.

Use `inventory_review.port_review` in the scope template to decide which old services must be drained before B1 claims production ports. A listener on `80`, `443`, `8188`, `11434`, `11438`, `3000`, `7860`, `8000`, or `8443` is evidence for review, not proof that the service belongs to the old AI stack.

The backup manifest records file-level SHA-256 checksums, source-to-archive mappings, whether sensitive data is present, and the invariant `old_stack_deletion_allowed=false`. This script never stops, modifies, deletes, or marks old resources as safe to remove.

## Open WebUI Preservation Plan

Before cutover, generate an Open WebUI-specific preservation plan from the reviewed inventory and verified old-stack backup:

```bash
make open-webui-migration-plan \
  INVENTORY=/srv/b1-ai-hub/backups/inventory-20260722-120000.json \
  BACKUP=/srv/b1-ai-hub/backups/old-stack-20260722-120000
```

This writes `$B1_BACKUP_ROOT/open-webui-migration-plan.json` in the `b1-ai-hub-open-webui-migration-plan/v1` format. The planner verifies the old-stack backup, correlates read-only Open WebUI SQLite database candidates from the inventory with files actually preserved in the backup, records Open WebUI container image/tag evidence from the inventory, and records a recommended strategy. It does not read chat rows, prompts, uploaded documents, or settings values; it uses only the inventory's table/count metadata, inventory image metadata, and the backup manifest.

If a readable Open WebUI database is not covered by the verified old-stack backup, the plan records a warning and recommends backing up Open WebUI before cutover. If the inventory discovered an Open WebUI Docker volume root but could not scan it, the plan records that unreadable root and warns that the operator must include the corresponding Docker volume in the reviewed old-stack backup or rerun inventory with read access. If the inventory cannot identify the old Open WebUI image/version, or if the source image uses a floating tag such as `latest`, the plan records a source-version warning. If the database is backed up, the plan still does not generate an automatic import or approve direct database reuse. Restore the backup to an alternate directory, start B1 on temporary ports, and test a supported Open WebUI migration/export/import path before pointing production Open WebUI at any preserved data. Keep the old database and volumes recoverable even after successful cutover.

## Cutover Plan

After the old-stack backup verifies, generate the non-destructive cutover and rollback plan:

```bash
make cutover-plan \
  INVENTORY=/srv/b1-ai-hub/backups/inventory-20260722-120000.json \
  SCOPE=/srv/b1-ai-hub/backups/old-stack-scope.json \
  BACKUP=/srv/b1-ai-hub/backups/old-stack-20260722-120000 \
  OPEN_WEBUI_PLAN=/srv/b1-ai-hub/backups/open-webui-migration-plan.json
```

This writes `$B1_BACKUP_ROOT/cutover-plan.json` in the `b1-ai-hub-cutover-plan/v1` format. The planner verifies the old-stack backup before writing the plan, refuses scoped containers that the inventory classified as `b1-ai-hub-current-preserve` or `preserve-unrelated`, refuses to write a staging plan when the selected temporary HTTP/HTTPS ports are already listening, validates that the Open WebUI preservation plan was generated from the same inventory and backup, carries hardware readiness, GPU container runtime readiness, and runtime-agent Docker socket readiness from the same inventory, and records warnings for explicitly scoped containers that were not present in the inventory or were `unknown-preserve-by-default`.

The cutover plan is a reviewed runbook, not an executor. It includes commands to start B1 AI Hub on temporary ports, suggested validation commands, exact `docker stop` / rollback `docker start` commands for scoped old-stack containers, exact `systemctl stop` / rollback `systemctl start` commands for scoped old-stack systemd services, `hardware_readiness`, `gpu_runtime_readiness`, `runtime_agent_socket_readiness`, `port_readiness`, `dns_readiness`, and `open_webui_preservation` sections, and a production `docker compose up -d` command. If production ports `80` or `443` or the optional legacy ComfyUI port `8188` are already listening, the plan records warnings instead of assuming those listeners belong to the old AI stack. If any core B1 virtual host is missing from the inventory DNS records, or if the core virtual hosts do not share a common gateway address, the plan records DNS warnings and leaves route changes as explicit operator actions. Optional profile hosts are reported separately under `dns_readiness.optional_*`; a missing optional monitoring hostname is acceptable unless the monitoring profile is enabled, while an optional hostname that resolves to a different address is flagged for review before that profile is used. If the inventory reports less than the initial 12 GB VRAM / 32 GB RAM profile, the plan records hardware warnings and operators must either change hardware or explicitly validate a reduced-resource policy before cutover. If the inventory cannot prove `nvidia-smi`, Docker's `nvidia` runtime, and `nvidia-ctk` are healthy, the plan records GPU runtime warnings because production LocalAI, ComfyUI, and Voicebox overlays depend on NVIDIA Container Toolkit. If the inventory reports missing Docker socket access or a `B1_DOCKER_GID` mismatch, the plan records runtime-agent socket warnings because service inventory, bounded logs, unload/recovery, and controlled rollback depend on that access. If the Open WebUI preservation plan contains warnings, lacks source-version evidence, uses a floating source image tag, or uses a strategy that requires manual export/import, the cutover plan carries those warnings forward. Operators must still enter maintenance mode, drain work, validate temporary B1 services, resolve production port listeners, apply DNS or reverse-proxy changes, and run the listed commands manually during the cutover window.

Cutover rules:

1. Back up old Compose files, environment, volumes, Open WebUI DB, models, and relevant configuration.
2. Start B1 AI Hub on temporary hostnames or ports.
3. Avoid running two GPU inference stacks concurrently.
4. Validate chat, API, TTS, image, video, ComfyUI compatibility, Model Hub, and external integrations.
5. Stop only identified old-stack containers and host services during the cutover window.
6. Retain old volumes and data stopped/read-only for rollback.

No script may delete the old stack automatically after migration.
