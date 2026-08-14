# DeepSeek quality Phase-1 rollback

Worker backups are in:

`/srv/b1-p40-worker/backups/deepseek-quality-phase1-20260813T2230Z`

To roll back the worker, first let any active B1 request finish. Restore only
the three backed-up managed-worker files, verify `SHA256SUMS`, and recreate only
the DeepSeek router:

```bash
sudo install -o root -g root -m 0444 BACKUP/deepseek-profiles.json /srv/b1-p40-worker/data/deepseek-profiles.json
sudo install -o root -g root -m 0555 BACKUP/deepseek_manager.py /opt/b1-p40-worker/deploy/deepseek-v4/deepseek_manager.py
sudo install -o root -g root -m 0644 BACKUP/compose.yaml /opt/b1-p40-worker/compose.yaml
cd /opt/b1-p40-worker
sudo docker compose up -d --no-deps --force-recreate deepseek-router
```

Replace `BACKUP` with the absolute directory above. This restores the prior
quality budget of 512 and prior router image without touching the GGUFs,
placement, Laguna, LocalAI, Caddy, or other services.

For B1, revert the Phase-1 commit in `localAIcentre`, rebuild the existing
control-plane image, and recreate only `control-plane`. Do not reset the branch
or discard unrelated working-tree files. Verify afterward that an out-of-range
quality budget follows the old behavior and that `deepseek-main`,
`chat-default`, and Laguna aliases still resolve to their original models.
