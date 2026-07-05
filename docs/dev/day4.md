# Day 4 — Production Hardening: Compose Fixes, Monitoring, Backups, Key Management

**Who this is for:** the same developer who completed Days 1–3. Day 3 concepts (Compose, UFW, vLLM FP8, LiteLLM virtual keys, Tailscale Funnel) are assumed. New concepts introduced today are explained on first use.

**Day 4 goal in one sentence:** close every open item left by the Day 3 fork, then apply the full production hardening layer — compose file correctness, Funnel persistence, monitoring with real GPU data, automated backups, host hardening, and production virtual key issuance.

**What Day 3 left you with (read the fork doc first):**

| Item | State |
|---|---|
| vLLM FP8 on RTX 5090 | ✅ Working |
| Chat completions, streaming + non-streaming, external | ✅ Confirmed |
| Embeddings via LiteLLM, external | ✅ Confirmed |
| Tailscale Funnel HTTPS on `https://ubuntu.tailcd8da4.ts.net` | ✅ Working |
| UFW active (SSH + 80 + 443 + tailscale0) | ✅ Active, needs audit |
| Reranker (`tei-rerank`) end-to-end verified | ❌ Unconfirmed — ONNX ambiguity |
| Qdrant collection initialized on prod | ❌ Not done |
| `restart: unless-stopped` on TEI services | ❌ Missing |
| TEI ports loopback-restricted | ❌ Currently `0.0.0.0`-bound |
| `CUDA_VISIBLE_DEVICES` pinned | ❌ Not hardened |
| Qdrant image pinned | ❌ On `latest` |
| Caddy container stopped | ❌ Running idle |
| Funnel persistence across reboots | ❌ Not automated |
| Monitoring stack | ❌ Not started |
| Backups | ❌ Not started |
| Production virtual keys | ❌ Not issued |
| `.env.example` updated | ❌ Outdated |

**Day 4 sequencing:** the first half of the day (Steps 1–5) closes Day 3's open items and hardens the Compose file. These are pre-conditions for everything else — monitoring and backups work against a correctly configured stack. The second half (Steps 6–10) adds the new production-grade layer. Step 11 is production key issuance.

> **A note on Caddy vs. Retire:** the fork doc left this as an open decision. **This guide takes Option A — retire Caddy.** Tailscale Funnel is the confirmed production ingress; Caddy adds complexity for no current benefit. If rate limiting becomes a requirement later, it can be added as a Funnel-side or LiteLLM middleware layer. If you prefer Option B (Caddy as internal rate limiter), skip Step 1c and instead update the Caddyfile to listen on an internal port, then add `FUNNEL_TARGET_PORT` to point Funnel at Caddy instead of LiteLLM directly. This guide does not cover that path.

---

## 0. New concepts for Day 4

- **Prometheus:** an open-source metrics collection system. It scrapes numeric time-series data (request counts, latency, GPU memory, error rates) from HTTP endpoints on each service at a regular interval (every 15s by default), and stores it locally.
- **Grafana:** a dashboarding tool that reads from Prometheus and renders the data as graphs and alerts. Grafana does not collect data itself — it visualizes what Prometheus collected.
- **DCGM Exporter:** NVIDIA's official Prometheus exporter for GPU metrics. Runs as a container with access to the NVIDIA driver, exposes VRAM usage, GPU utilization, temperature, and power draw in Prometheus format. Requires the NVIDIA Container Toolkit (already installed).
- **Loki:** a log aggregation system from Grafana Labs. Rather than storing full log text in a database (like Elasticsearch), Loki stores only labels and indexes, with the raw log lines kept in cheap object/disk storage. Designed to work alongside Prometheus, not replace it.
- **Promtail:** a log-shipping agent. Runs on the same host as the Docker containers, tails the container log files (`/var/lib/docker/containers/...`), and forwards them to Loki with service-name labels.
- **Grafana alert:** a rule evaluated on a Prometheus query at an interval. When the query crosses a threshold (e.g., VRAM > 90%), Grafana fires a notification to a configured contact point (email, Slack, webhook, etc.).
- **`fail2ban`:** a host-level intrusion prevention tool. It watches log files (typically `/var/log/auth.log` for SSH) and automatically adds firewall rules to block IPs that trigger too many failed login attempts. Reduces brute-force SSH exposure.
- **`unattended-upgrades`:** Ubuntu's daemon for automatic security patch installation. It applies OS security updates without requiring manual `apt upgrade` runs. Keeps the host patched against kernel and system library CVEs.
- **`pg_dump`:** PostgreSQL's built-in tool for creating a portable SQL backup of a database. We use it to snapshot the LiteLLM Postgres database (which contains all virtual keys, budgets, and usage logs).
- **Restic:** a fast, encrypted, deduplicated backup tool. Supports local, SFTP, S3, B2, and other targets. Runs as a single binary with no daemon required.
- **systemd service / cron `@reboot`:** two ways to run a command automatically when the machine boots. `@reboot` in cron is simpler for a one-liner; a systemd service file gives more control over ordering and restart behavior.
- **LiteLLM virtual key (production):** a `sk-...` credential scoped to specific model aliases (`qfind-chat`, `qfind-embed`, `qfind-rerank`), with a hard `max_budget`, per-minute request and token caps (`rpm_limit`, `tpm_limit`), a `budget_duration`, and a `user_id` identifying who or what system holds it. Virtual keys are stored in Postgres (encrypted by `LITELLM_SALT_KEY`). Revoking one is instant and does not affect any other key.

---

## 1. Close Day 3 open items — Compose, firewall, reranker, Funnel persistence

Work through these in order. Each one is a pre-condition for the steps that follow.

### 1a. Audit host ports before touching anything

```bash
ss -tlnp
```

Confirm no unexpected host-level processes are bound to any port. Specifically verify:
- Nothing is bound to `0.0.0.0:8000`, `0.0.0.0:4000`, `0.0.0.0:6333` (those must be Docker-internal or loopback only).
- Port `4000` is bound on `127.0.0.1` (Docker published) — this is what Funnel reaches.

If you see any `0.0.0.0:<port>` entries for service ports, investigate before proceeding. This was how the stray `ChatbotAPI` process was discovered on Day 3.

### 1b. Verify and fix the reranker

The ONNX vs. safetensors ambiguity from Day 3 must be resolved before anything else depends on the reranker. Test it directly:

```bash
cd ~/litellm-deploy/deployment
curl -s http://localhost:8002/rerank \
  -H 'Content-Type: application/json' \
  -d '{"query":"file search","texts":["a tool that searches files","a recipe for soup"]}' \
  | python3 -m json.tool
```

**Two outcomes:**

**A — Returns a scored JSON array** (e.g., `[{"index":0,"score":0.92},{"index":1,"score":0.03}]`):
The CPU image successfully loaded the model from the ONNX directory. Proceed. Note which model format worked for the runbook.

