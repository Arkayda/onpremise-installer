# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

The **Compass On-Premise Installer** — a Python 3.8+ toolkit that deploys the Compass messenger (self-hosted team collaboration platform) on a single server or cluster via Docker Swarm. The installer runs on the target server: it renders Go templates into Docker Compose files and service configs, then drives `docker stack deploy`.

**Supported OSes:** Ubuntu 20.04+, Debian 10+, Fedora 36+, AlmaLinux 9.6+, РЕД ОС 8+, МСВСфера 9.6+, ALT Linux 11.0+

**Constraints:**
- All `script/*.py` entry points require root (`scriptutils.assert_root()`); use `sudo`.
- Template rendering works only on Linux: `script/template.py` picks `packages/go_template_linux` by `sys.platform` (a darwin branch exists but `packages/go_template_darwin` is absent from the repo).
- There is no Python test suite or linter. The web frontend has ESLint (`pnpm lint`).
- `packages/yq_linux_amd64` is not invoked by any Python script (only `src/search/_manual/manticore.sh` calls `yq` from PATH).

## Common Commands

### Installation lifecycle (run in this order)

```bash
sudo python3 init-install/init.py        # prepare server: Docker, firewall, limits, Node.js
sudo python3 script/create_configs.py    # copy yaml_template/configs/*.tpl.yaml -> configs/*.yaml (refuses to overwrite existing)
# ... user edits configs/*.yaml (domain, database, auth, team, etc.)
sudo python3 script/init.py              # interactive reconciler -> writes src/values.compass.yaml
sudo python3 script/install.py -e production --confirm-all
```

### Install / deploy / update

```bash
# Main installation (validates, generates secrets/certs, deploys, waits for health)
sudo python3 script/install.py -e production --confirm-all

# Validate only — read-only, prints JSON list of invalid config keys
sudo python3 script/install.py -e production --validate-only --installer-output

# Extra data / integrations
sudo python3 script/install.py -e production --data '{"product_type":"dev"}' --install-integration

# Deploy one project. -v is the values NAME (resolves to src/values.compass.yaml), not a filename
sudo python3 script/deploy.py -e production -p monolith -v compass
sudo python3 script/deploy.py -e production -p api_gateway -v compass --data '{"gateway_id":"gateway-1"}' --dry

# Update an existing installation
sudo python3 script/update.py -e production --docker-prune
sudo python3 script/update.py -e production --is-restore-db 1   # restore-from-backup mode: skips migrations
```

`deploy.py --dry` renders everything into `script/tmp/` and exits before `docker stack deploy` — the primary way to inspect generated output.

### Management scripts (script/)

`create_root_user.py`, `create_team.py`, `delete_team.py`, `backup_all_data.py`, `restore_db.py`, `uninstall.py`, `get_users.py`, plus one `generate_*_configuration.py` per config area (captcha, sms, auth_data, restrictions, preview, smart_apps). `script/replication/` holds MySQL/Manticore replication management scripts (master/reserve cluster setup, health checks, zabbix hooks).

### Web installer

```bash
# Backend — FastAPI + uvicorn (NOT Flask)
cd web_installer/backend
pip3 install -r requirements.txt
python3 app.py            # serves API + built SPA

# Frontend — React 19 + Vite + Tailwind 4 + shadcn/ui, pnpm
cd web_installer/frontend
pnpm install
pnpm dev                  # development server
pnpm build                # tsc -b && vite build
pnpm lint
```

## Architecture

### Configuration pipeline (the core flow)

```
yaml_template/configs/*.tpl.yaml          user-editable templates
        │  create_configs.py
        ▼
configs/*.yaml                            USER EDITS THESE (never regenerated in place)
        │  script/init.py (interactive/validate; secrets preserved in configs/global.protected.yaml)
        ▼
src/values.<name>.yaml                    resolved values written by init.py
        │  deploy.py merges: src/values.yaml (defaults) + values.<name>.yaml + {root_mount_path}/security.yaml
        ▼
go_template_linux renders .goyaml/.goenv  (script/template.py)
        ▼
docker stack deploy                       3 compose files from tmp/
```

Key points:

- **Values resolution chain** (same in install.py/deploy.py/update.py): `src/values.<environment>.<values>.yaml` → `src/values.<values>.<product_type>.yaml` → `src/values.<values>.yaml` → `src/values.yaml`.
- **Stack name** = `{environment}-{values}-{project-label}`, e.g. `production-compass-monolith` (suffix `-<service_label>` when replication labels are used).
- **Three compose files** are always rendered: `compose.goyaml` (base), `compose.override.<environment>.goyaml`, and `compose.sidecar.<environment>.goyaml` (stub if absent) — deployed together with `docker stack deploy --with-registry-auth --prune`.
- **Variable files**: `src/<project>/variable/*.goenv` are the sources; any `*.txt`/generated files next to them are render output — don't hand-edit. Rendered env files pass through `deploy_prepare_env.py` (dotenv → docker env-file escaping); rendered configs get an md5 stamped into `override_data["config_revisions.<name>"]` so services can detect config drift.
- **Secrets/certs** are generated by dedicated scripts that install.py/update.py invoke (`generate_security_keys.py` → `{root_mount_path}/security.yaml`, `generate_ssl_certificates.py` → `certs/`, `generate_mysql_ssl_certificates.py`) — `init.py` itself only reconciles config values.
- **Install progress** is tracked in `.install_completed_steps.json` at repo root (read by the web installer for step-by-step status).

### Monolith and deploy units

`monolith` is not a service but an orchestration unit: one stack that contains most services. `projects.monolith.deploy_units` in `src/values.yaml` lists them (kafka, pivot, domino, file, auth, api_gateway, search, federation, integration, jitsi, join_web, userbot, announcement, license, outlook_add_in …). Before deploy, the trigger `triggers/prepare_deploy_symbolic_links.py` creates symlinks `src/monolith/{config,variable}/<deploy_unit>` → `src/<deploy_unit>/{config,variable}` so a single compose render picks up every unit's variables and configs; `delete_deploy_symbolic_links.py` cleans up afterwards.

Services deployed outside monolith stacks: `monitoring` (Grafana/Prometheus/Loki), `integration` (optional), replication reserves.

### Triggers system

Lifecycle hooks run by `deploy.py` via `script/trigger.py -t <before|after|finally>`. Configured in `src/values.yaml` both globally (`triggers.before: [triggers/check_security.py]`) and per project (`projects.<project>.triggers`), global first then project. `finally` runs from an `atexit`/SIGINT cleanup handler. Existing triggers: cert/key validation (`check_security.py`), nginx cert config generation, and the monolith symlink pair.

### Update mechanism

Repo root `.version` holds the applied installer version. `update.py` runs `script/installer_migrations_up.py`, which walks `updates/<version>/` in sorted order, skipping versions `<= .version`. Each `updates/<version>/migration.yaml` has `migration_commands` (Python snippets, `exec()`'d) and `migration_scripts` (run as subprocesses with `-e ENV -v VALUES`); the highest applied version is written back to `.version`. Migration scripts typically patch `configs/global.yaml`. `update.py` also handles service_label changes (deleting old stacks), pre/post-deploy company DB migrations, and replication repair.

### Environments

`-e/--environment` (default `production`) affects: stack-name prefix, values file selection (`values.<environment>.<values>.yaml` wins), and which `compose.override.<environment>.goyaml` / `compose.sidecar.<environment>.goyaml` are used. `local` and `tes` are special-cased in deploy.py (skip init re-run and cert copying). `dev` flips `server_type`/tags in init.py.

### Web installer (web_installer/)

FastAPI backend (`backend/app.py`) that wraps the CLI scripts via subprocess: `/api/install/configure` writes `configs/*.yaml` (+ acme.sh Let's Encrypt flow), `/api/install/validate` runs `install.py --validate-only --installer-output`, `/api/install/run` spawns a background job with streamed logs, `/api/install/status/{id}` reads `.install_completed_steps.json`. Frontend is React 19 + jotai + shadcn/ui.

## Working in This Codebase

### Adding a new service

1. Create `src/<service>/` with `compose.goyaml`, `compose.override.<environment>.goyaml`, `variable/*.goenv`, `config/`.
2. Add a `projects.<service>` block to `src/values.yaml` (label, network, triggers, service settings).
3. To fold it into the monolith stack, add it to `projects.monolith.deploy_units`.
4. If users must configure it, add `yaml_template/configs/<service>.tpl.yaml` and handling in `script/init.py`.

### Conventions

- User-editable settings → `yaml_template/configs/*.tpl.yaml`; deployment defaults → `src/values.yaml`; per-service templates → `src/<service>/compose.goyaml` + `variable/`.
- Scripts set `sys.dont_write_bytecode = True`; shared helpers live in `script/utils/scriptutils.py` (colors/confirm/OS detection/replication checks) and `script/utils/interactive.py` (init.py prompts).
- Service selection everywhere uses config **keys** (e.g. `domino_id`, `file_node_id`), not `label` values.
- Generated/local artifacts are gitignored: `configs/`, `certs/`, `src/security.yaml`, `src/values.*.yaml`, `src/nginx/compose*.yaml`, `.version`, `.service_label`, `.install_completed_steps.json`.

### Debugging

- `deploy.py --dry` — render to `script/tmp/` and stop.
- `install.py --validate-only --installer-output` — machine-readable list of invalid config keys.
- `docker service logs <stack>_<service>` / `docker stack ps <stack>` for runtime state.