**B — Returns an error or the container is stopped:**
The ONNX-only directory did not work with TEI's Candle backend. Download the raw safetensors variant:
```bash
pip3 install -U "huggingface_hub[cli]"
huggingface-cli download BAAI/bge-reranker-v2-m3 \
  --local-dir model-cache/bge-reranker-v2-m3
```
Then update `docker-compose.prod.yml` to point `tei-rerank` at the new directory:
```yaml
# in tei-rerank volumes:
- ./model-cache/bge-reranker-v2-m3:/models/bge-reranker-v2-m3:ro
```
Restart and retest:
```bash
docker compose -f docker-compose.prod.yml up -d --force-recreate tei-rerank
docker compose -f docker-compose.prod.yml logs -f tei-rerank   # wait for "Ready"
curl -s http://localhost:8002/rerank \
  -H 'Content-Type: application/json' \
  -d '{"query":"file search","texts":["a tool that searches files","a recipe for soup"]}' \
  | python3 -m json.tool
```

### Check
- `curl` to port 8002 returns a JSON array with `index` and `score` fields.
- No error in `docker compose logs tei-rerank`.


### 1c. Stop and retire Caddy

```bash
cd ~/litellm-deploy/deployment
docker compose -f docker-compose.prod.yml stop caddy
```

Confirm it stopped:
```bash
docker compose -f docker-compose.prod.yml ps caddy
# → shows "Exited" or "stopped"
```

Confirm Funnel still works (Caddy is not in the path):
```bash
curl -s https://ubuntu.tailcd8da4.ts.net/v1/models \
  -H "Authorization: Bearer $(grep LITELLM_MASTER_KEY .env | cut -d= -f2)"
# → returns JSON with model list
```

### 1d. Update `docker-compose.prod.yml` — all structural fixes in one edit

Open `deployment/docker-compose.prod.yml` and apply all four structural fixes together:

1. **Remove the `caddy` service entirely** (or comment it out with a note).
2. **Add `restart: unless-stopped`** to `tei-embed` and `tei-rerank`.
3. **Add loopback bind** to TEI ports: `"8001:80"` → `"127.0.0.1:8001:80"`, same for 8002.
4. **Add `CUDA_VISIBLE_DEVICES` pinning** to `vllm-chat`.
5. **Pin Qdrant image** from `latest` to the version currently running.

First, check the running Qdrant version:
```bash
docker inspect $(docker compose -f docker-compose.prod.yml ps -q qdrant) \
  | python3 -c "import sys,json; d=json.load(sys.stdin); print(d[0]['Config']['Image'])"
# → e.g. qdrant/qdrant:v1.13.6
```

Then confirm the correct GPU device index for `CUDA_VISIBLE_DEVICES`:
```bash
nvidia-smi -L
# Output example:
# GPU 0: NVIDIA RTX A400 (UUID: ...)
# GPU 1: NVIDIA GeForce RTX 5090 (UUID: ...)
```
The RTX 5090 is at index 1 in this example. Cross-check with `lspci`:
```bash
lspci | grep -i nvidia
# 01:00.0 = A400, 09:00.0 = RTX 5090
# PCI_BUS_ID order: A400 first (lower bus), 5090 second → index 1
```

The final `docker-compose.prod.yml` after all fixes:

```yaml
# Production Compose — RTX 5090 server
# Ingress: Tailscale Funnel → LiteLLM:4000 (Caddy retired; Funnel handles TLS)
# Usage: docker compose -f docker-compose.prod.yml up -d

services:

  # ─── Databases ──────────────────────────────────────────────────────────────

  litellm-db:
    image: postgres:16
    restart: unless-stopped
    environment:
      POSTGRES_USER: ${POSTGRES_USER}
      POSTGRES_PASSWORD: ${POSTGRES_PASSWORD}
      POSTGRES_DB: ${POSTGRES_DB}
    volumes:
      - pgdata:/var/lib/postgresql/data
    networks: [qfind-net]

  litellm-redis:
    image: redis:7
    restart: unless-stopped
    networks: [qfind-net]

  # ─── Inference engines ──────────────────────────────────────────────────────

  vllm-chat:
    image: vllm/vllm-openai:v0.17.0
    restart: unless-stopped
    environment:
      # Explicit GPU selection: A400=0, RTX 5090=1 (PCI_BUS_ID order)
      # Prevents vLLM from binding to the wrong GPU after a driver update/reboot
      CUDA_DEVICE_ORDER: PCI_BUS_ID
      CUDA_VISIBLE_DEVICES: "1"
    command: >
      --model /models/qwen2.5-14b-instruct
      --quantization fp8
      --served-model-name qfind-chat
      --gpu-memory-utilization 0.70
      --max-model-len 16384
      --tensor-parallel-size 1
      --enable-prefix-caching
    volumes:
      - ./model-cache/qwen2.5-14b-instruct:/models/qwen2.5-14b-instruct:ro
    ports:
      - "127.0.0.1:8000:8000"
    deploy:
      resources:
        reservations:
          devices:
            - { driver: nvidia, count: all, capabilities: [gpu] }
    networks: [qfind-net]

  tei-embed:
    image: ghcr.io/huggingface/text-embeddings-inference:cpu-1.5
    restart: unless-stopped
    command: ["--model-id", "/models/bge-m3"]
    volumes:
      - ./model-cache/bge-m3:/models/bge-m3:ro
    ports:
      - "127.0.0.1:8001:80"    # loopback-only; was 0.0.0.0 in Day 3
    networks: [qfind-net]

  tei-rerank:
    image: ghcr.io/huggingface/text-embeddings-inference:cpu-1.5
    restart: unless-stopped
    command: ["--model-id", "/models/bge-reranker-v2-m3"]
    volumes:
      # Use whichever directory was confirmed working in Step 1b
      - ./model-cache/bge-reranker-v2-m3-onnx:/models/bge-reranker-v2-m3:ro
    ports:
      - "127.0.0.1:8002:80"    # loopback-only; was 0.0.0.0 in Day 3
    networks: [qfind-net]

  # ─── Gateway ────────────────────────────────────────────────────────────────

  litellm:
    image: ghcr.io/berriai/litellm:main-v1.40.0
    restart: unless-stopped
    command: ["--config", "/app/config.yaml", "--port", "4000"]
    volumes:
      - ./litellm/config.yaml:/app/config.yaml:ro
    ports:
      - "127.0.0.1:4000:4000"
    env_file: [.env]
    depends_on:
      - litellm-db
      - litellm-redis
    networks: [qfind-net]

  # ─── Vector store ───────────────────────────────────────────────────────────

  qdrant:
    image: qdrant/qdrant:v1.13.6   # pin to the version confirmed running; update if different
    restart: unless-stopped
    volumes:
      - qdrant-data:/qdrant/storage
    ports:
      - "127.0.0.1:6333:6333"
      - "127.0.0.1:6334:6334"
    networks: [qfind-net]

  # ─── Monitoring ─────────────────────────────────────────────────────────────
  # (added in Day 4 Step 6 — placeholder services will be appended there)

networks:
  qfind-net:
    external: false

volumes:
  pgdata:
  qdrant-data:
  # caddy-data and caddy-config removed — Funnel handles TLS, no local certs
```

> **Save this file.** Do not bring the stack down to apply it yet — the TEI services need a rolling restart, not a full stack cycle.

Apply TEI changes (restart only the affected services):
```bash
docker compose -f docker-compose.prod.yml up -d --force-recreate tei-embed tei-rerank
```

Apply vLLM GPU pinning (requires a container restart, which takes 3–5 minutes for the 14B model to reload):
```bash
docker compose -f docker-compose.prod.yml up -d --force-recreate vllm-chat
docker compose -f docker-compose.prod.yml logs -f vllm-chat   # wait for "Application startup complete"
```

Verify vLLM reloaded on the correct GPU:
```bash
nvidia-smi
# VRAM usage should show ~14-16 GB on GPU 1 (RTX 5090), not GPU 0 (A400)
```

Also confirm no driver-order warning in the vLLM logs:
```bash
docker compose -f docker-compose.prod.yml logs vllm-chat | grep -i "device\|gpu\|warning" | head -20
```

### Check — Step 1
- `ss -tlnp` shows no unexpected public-facing service ports.
- Reranker returns scored JSON from port 8002.
- `docker compose ps` shows caddy as stopped or absent.
- Funnel still reachable: `curl https://ubuntu.tailcd8da4.ts.net/v1/models` returns model list.
- `nvidia-smi` shows VRAM on GPU 1 (RTX 5090), not GPU 0 (A400).
- All services (excluding caddy) are `running`.


---

## 2. Audit and tighten UFW

With Caddy retired, ports 80 and 443 no longer serve any purpose. Close them.

### 2a. Check current state

```bash
sudo ufw status verbose
```

Expected current state (from Day 3):
```
Status: active

To                         Action      From
--                         ------      ----
22/tcp                     ALLOW IN    Anywhere
80/tcp                     ALLOW IN    Anywhere
443/tcp                    ALLOW IN    Anywhere
Anywhere on tailscale0     ALLOW IN    Anywhere
```

### 2b. Remove the now-unnecessary rules

```bash
sudo ufw delete allow 80/tcp
sudo ufw delete allow 443/tcp
```

> Port 80 was only needed for Caddy's Let's Encrypt HTTP-01 challenge. Port 443 was only needed for Caddy to receive HTTPS. Both are now handled by Tailscale's infrastructure, not this host. Neither port needs to be open on UFW.

### 2c. Verify the final firewall state

```bash
sudo ufw status verbose
```

Expected after cleanup:
```
Status: active

To                         Action      From
--                         ------      ----
22/tcp                     ALLOW IN    Anywhere
Anywhere on tailscale0     ALLOW IN    Anywhere
```

> **SSH hardening (if not done on Day 3):** if port 22 is open to the whole internet, consider restricting it to known source IPs or moving it to a non-standard port. At minimum, confirm password authentication is disabled:
> ```bash
> sudo grep -E "^PasswordAuthentication|^PubkeyAuthentication" /etc/ssh/sshd_config
> # Should show: PasswordAuthentication no
> #              PubkeyAuthentication yes
> ```
> If `PasswordAuthentication` is not `no`, set it:
> ```bash
> sudo sed -i 's/^#\?PasswordAuthentication.*/PasswordAuthentication no/' /etc/ssh/sshd_config
> sudo systemctl reload sshd
> ```

### Check
- `sudo ufw status verbose` shows only SSH (22) and `tailscale0` open.
- `curl https://ubuntu.tailcd8da4.ts.net/v1/models` still works (Funnel traffic goes through `tailscale0`, not port 443).

---

## 3. Initialize the Qdrant collection

The Qdrant container on prod has a fresh, empty volume from Day 3. The `init_collection.py` script was never run against it.

```bash
cd ~/litellm-deploy/deployment

# Install qdrant-client if not already present in this environment
pip3 install qdrant-client

# Run the collection initializer
python3 qdrant/init_collection.py
```

Expected output:
```
Collection 'qfind_docs' created.
Collections: ['qfind_docs']
```

Verify via the HTTP API:
```bash
curl -s http://localhost:6333/collections/qfind_docs | python3 -m json.tool
```
→ Returns JSON with `"status": "green"` and vector config showing 1024-dimensional dense + sparse vectors.

If the collection already exists (script is idempotent — it checks before creating):
```
Collection 'qfind_docs' already exists — skipping creation.
Collections: ['qfind_docs']
```
That's correct. Proceed.

### Check
- `curl http://localhost:6333/collections/qfind_docs` returns `"status": "green"`.

---

## 4. Automate Funnel persistence across reboots

Currently `tailscale funnel --bg 4000` was run manually on Day 3. A server reboot would bring up all Docker services (via `restart: unless-stopped`) but leave Funnel inactive — the stack would be running but externally unreachable.

### 4a. Verify Funnel is currently active

```bash
tailscale funnel status
```

Expected output:
```
https://ubuntu.tailcd8da4.ts.net (Funnel on)
|-- / proxy http://127.0.0.1:4000
```

If it shows "No serve config", restart it:
```bash
tailscale funnel --bg 4000
```

### 4b. Create a systemd service for Funnel

A systemd service is more robust than a cron `@reboot` entry — it handles start ordering, restart-on-failure, and logging through `journalctl`.

Create the service file:
```bash
sudo tee /etc/systemd/system/tailscale-funnel.service > /dev/null << 'EOF'
[Unit]
Description=Tailscale Funnel — expose LiteLLM port 4000 publicly
After=network-online.target tailscaled.service
Wants=network-online.target

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/usr/bin/tailscale funnel --bg 4000
ExecStop=/usr/bin/tailscale funnel --bg --off 4000
Restart=on-failure

[Install]
WantedBy=multi-user.target
EOF
```

Enable and start it:
```bash
sudo systemctl daemon-reload
sudo systemctl enable tailscale-funnel.service
sudo systemctl start tailscale-funnel.service
```

Verify it's active:
```bash
sudo systemctl status tailscale-funnel.service
# → "active (exited)" is correct for Type=oneshot; this means the command ran successfully
tailscale funnel status
# → should still show Funnel active
```

### 4c. Test reboot persistence (optional but recommended)

If you have a maintenance window, test it:
```bash
sudo reboot
# ... reconnect via SSH after ~60 seconds ...
tailscale funnel status      # should show Funnel active
docker compose -f ~/litellm-deploy/deployment/docker-compose.prod.yml ps   # all services running
curl https://ubuntu.tailcd8da4.ts.net/v1/models \
  -H "Authorization: Bearer $(grep LITELLM_MASTER_KEY ~/litellm-deploy/deployment/.env | cut -d= -f2)"
# → model list JSON
```

If you skip the reboot test, confirm the service is enabled for next boot at minimum:
```bash
sudo systemctl is-enabled tailscale-funnel.service
# → "enabled"
```

### Check
- `tailscale funnel status` shows Funnel active.
- `sudo systemctl is-enabled tailscale-funnel.service` returns `enabled`.
- External curl to Funnel URL returns the model list.

---

## 5. Update `.env.example`

The current `.env.example` is out of date (has `LITELLM_VIRTUAL_KEY`, missing `LITELLM_SALT_KEY`). Fix it now so the repo is accurate before any commits today.

Replace `deployment/.env.example` with:

```dotenv
# ─── Postgres ────────────────────────────────────────────────────────────────
POSTGRES_USER=litellm
POSTGRES_PASSWORD=change-me          # generate: openssl rand -base64 32
POSTGRES_DB=litellm

# ─── LiteLLM ─────────────────────────────────────────────────────────────────
DATABASE_URL=postgresql://litellm:change-me@litellm-db:5432/litellm

# Master key — admin only, never share, never commit the real value
LITELLM_MASTER_KEY=change-me         # generate: echo "sk-master-$(openssl rand -hex 32)"

# Salt key — encrypts virtual key data stored in Postgres.
# CRITICAL: generate once, never change after first run (invalidates all stored keys).
LITELLM_SALT_KEY=change-me           # generate: openssl rand -base64 32
```

Commit it:
```bash
cd ~/litellm-deploy
git add deployment/.env.example
git commit -m "fix: update .env.example — add LITELLM_SALT_KEY, remove LITELLM_VIRTUAL_KEY"
```

### Check
- `cat deployment/.env.example` shows exactly the four variables above — no `LITELLM_VIRTUAL_KEY`, no stale placeholders.
- `git log --oneline -1` shows the commit.


---

## 6. Host hardening — `fail2ban` + automatic security updates

These two steps are quick to apply and protect the host against the most common passive threats: brute-force SSH and unpatched CVEs.

### 6a. Enable automatic security updates

```bash
sudo apt-get install -y unattended-upgrades
sudo dpkg-reconfigure --priority=low unattended-upgrades
# → answer "Yes" when prompted
```

Confirm it's active:
```bash
sudo systemctl is-active unattended-upgrades
# → active
cat /etc/apt/apt.conf.d/20auto-upgrades
# Should contain:
# APT::Periodic::Update-Package-Lists "1";
# APT::Periodic::Unattended-Upgrade "1";
```

> This only installs security patches automatically, not all upgrades. Docker image upgrades and OS major upgrades remain manual — as they should be on a production inference server.

### 6b. Install and configure `fail2ban`

```bash
sudo apt-get install -y fail2ban
```

Create a local override (never edit the default `jail.conf` — it gets overwritten on upgrades):
```bash
sudo tee /etc/fail2ban/jail.local > /dev/null << 'EOF'
[DEFAULT]
bantime  = 1h
findtime = 10m
maxretry = 5

[sshd]
enabled = true
port    = ssh
logpath = %(sshd_log)s
backend = %(syslog_backend)s
EOF
```

Enable and start:
```bash
sudo systemctl enable fail2ban
sudo systemctl start fail2ban
```

Verify it's watching SSH:
```bash
sudo fail2ban-client status sshd
# → Shows "Currently banned: 0" and "Total banned: 0" on a clean server
```

### Check
- `sudo systemctl is-active unattended-upgrades` returns `active`.
- `sudo fail2ban-client status sshd` returns without error.

---

## 7. Set up the monitoring stack

The monitoring stack adds four containers to the Compose file: Prometheus (metrics collection), DCGM Exporter (GPU metrics), Loki (log aggregation), and Promtail (log shipping). Grafana (dashboards + alerting) is the fifth. All run on `qfind-net` and are only reachable from the host — they are **not** exposed through Funnel.

### 7a. Create monitoring config files

Create the directory structure:
```bash
mkdir -p ~/litellm-deploy/deployment/monitoring/grafana/provisioning/datasources
mkdir -p ~/litellm-deploy/deployment/monitoring/grafana/provisioning/dashboards
```

**Prometheus scrape config** — `deployment/monitoring/prometheus.yml`:
```yaml
global:
  scrape_interval: 15s
  evaluation_interval: 15s

scrape_configs:

  - job_name: vllm
    static_configs:
      - targets: ['vllm-chat:8000']
    metrics_path: /metrics

  - job_name: litellm
    static_configs:
      - targets: ['litellm:4000']
    metrics_path: /metrics

  - job_name: dcgm
    static_configs:
      - targets: ['dcgm-exporter:9400']

  - job_name: cadvisor
    static_configs:
      - targets: ['cadvisor:8080']
```

> **Why cAdvisor?** It exports per-container CPU/memory/network metrics from Docker. Lightweight and zero-config. Added here so Grafana shows container-level resource usage alongside GPU metrics.

**Loki config** — `deployment/monitoring/loki-config.yml`:
```yaml
auth_enabled: false

server:
  http_listen_port: 3100

common:
  path_prefix: /loki
  storage:
    filesystem:
      chunks_directory: /loki/chunks
      rules_directory: /loki/rules
  replication_factor: 1
  ring:
    instance_addr: 127.0.0.1
    kvstore:
      store: inmemory

schema_config:
  configs:
    - from: 2024-01-01
      store: tsdb
      object_store: filesystem
      schema: v13
      index:
        prefix: index_
        period: 24h

limits_config:
  allow_structured_metadata: false
```

**Promtail config** — `deployment/monitoring/promtail-config.yml`:
```yaml
server:
  http_listen_port: 9080
  grpc_listen_port: 0

positions:
  filename: /tmp/positions.yaml

clients:
  - url: http://loki:3100/loki/api/v1/push

scrape_configs:
  - job_name: docker
    static_configs:
      - targets:
          - localhost
        labels:
          job: docker
          __path__: /var/lib/docker/containers/*/*-json.log
    pipeline_stages:
      - json:
          expressions:
            stream: stream
            attrs: attrs
            tag: attrs.tag
      - labels:
          stream:
          tag:
```

**Grafana datasource provisioning** — `deployment/monitoring/grafana/provisioning/datasources/datasources.yml`:
```yaml
apiVersion: 1

datasources:
  - name: Prometheus
    type: prometheus
    access: proxy
    url: http://prometheus:9090
    isDefault: true

  - name: Loki
    type: loki
    access: proxy
    url: http://loki:3100
```

### 7b. Add monitoring services to `docker-compose.prod.yml`

Append the following services block to `docker-compose.prod.yml` under the `qdrant` service (before the `networks:` section):

```yaml
  # ─── Monitoring ─────────────────────────────────────────────────────────────

  prometheus:
    image: prom/prometheus:v2.53.0
    restart: unless-stopped
    command:
      - '--config.file=/etc/prometheus/prometheus.yml'
      - '--storage.tsdb.retention.time=30d'
    volumes:
      - ./monitoring/prometheus.yml:/etc/prometheus/prometheus.yml:ro
      - prometheus-data:/prometheus
    ports:
      - "127.0.0.1:9090:9090"   # loopback only — access via SSH tunnel
    networks: [qfind-net]

  dcgm-exporter:
    image: nvcr.io/nvidia/k8s/dcgm-exporter:3.3.5-3.4.0-ubuntu22.04
    restart: unless-stopped
    deploy:
      resources:
        reservations:
          devices:
            - { driver: nvidia, count: all, capabilities: [gpu] }
    ports:
      - "127.0.0.1:9400:9400"
    networks: [qfind-net]

  cadvisor:
    image: gcr.io/cadvisor/cadvisor:v0.49.1
    restart: unless-stopped
    privileged: true
    volumes:
      - /:/rootfs:ro
      - /var/run:/var/run:ro
      - /sys:/sys:ro
      - /var/lib/docker/:/var/lib/docker:ro
    ports:
      - "127.0.0.1:8080:8080"
    networks: [qfind-net]

  loki:
    image: grafana/loki:3.0.0
    restart: unless-stopped
    command: -config.file=/etc/loki/loki-config.yml
    volumes:
      - ./monitoring/loki-config.yml:/etc/loki/loki-config.yml:ro
      - loki-data:/loki
    ports:
      - "127.0.0.1:3100:3100"
    networks: [qfind-net]

  promtail:
    image: grafana/promtail:3.0.0
    restart: unless-stopped
    volumes:
      - ./monitoring/promtail-config.yml:/etc/promtail/promtail-config.yml:ro
      - /var/lib/docker/containers:/var/lib/docker/containers:ro
      - /var/run/docker.sock:/var/run/docker.sock:ro
    command: -config.file=/etc/promtail/promtail-config.yml
    networks: [qfind-net]

  grafana:
    image: grafana/grafana:11.1.0
    restart: unless-stopped
    environment:
      GF_SECURITY_ADMIN_PASSWORD: ${GRAFANA_ADMIN_PASSWORD}
      GF_USERS_ALLOW_SIGN_UP: "false"
    volumes:
      - ./monitoring/grafana/provisioning:/etc/grafana/provisioning:ro
      - grafana-data:/var/lib/grafana
    ports:
      - "127.0.0.1:3000:3000"   # loopback only — access via SSH tunnel
    networks: [qfind-net]
```

Also add the new volumes to the top-level `volumes:` section:
```yaml
volumes:
  pgdata:
  qdrant-data:
  prometheus-data:
  loki-data:
  grafana-data:
```

### 7c. Add `GRAFANA_ADMIN_PASSWORD` to `.env`

```bash
cd ~/litellm-deploy/deployment
GRAFANA_PASS=$(openssl rand -base64 16)
echo "GRAFANA_ADMIN_PASSWORD=${GRAFANA_PASS}" >> .env
chmod 600 .env
echo "Grafana admin password: ${GRAFANA_PASS}"
```
Save this password in your password manager.

### 7d. Start the monitoring stack

```bash
cd ~/litellm-deploy/deployment
docker compose -f docker-compose.prod.yml up -d \
  prometheus dcgm-exporter cadvisor loki promtail grafana
docker compose -f docker-compose.prod.yml logs -f prometheus   # wait for "Server is ready to receive web requests"
docker compose -f docker-compose.prod.yml logs -f grafana      # wait for "HTTP Server Listen" on port 3000
```

### 7e. Access Grafana via SSH tunnel

Grafana binds to `127.0.0.1:3000` — it's not exposed through Funnel. Access it via an SSH tunnel from your laptop:

```bash
# From your laptop (not the server):
ssh -L 3000:127.0.0.1:3000 <your-user>@<prod-server-ip> -N
```

Then open `http://localhost:3000` in a browser. Log in with username `admin` and the password from Step 7c.

### 7f. Import the NVIDIA GPU dashboard

In the Grafana UI:
1. Left sidebar → **Dashboards** → **Import**.
2. Enter dashboard ID **12239** → **Load**.
3. Select **Prometheus** as the data source → **Import**.
4. The dashboard loads. Confirm GPU metrics (VRAM usage, GPU utilization, temperature) show real data.

> If the DCGM Exporter container started correctly, you should see two GPUs — the A400 and the RTX 5090. The vLLM workload should appear on the RTX 5090 (index 1).

### Check
- `docker compose ps` shows all monitoring services `running`.
- Grafana dashboard 12239 shows RTX 5090 VRAM at ~14–16 GB (FP8 model loaded).
- Prometheus at `http://localhost:9090/targets` (via SSH tunnel) shows all scrape targets `UP`.
- Loki receiving logs: in Grafana → Explore → select Loki datasource → run query `{job="docker"}` → see container logs.


---

## 8. Configure Grafana alerts

Alerts are only useful if they route to a channel someone actually monitors. Configure at least the four critical alerts below. Adjust thresholds to your environment.

### 8a. Configure a contact point

In Grafana UI → **Alerting** → **Contact points** → **Add contact point**:

- For **email:** set `GF_SMTP_*` environment variables in the `grafana` service (add to `.env`). Outside the scope of this guide — use the Grafana docs for SMTP setup.
- For **webhook (Slack, Discord, etc.):** select type **Webhook**, paste the incoming webhook URL from your team's Slack/Discord channel. No additional server config needed.

Save the contact point. Set it as the default in **Alerting** → **Notification policies** → edit the default policy → select your contact point.

### 8b. Create the four critical alert rules

In Grafana UI → **Alerting** → **Alert rules** → **New alert rule**.

**Alert 1 — VRAM sustained over 90%:**
- Query (Prometheus):
  ```promql
  avg(DCGM_FI_DEV_FB_USED{gpu="1"}) / avg(DCGM_FI_DEV_FB_TOTAL{gpu="1"}) * 100
  ```
- Condition: `IS ABOVE 90`
- For: `5m` (sustained, not a spike)
- Name: `GPU VRAM > 90% (RTX 5090)`

**Alert 2 — vLLM down:**
- Query:
  ```promql
  up{job="vllm"}
  ```
- Condition: `IS BELOW 1`
- For: `1m`
- Name: `vLLM not responding`

**Alert 3 — LiteLLM error rate spike:**
- Query (checks 5xx responses; adjust metric name if LiteLLM's metric differs):
  ```promql
  rate(litellm_requests_total{status=~"5.."}[5m])
  ```
- Condition: `IS ABOVE 0.1` (more than 0.1 errors/sec over 5 min)
- For: `2m`
- Name: `LiteLLM 5xx error spike`

**Alert 4 — Host disk > 80% used:**
- Query (via cAdvisor or node-exporter — cAdvisor doesn't expose disk; use node_exporter or check via shell):

  Add a **node_exporter** service if you need host-level disk metrics (it's a single lightweight container). Alternatively, set up a simple cron-based alerting script for disk as a stopgap:
  ```bash
  # Add to crontab: check disk every hour, alert if > 80%
  0 * * * * df -h / | awk 'NR==2{gsub("%",""); if($5>80) print "DISK ALERT: "$5"% used on "$6}' | \
    grep ALERT | mail -s "Disk Alert: prod server" your@email.com 2>/dev/null || true
  ```
  For a full Prometheus-based disk alert, add `node_exporter` to the Compose file (see Troubleshooting table).

### 8c. Trigger a test alert

The easiest test: temporarily lower the VRAM alert threshold to a value below the current usage, confirm the alert fires, then restore the threshold.

In Grafana → Alert rules → edit the VRAM alert → lower threshold to `5` → save → wait up to 1 minute → confirm notification arrives in your contact channel → restore threshold to `90`.

### Check
- At least one contact point is configured and saved.
- All four alert rules exist in Grafana.
- A test alert fired and was received in the team's notification channel.

---

## 9. Set up automated backups

Two things need regular backups: the **Postgres database** (LiteLLM virtual keys, budgets, usage logs) and the **Qdrant storage** (document vectors, once ingested). The `caddy-data` volume is no longer relevant.

> **Backup target:** you need an off-site destination before this step is complete. Options: an S3-compatible bucket (AWS S3, Cloudflare R2, Backblaze B2), an SFTP server, or a local NAS with network access. This guide uses **Restic with an S3-compatible target**. Adjust the repository URL if you use a different backend.

### 9a. Install Restic

```bash
sudo apt-get install -y restic
restic version
```

### 9b. Initialize the Restic repository

```bash
# Set your backup target — example using Backblaze B2:
export RESTIC_REPOSITORY="b2:your-bucket-name:/qfind-backup"
export RESTIC_PASSWORD="$(openssl rand -base64 32)"
export B2_ACCOUNT_ID="your-b2-account-id"
export B2_ACCOUNT_KEY="your-b2-account-key"

restic init
```

> **Save `RESTIC_PASSWORD` in your password manager immediately.** Restic encrypts the repository with this password. Lose the password and the backup is permanently inaccessible, even if the files exist.

Add the credentials to a restricted file the backup script will source:
```bash
sudo tee /etc/restic-env > /dev/null << EOF
export RESTIC_REPOSITORY="${RESTIC_REPOSITORY}"
export RESTIC_PASSWORD="${RESTIC_PASSWORD}"
export B2_ACCOUNT_ID="${B2_ACCOUNT_ID}"
export B2_ACCOUNT_KEY="${B2_ACCOUNT_KEY}"
EOF
sudo chmod 600 /etc/restic-env
```

### 9c. Create the backup script

Create `deployment/backups/backup.sh`:

```bash
#!/usr/bin/env bash
set -euo pipefail
# Qfind production backup — Postgres + Qdrant snapshots → Restic
# Runs as: sudo bash backup.sh
# Scheduled: daily at 03:00 (see crontab entry in Step 9d)

DEPLOYMENT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BACKUP_TMP="/tmp/qfind-backup-$$"
TIMESTAMP=$(date +%Y%m%d-%H%M%S)

# Load Restic credentials
source /etc/restic-env

mkdir -p "${BACKUP_TMP}"
trap "rm -rf ${BACKUP_TMP}" EXIT

echo "[${TIMESTAMP}] Starting backup"

# ── 1. Postgres dump ──────────────────────────────────────────────────────────
echo "[${TIMESTAMP}] Dumping Postgres..."
docker compose -f "${DEPLOYMENT_DIR}/docker-compose.prod.yml" exec -T litellm-db \
  pg_dump -U litellm litellm > "${BACKUP_TMP}/litellm-postgres-${TIMESTAMP}.sql"
echo "[${TIMESTAMP}] Postgres dump complete: $(du -sh ${BACKUP_TMP}/*.sql | awk '{print $1}')"

# ── 2. Qdrant snapshot ────────────────────────────────────────────────────────
echo "[${TIMESTAMP}] Creating Qdrant snapshot..."
SNAPSHOT_RESP=$(curl -s -X POST http://localhost:6333/collections/qfind_docs/snapshots)
SNAPSHOT_NAME=$(echo "${SNAPSHOT_RESP}" | python3 -c "import sys,json; print(json.load(sys.stdin)['result']['name'])")

# Download the snapshot
curl -s "http://localhost:6333/collections/qfind_docs/snapshots/${SNAPSHOT_NAME}" \
  -o "${BACKUP_TMP}/qdrant-qfind_docs-${TIMESTAMP}.snapshot"

# Clean up old snapshots on the Qdrant server (keep only the current one)
curl -s -X DELETE "http://localhost:6333/collections/qfind_docs/snapshots/${SNAPSHOT_NAME}" > /dev/null
echo "[${TIMESTAMP}] Qdrant snapshot complete"

# ── 3. Restic backup ──────────────────────────────────────────────────────────
echo "[${TIMESTAMP}] Running Restic backup..."
restic backup "${BACKUP_TMP}" --tag "qfind-prod" --tag "${TIMESTAMP}"

# ── 4. Prune old snapshots (keep 7 daily, 4 weekly, 3 monthly) ───────────────
restic forget --keep-daily 7 --keep-weekly 4 --keep-monthly 3 --prune

echo "[${TIMESTAMP}] Backup complete"
```

Make it executable:
```bash
chmod +x deployment/backups/backup.sh
```

### 9d. Run a manual backup first — verify it works before scheduling

```bash
sudo bash ~/litellm-deploy/deployment/backups/backup.sh
```

Expected output:
```
[20260705-140000] Starting backup
[20260705-140000] Dumping Postgres...
[20260705-140000] Postgres dump complete: 48K
[20260705-140000] Creating Qdrant snapshot...
[20260705-140000] Qdrant snapshot complete
[20260705-140000] Running Restic backup...
snapshot abc12345 saved
[20260705-140000] Backup complete
```

Verify the backup exists in the Restic repository:
```bash
source /etc/restic-env && restic snapshots
# → Shows at least one snapshot with tag "qfind-prod"
```

### 9e. Perform a test restore

A backup that has never been restored is an untested backup. Do this now.

```bash
RESTORE_DIR="/tmp/qfind-restore-test"
mkdir -p "${RESTORE_DIR}"
source /etc/restic-env

# Restore the latest snapshot
restic restore latest --target "${RESTORE_DIR}" --tag "qfind-prod"

# Verify the files are there
ls "${RESTORE_DIR}/tmp/qfind-backup-"*/
# → Should show .sql and .snapshot files

# Spot-check the SQL dump is valid (not empty or corrupted)
head -5 "${RESTORE_DIR}"/tmp/qfind-backup-*/*.sql
# → Should start with: -- PostgreSQL database dump

rm -rf "${RESTORE_DIR}"
```

### 9f. Schedule the backup via cron

```bash
sudo crontab -e
```

Add this line (runs daily at 03:00):
```
0 3 * * * /usr/bin/bash /home/<your-user>/litellm-deploy/deployment/backups/backup.sh >> /var/log/qfind-backup.log 2>&1
```
Replace `<your-user>` with the actual username. Save and exit.

Verify the cron entry:
```bash
sudo crontab -l | grep backup
```

### Check
- Manual backup run completes without errors.
- `restic snapshots` shows at least one snapshot.
- Test restore produces a readable `.sql` file and `.snapshot` file.
- Cron entry is set.


---

## 10. Run the full smoke test against the Funnel endpoint

The `smoke-test.sh` from Day 2 hardcodes `localhost:4000`. Now that the stack has a public Funnel URL, confirm the full path works end-to-end from the server before issuing production keys.

### 10a. Generate a temporary test virtual key

```bash
cd ~/litellm-deploy/deployment
MASTER_KEY=$(grep LITELLM_MASTER_KEY .env | cut -d= -f2)

curl -s -X POST http://localhost:4000/key/generate \
  -H "Authorization: Bearer ${MASTER_KEY}" \
  -H "Content-Type: application/json" \
  -d '{
    "models": ["qfind-chat", "qfind-embed", "qfind-rerank"],
    "max_budget": 1.0,
    "rpm_limit": 30,
    "budget_duration": "1d",
    "user_id": "smoke-test-day4"
  }' | python3 -m json.tool
```
→ Copy the `"key": "sk-..."` value.

### 10b. Run the gateway smoke test locally (loopback)

```bash
LITELLM_VIRTUAL_KEY="sk-YOUR-KEY-HERE" \
  ~/litellm-deploy/deployment/scripts/smoke-test.sh
```
→ `ALL CHECKS PASSED`

### 10c. Test the full pipeline via Funnel from an external device

From a phone on mobile data, or any machine not on the same network as the server:

```bash
# Non-streaming chat
curl https://ubuntu.tailcd8da4.ts.net/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer sk-YOUR-KEY-HERE" \
  -d '{"model":"qfind-chat","messages":[{"role":"user","content":"Reply in one word: hello."}]}'

# Streaming chat
curl https://ubuntu.tailcd8da4.ts.net/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer sk-YOUR-KEY-HERE" \
  -d '{"model":"qfind-chat","stream":true,"messages":[{"role":"user","content":"Count to three."}]}'

# Embeddings
curl https://ubuntu.tailcd8da4.ts.net/v1/embeddings \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer sk-YOUR-KEY-HERE" \
  -d '{"model":"qfind-embed","input":"external embedding test"}'

# Reranking (via LiteLLM rerank endpoint)
curl https://ubuntu.tailcd8da4.ts.net/v1/rerank \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer sk-YOUR-KEY-HERE" \
  -d '{"model":"qfind-rerank","query":"file search","documents":["a tool that searches files","a recipe for soup"]}'
```

All four must return valid responses. The rerank call is the critical one — this is the first end-to-end external confirmation of `tei-rerank`.

### 10d. Confirm the disallowed model is rejected

```bash
curl https://ubuntu.tailcd8da4.ts.net/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer sk-YOUR-KEY-HERE" \
  -d '{"model":"gpt-4","messages":[{"role":"user","content":"hi"}]}'
```
→ Must return a 4xx error (model not allowed for this key).

### 10e. Revoke the smoke test key

```bash
MASTER_KEY=$(grep LITELLM_MASTER_KEY .env | cut -d= -f2)
curl -s -X DELETE http://localhost:4000/key/delete \
  -H "Authorization: Bearer ${MASTER_KEY}" \
  -H "Content-Type: application/json" \
  -d '{"keys": ["sk-YOUR-KEY-HERE"]}'
```
→ Returns `{"deleted_keys": ["sk-..."]}`.

Confirm revocation:
```bash
curl https://ubuntu.tailcd8da4.ts.net/v1/chat/completions \
  -H "Authorization: Bearer sk-YOUR-KEY-HERE" \
  -H "Content-Type: application/json" \
  -d '{"model":"qfind-chat","messages":[{"role":"user","content":"hi"}]}'
```
→ Must return 401. Revocation is immediate.

### Check
- Local smoke test: `ALL CHECKS PASSED`.
- All four external Funnel tests return valid responses.
- Disallowed model returns an error (not 200).
- Revoked key returns 401 immediately.

---

## 11. Issue production virtual keys

Smoke test key is revoked. Now issue the real production keys.

### 11a. Decide on key scope

For the initial production deployment, create at minimum:
- **One key per integration** (e.g., one key for the Qfind desktop client, one key for any internal testing tool). Do not share a single key between multiple systems — independent keys enable independent revocation.

### 11b. Issue keys

For each consumer, run:
```bash
MASTER_KEY=$(grep LITELLM_MASTER_KEY .env | cut -d= -f2)

curl -s -X POST http://localhost:4000/key/generate \
  -H "Authorization: Bearer ${MASTER_KEY}" \
  -H "Content-Type: application/json" \
  -d '{
    "models": ["qfind-chat", "qfind-embed", "qfind-rerank"],
    "max_budget": 100.0,
    "rpm_limit": 60,
    "tpm_limit": 100000,
    "budget_duration": "30d",
    "user_id": "qfind-desktop-client-v1"
  }' | python3 -m json.tool
```

Adjust `user_id`, `max_budget`, `rpm_limit`, and `tpm_limit` per consumer.

Record each key in your team's password manager or secrets vault. The key value is shown only at creation time — LiteLLM stores a hash of it, not the raw key. If lost, delete and re-issue.

### 11c. Verify key scoping

For each issued key, confirm:
1. Allowed model (`qfind-chat`) returns 200.
2. Disallowed model (`gpt-4`) returns a 4xx error.

### 11d. Confirm master key is secured

The master key must not be distributed or embedded in any client application. Verify it exists only in `deployment/.env` (permissions 600) and your password manager.

```bash
ls -la ~/litellm-deploy/deployment/.env
# → -rw------- 1 <user> <user> ... .env
```

### Check
- Production virtual keys issued for each integration point.
- Each key is stored securely (password manager / secrets vault).
- `gpt-4` returns an error with each key.
- Master key is not in version control (`git log --all -- '*.env'` returns nothing).

---

## 12. Commit the updated `docker-compose.prod.yml`

The Compose file has been significantly changed today. Commit it with a clear message:

```bash
cd ~/litellm-deploy
git add deployment/docker-compose.prod.yml \
        deployment/monitoring/ \
        deployment/backups/backup.sh
git commit -m "day4: harden compose, add monitoring stack, backup script

- Pin CUDA_VISIBLE_DEVICES=1 for RTX 5090 (dual-GPU server)
- Add restart: unless-stopped to tei-embed and tei-rerank
- Loopback-restrict TEI ports (127.0.0.1:8001/8002)
- Pin qdrant image version (remove :latest)
- Retire caddy service (Funnel handles ingress)
- Add monitoring: prometheus, dcgm-exporter, cadvisor, loki, promtail, grafana
- Add backups/backup.sh (Postgres + Qdrant → Restic)
"
```

---

## 13. End-of-Day 4 Definition of Done

**Compose and infrastructure fixes (Day 3 carry-over)**
- [ ] `tei-rerank` verified serving correct scored JSON responses.
- [ ] `restart: unless-stopped` present on `tei-embed` and `tei-rerank`.
- [ ] TEI ports bound to `127.0.0.1` (not `0.0.0.0`).
- [ ] `CUDA_VISIBLE_DEVICES=1` set on `vllm-chat`; `nvidia-smi` confirms VRAM on RTX 5090.
- [ ] Qdrant image pinned (no `latest`).
- [ ] Caddy container stopped; Funnel confirmed working without it.
- [ ] Tailscale Funnel systemd service enabled and active.
- [ ] `.env.example` updated and committed.

**Firewall**
- [ ] UFW: only SSH (22) and `tailscale0` open — ports 80 and 443 removed.
- [ ] `sudo ufw status verbose` confirms the above.

**Qdrant**
- [ ] `init_collection.py` run; `curl /collections/qfind_docs` returns `"status": "green"`.

**Host hardening**
- [ ] `unattended-upgrades` active.
- [ ] `fail2ban` active, watching SSH.
- [ ] SSH password authentication disabled.

**Monitoring**
- [ ] All 6 monitoring services running: `prometheus`, `dcgm-exporter`, `cadvisor`, `loki`, `promtail`, `grafana`.
- [ ] Grafana dashboard 12239 shows live RTX 5090 VRAM data.
- [ ] All Prometheus scrape targets show `UP`.
- [ ] At least one alert rule configured and test-fired successfully.

**Backups**
- [ ] `backup.sh` runs without errors.
- [ ] `restic snapshots` shows at least one snapshot.
- [ ] Test restore produces valid `.sql` and `.snapshot` files.
- [ ] Cron job scheduled (daily at 03:00).

**Key management**
- [ ] Full smoke test (`ALL CHECKS PASSED`) against local and Funnel endpoints.
- [ ] All four external endpoint types confirmed: chat (streaming + non-streaming), embed, rerank.
- [ ] Smoke test key revoked; revocation returns 401 immediately.
- [ ] Production virtual keys issued, stored securely, scoped correctly.
- [ ] Master key confirmed restricted to `deployment/.env` (permissions 600) and password manager.

If all boxes are checked, Day 5 (concurrency validation, load testing, pilot launch, and runbook) can proceed.

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `tei-rerank` returns error on `/rerank` | ONNX model format not supported | Download raw safetensors: `huggingface-cli download BAAI/bge-reranker-v2-m3 --local-dir model-cache/bge-reranker-v2-m3`; update volume mount in Compose |
| vLLM loaded on A400 instead of RTX 5090 after restart | `CUDA_VISIBLE_DEVICES` not set or wrong index | Verify `nvidia-smi -L` device order; set `CUDA_VISIBLE_DEVICES: "1"` in vllm-chat environment |
| Funnel stops working after `tailscale-funnel.service` restart | Service stopped Funnel then didn't restart | Check `journalctl -u tailscale-funnel.service`; re-run `tailscale funnel --bg 4000` manually |
| `tailscale funnel --bg --off` hangs | Tailscale daemon not responding | `sudo systemctl restart tailscaled`; then re-enable Funnel |
| Grafana shows no GPU data | DCGM Exporter not started or no GPU access | `docker compose logs dcgm-exporter`; confirm it has the GPU device reservation and NVIDIA Container Toolkit is configured |
| Prometheus target `vllm` shows `DOWN` | vLLM metrics endpoint not reachable | `docker compose logs vllm-chat`; confirm vLLM started; the `/metrics` endpoint requires the service to be fully loaded |
| Loki receives no logs | Promtail path not matching container log location | Check `/var/lib/docker/containers/` exists and Promtail has read access; confirm Docker uses the `json-file` log driver (default) |
| `restic backup` fails with auth error | `B2_ACCOUNT_ID`/`B2_ACCOUNT_KEY` wrong, or wrong bucket | Re-check credentials in `/etc/restic-env`; test with `restic snapshots` interactively |
| `pg_dump` produces an empty file | Postgres not reachable inside container at backup time | Check `docker compose ps litellm-db`; ensure backup script uses the correct Compose project name |
| `fail2ban` banning your own IP | Too many SSH connection attempts from your machine | `sudo fail2ban-client set sshd unbanip <your-ip>`; increase `maxretry` if your IP is dynamic |
| Qdrant collection already exists error in `init_collection.py` | Script run more than once | Safe to ignore — the script is idempotent and skips creation if the collection exists |
| Virtual key returns 401 despite correct key value | `LITELLM_SALT_KEY` changed between runs, invalidating stored keys | Do not change `LITELLM_SALT_KEY` after the first run; if changed accidentally, wipe `pgdata` volume and regenerate all keys |
| `unattended-upgrades` not active | Not installed, or dpkg-reconfigure was answered "No" | `sudo dpkg-reconfigure --priority=low unattended-upgrades`; answer "Yes" |

---

## Learnings — what Day 4 teaches

1. **The fork doc is the actual source of truth, not the original plan.** Day 4 started by reading the fork, not the plan. Every step built on the real system state — the dual-GPU situation, the Funnel topology, the TEI CPU fallback. Plans are hypotheses; fork docs are facts.

2. **Compose file hardening is cheap and the payoff is high.** Adding `restart: unless-stopped` to two services, loopback-restricting two ports, and pinning one environment variable takes minutes. Together they eliminate three failure modes: silent reboots that leave TEI down, TEI ports accidentally reachable from the network, and vLLM silently binding to the wrong GPU after a driver reorder.

3. **Systemd is more reliable than cron `@reboot` for startup ordering.** `@reboot` cron runs after login or at a fixed delay, with no dependency tracking. A systemd service with `After=tailscaled.service` ensures Funnel doesn't start before the Tailscale daemon is ready — the exact kind of race condition that produces "it works sometimes after reboot" bugs.

4. **Monitoring binds to loopback and is accessed via SSH tunnel.** Grafana and Prometheus ports (3000, 9090) bind to `127.0.0.1`, not exposed through Funnel. This keeps monitoring off the public internet while still being accessible. SSH tunneling is the right tool for this: it uses the already-secured SSH channel, requires no additional authentication config, and produces no new attack surface.

5. **DCGM Exporter makes GPU utilization a first-class observable.** `nvidia-smi` is a point-in-time command. DCGM Exporter continuously scrapes GPU VRAM, utilization, temperature, and power draw, and Grafana dashboard 12239 turns those into time-series graphs. The first time the monitoring stack shows a VRAM spike correlating with a specific user's request pattern is the moment "we have enough GPU headroom" stops being an assumption and becomes a measured fact.

6. **Test the restore, not just the backup.** `restic backup` succeeding proves data was written to the target. The restore test proves it can actually be read back and reassembled. These are different guarantees. The first backup that matters is always the one you need to recover from — and discovering a broken backup during a crisis is far worse than during a scheduled test.

7. **Virtual key revocation is the key security property.** The whole point of LiteLLM's virtual key system is that a compromised or over-budget key can be deleted instantly without affecting anything else. The Day 4 smoke test deliberately includes a revocation check — issuing a key, confirming it works, revoking it, confirming it returns 401 — because that operational path should be exercised before it's needed in an emergency.
