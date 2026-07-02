# Day 3 — Cutover to Production: Infra + Inference Live on RTX 5090

**Who this is for:** the same developer who completed Days 1 and 2. Day 1 and 2 concepts (Docker, Compose, vLLM, TEI, LiteLLM, Qdrant, virtual keys, the RAG pipeline) are assumed. New concepts introduced today are explained on first use.

**Day 3 goal in one sentence:** the validated local stack is re-deployed on the remote RTX 5090 Linux server with full production configuration — fresh secrets, real domain DNS, HTTPS via Caddy, the full Compose stack with restart policies — and confirmed working end-to-end from an external network.

**What you proved in Phase A (Days 1–2):**
- vLLM, TEI (embed + rerank), Postgres, Redis, LiteLLM, and Qdrant all run together inside Docker on `qfind-net`.
- The four-stage RAG pipeline (embed → search → RRF → rerank) retrieves relevant chunks from Qdrant.
- LiteLLM routes requests through virtual keys with budget/rate limits.
- `scripts/smoke-test.sh` and `scripts/loadtest.py` are reusable validation harnesses.

**What changes on Day 3 (the cutover):**

| Aspect | Local (Phase A) | Production (Day 3+) |
|---|---|---|
| Chat model | Qwen2.5-3B-Instruct AWQ | **Qwen2.5-14B** (same `qfind-chat` alias) |
| TEI placement | CPU (spare VRAM for 4050) | **GPU** (5090 has 32 GB) |
| Quantization | AWQ (FP8 unsupported on WSL2) | **FP8** (AWQ fallback if unstable) |
| vLLM VRAM flags | `--max-model-len 8192 --max-num-seqs 8 --enforce-eager` | **relaxed/removed** — re-tune for 32 GB |
| Secrets | Dev placeholders | **Fresh, randomly generated prod secrets** |
| Entry point | Direct port 4000 (localhost) | **Caddy HTTPS on real domain** |
| Firewall | WSL2 / Windows Firewall | **UFW: only 80/443/SSH open** |
| Restart policy | Not set | **`restart: unless-stopped` on every service** |
| Cert | None | **Let's Encrypt auto-TLS** |

Everything else — the Compose topology, service names, config file paths, LiteLLM `model_list` aliases, RAG pipeline scripts, and the smoke-test harness — migrates unchanged. The cutover is a config change, not a rebuild.

> **Day 3 is the real GPU validation day.** Days 1–2 only proved plumbing on a 4050. FP8, the §11.4 VRAM budget, and concurrency are validated here for the first time. Budget time accordingly — driver verification and model weight downloads can consume the morning if the server is freshly provisioned.

---

## 0. New concepts for Day 3

- **Production Linux server:** a bare-metal or cloud machine running Ubuntu 22.04/24.04 LTS, with direct (not WSL2) access to the RTX 5090. Unlike your laptop, this machine has no Windows layer — the NVIDIA driver is installed directly in Linux, and FP8 quantization is supported.
- **FP8 quantization:** a lower-precision (8-bit floating point) format that cuts VRAM use while preserving quality. vLLM v0.17.0+ supports FP8 on Blackwell-architecture GPUs (like the RTX 5090 / SM120). Not available under WSL2 — validated for the first time here.
- **AWQ fallback:** if FP8 is unstable on the 5090 (driver issues, model compatibility), you fall back to AWQ (the same format used locally). Quality is comparable; the model loads from the pre-downloaded cache.
- **Caddy:** a web server written in Go that acts as the HTTPS reverse proxy in front of LiteLLM. Its killer feature: it obtains and auto-renews Let's Encrypt TLS certificates with zero configuration, as long as DNS resolves correctly to the server.
- **Let's Encrypt:** a free certificate authority. Caddy speaks the ACME protocol to automatically get a certificate for your domain. It needs port 80 reachable from the internet for the HTTP-01 challenge, and a valid A record pointing to the server's IP.
- **Reverse proxy:** Caddy sits on port 443 (HTTPS), terminates TLS, and forwards plain HTTP to LiteLLM on port 4000 (which stays private). The world sees HTTPS; LiteLLM never handles TLS itself.
- **UFW (Uncomplicated Firewall):** Ubuntu's front-end for `iptables`. You will use it to allow only ports 80, 443, and SSH, and block everything else — including the raw service ports (8000, 4000, 6333, etc.) that should never be publicly reachable.
- **`restart: unless-stopped`:** a Compose restart policy that tells Docker to automatically restart a service if it crashes or the machine reboots, unless you explicitly stopped it yourself. Without this, a reboot leaves all services down.
- **LITELLM_SALT_KEY:** an additional secret used by LiteLLM to hash/encrypt virtual key data stored in Postgres. Separate from the master key. Must be generated fresh for prod and never changed after the first run (changing it invalidates all stored keys).
- **External network test:** confirming HTTPS works from a machine that is NOT the server and NOT your LAN — a phone on mobile data, or a remote machine. This is the only way to confirm Caddy's cert is valid and the firewall is correctly configured.

---

## 1. Server access + GPU driver verification

Before doing anything else, establish that the server is reachable and the GPU is usable.

### 1a. SSH into the production server

```bash
ssh <your-user>@<prod-server-ip>
```

Confirm you have `sudo` rights:
```bash
sudo whoami   # should print: root
```

Confirm the OS:
```bash
lsb_release -a
# Should show Ubuntu 22.04 LTS or 24.04 LTS
uname -r      # kernel version — should be 5.15+ (22.04) or 6.8+ (24.04)
```

> If you don't yet have SSH credentials or keys set up for the server, stop here and configure access before proceeding. Day 3 cannot start without a working SSH session.

### 1b. Check for an existing NVIDIA driver

```bash
nvidia-smi
```

**Three possible outcomes:**

| Result | Meaning | Action |
|---|---|---|
| Shows RTX 5090 table with CUDA 12.x | Driver already installed and correct | Proceed to Step 2 directly |
| Shows a GPU but old CUDA (< 12.6) | Old driver — needs upgrade | Follow Step 1c |
| `nvidia-smi: command not found` | No driver | Follow Step 1c |

### 1c. Install / upgrade the NVIDIA driver (if needed)

> **This is native Linux, not WSL2.** Unlike Day 1, you install the full Linux NVIDIA driver here — not just the CUDA toolkit.

```bash
# Add the NVIDIA CUDA repository (for Ubuntu 22.04 — adjust for 24.04 if needed)
wget https://developer.download.nvidia.com/compute/cuda/repos/ubuntu2204/x86_64/cuda-keyring_1.1-1_all.deb
sudo dpkg -i cuda-keyring_1.1-1_all.deb
sudo apt-get update

# Install the driver. 570+ supports Blackwell/SM120 (RTX 5090)
sudo apt-get install -y cuda-drivers-570
sudo reboot
```

After reboot, reconnect via SSH and verify:
```bash
nvidia-smi
# Must show: RTX 5090, CUDA Version: 12.8 (or higher)
```

> **Why pin 570?** The RTX 5090 (Blackwell / SM120) requires driver ≥ 570 for FP8 and full Blackwell feature support. The `cuda-drivers-570` meta-package installs driver 570.x with the matching CUDA runtime.

### Check
- `nvidia-smi` on the prod host prints the RTX 5090 with CUDA 12.x.
- No errors in the output.

---

## 2. Install Docker + NVIDIA Container Toolkit on the production server

The production server gets the same Docker Engine + NVIDIA Container Toolkit setup as your WSL2 machine — with one difference: on native Linux, Docker starts automatically via `systemd` (no manual `sudo service docker start`).

### 2a. Install Docker Engine + Compose plugin

```bash
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker $USER

# Log out and back in (or use newgrp) for the group change to take effect
newgrp docker

# Verify
docker run --rm hello-world
```

### 2b. Install NVIDIA Container Toolkit

```bash
curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey \
  | sudo gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg

curl -s -L https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list \
  | sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' \
  | sudo tee /etc/apt/sources.list.d/nvidia-container-toolkit.list

sudo apt-get update && sudo apt-get install -y nvidia-container-toolkit
sudo nvidia-ctk runtime configure --runtime=docker
sudo systemctl restart docker
```

### Check — GPU inside a container

```bash
docker run --rm --gpus all nvidia/cuda:12.8.0-base-ubuntu24.04 nvidia-smi
```
→ Prints the RTX 5090 table from inside a container. This is the same check as Day 1, Step 4.

---

## 3. DNS — Create the A record

Caddy obtains a Let's Encrypt certificate automatically, but only if the domain resolves correctly to the server's public IP. You must create the DNS `A` record **before** starting Caddy.

### 3a. Determine the server's public IP

```bash
curl -4 ifconfig.me
# Prints the server's public IPv4 address
```

### 3b. Create the A record in your DNS provider

- **Domain:** `api.<company-domain>` (replace `<company-domain>` with the real domain, e.g., `api.example.com`).
- **Type:** `A`
- **Value:** the IP from Step 3a.
- **TTL:** 300 (5 minutes) or lower during initial setup, so changes propagate quickly.

> **Who does this?** Whoever controls the domain's DNS — the company's IT admin, registrar account, or Cloudflare if the domain is managed there. Confirm this person exists and has access **before** Day 3.

### 3c. Confirm the record propagates

```bash
dig api.<company-domain>     # replace with your real domain
# or
nslookup api.<company-domain>
```
→ Must return the server's public IP from Step 3a.

Wait until propagation is complete (1–5 minutes for most DNS providers, up to an hour for some). **Do NOT start Caddy before the A record resolves** — Let's Encrypt will fail the challenge and rate-limit you.

### Check
- `dig api.<company-domain>` returns the server's public IP, queried from **outside** the server (e.g., from your laptop or phone).

---

## 4. Firewall — Lock down to 80/443/SSH

By default, a fresh Ubuntu install either has no firewall active or allows all traffic. You must explicitly block everything except the needed ports **before** exposing services.

### 4a. Install and configure UFW

```bash
sudo apt-get install -y ufw

# Allow SSH first (so you don't lock yourself out)
sudo ufw allow ssh       # port 22 by default
# OR if SSH runs on a custom port:
# sudo ufw allow 12345/tcp

# Allow HTTP and HTTPS
sudo ufw allow 80/tcp
sudo ufw allow 443/tcp

# Default deny
sudo ufw default deny incoming
sudo ufw default allow outgoing

# Enable the firewall
sudo ufw enable

# Verify
sudo ufw status verbose
```

### Expected output
```
Status: active

To                         Action      From
--                         ------      ----
22/tcp                     ALLOW       Anywhere
80/tcp                     ALLOW       Anywhere
443/tcp                     ALLOW       Anywhere
```

> **SSH hardening (optional but recommended):** if SSH is reachable from the whole internet on port 22, consider moving it to a non-standard port, limiting source IPs (`sudo ufw allow from <your-office-ip> to any port 22`), or using key-only authentication with password login disabled.

### Check
From your laptop (or any machine outside the server's network):
```bash
nmap <prod-server-ip>
```
→ Should show only ports 22, 80, 443 open. If you see 8000, 4000, 6333, or other service ports open, the firewall is not active. Re-check `ufw enable`.

---

## 5. Generate fresh production secrets

Never reuse local dev secrets in production. All three must be generated fresh.

### 5a. Generate the secrets

On the **production server**, in the deployment directory:

```bash
cd ~/litellm-deploy/deployment   # or wherever you place the stack on prod

# Postgres password (long random string)
PGPASS=$(openssl rand -base64 32)

# LiteLLM master key (sk-master-<hex>)
MASTER_KEY="sk-master-$(openssl rand -hex 32)"

# LiteLLM salt key (long random string)
SALT_KEY=$(openssl rand -base64 32)

# Write to .env (appending if not exists, overwriting if re-run)
cat > .env << EOF
POSTGRES_USER=litellm
POSTGRES_PASSWORD=${PGPASS}
POSTGRES_DB=litellm
DATABASE_URL=postgresql://litellm:${PGPASS}@litellm-db:5432/litellm
LITELLM_MASTER_KEY=${MASTER_KEY}
LITELLM_SALT_KEY=${SALT_KEY}
EOF

# Secure the file
chmod 600 .env

# Print the master key to your terminal (you'll need it for Step 8's virtual key generation)
echo "===== LITELLM_MASTER_KEY (save this) ====="
echo "$MASTER_KEY"
echo "=========================================="
```

**Save the master key somewhere secure** (a password manager, a company secrets vault). It's needed to create/revoke virtual keys and must never be committed to version control.

### Check
- `.env` exists with permissions `600` (user-read-only).
- `cat .env` shows three generated secrets (POSTGRES_PASSWORD, LITELLM_MASTER_KEY, LITELLM_SALT_KEY) — none are the word "change-me".

---

## 6. Copy the deployment repo to production and download production model weights

### 6a. Copy or clone the repo

Option 1 — Git (if the repo is on GitHub/GitLab):
```bash
cd ~
git clone <repo-url> litellm-deploy
cd litellm-deploy/deployment
```

Option 2 — `rsync` from your local machine:
```bash
# From your laptop (not the server)
rsync -avz --exclude model-cache --exclude venv \
  ~/litellm-deploy/ <user>@<prod-ip>:~/litellm-deploy/
```

### 6b. Download the **production** model weights

The production chat model is **Qwen2.5-14B**, not the 3B local model. Start the download now (it's large):

```bash
cd ~/litellm-deploy/deployment
mkdir -p model-cache

# Install huggingface-cli if not present
sudo apt-get install -y python3-pip
pip3 install -U "huggingface_hub[cli]"

# Production chat model (14B)
hf download Qwen/Qwen2.5-14B-Instruct \
  --local-dir model-cache/qwen2.5-14b-instruct

# Embedder and reranker (same as local, already in the repo if you rsynced)
hf download BAAI/bge-m3 \
  --local-dir model-cache/bge-m3

hf download onnx-community/bge-reranker-v2-m3-ONNX \
    --local-dir ./model-cache/bge-reranker-v2-m3-onnx
```

> These downloads run in the foreground and can take 10–30 minutes depending on network speed. While they run, you can open a second SSH session to continue with Step 7–8 in parallel.

### Check
- `ls model-cache/qwen2.5-14b-instruct/` shows many `.safetensors` files (the model weights).
- `ls model-cache/bge-m3/` and `ls model-cache/bge-reranker-v2-m3/` exist and are populated.

---

## 7. Create the production docker-compose.yml

The production Compose file differs from the local one in four concrete ways:
1. The chat model path and served-model config point to Qwen2.5-14B.
2. vLLM's tiny-VRAM flags are relaxed/removed — the 5090 has 32 GB.
3. TEI moves from the CPU image to the GPU image (no more `cpu-` prefix).
4. Every service gets `restart: unless-stopped`.

You have two options: create a separate `docker-compose.prod.yml` (cleaner separation) or use Compose override files. For this guide we use a **`docker-compose.prod.yml`** that is the complete, standalone prod stack. This avoids any chance of accidentally merging local dev flags into production.

Create `~/litellm-deploy/deployment/docker-compose.prod.yml`:

```yaml
# Production Compose — RTX 5090 server
# Usage: docker compose -f docker-compose.prod.yml up -d
# All model-cache paths are absolute so the file is unambiguous when referenced with -f

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
      - "127.0.0.1:8000:8000"   # bind to loopback only — Caddy/LiteLLM reach it via qfind-net
    deploy:
      resources:
        reservations:
          devices:
            - { driver: nvidia, count: all, capabilities: [gpu] }
    networks: [qfind-net]

  tei-embed:
    image: ghcr.io/huggingface/text-embeddings-inference:1.5   # GPU image (no cpu- prefix)
    restart: unless-stopped
    command:
      - "--model-id"
      - "/models/bge-m3"
      - "--max-batch-tokens"
      - "16384"
      - "--max-concurrent-requests"
      - "128"
    volumes:
      - ./model-cache/bge-m3:/models/bge-m3:ro
    ports:
      - "127.0.0.1:8001:80"
    deploy:
      resources:
        reservations:
          devices:
            - { driver: nvidia, count: all, capabilities: [gpu] }
    networks: [qfind-net]

  tei-rerank:
    image: ghcr.io/huggingface/text-embeddings-inference:1.5   # GPU image
    restart: unless-stopped
    command:
      - "--model-id"
      - "/models/bge-reranker-v2-m3"
      - "--max-batch-tokens"
      - "8192"
      - "--max-concurrent-requests"
      - "64"
    volumes:
      - ./model-cache/bge-reranker-v2-m3:/models/bge-reranker-v2-m3:ro
    ports:
      - "127.0.0.1:8002:80"
    deploy:
      resources:
        reservations:
          devices:
            - { driver: nvidia, count: all, capabilities: [gpu] }
    networks: [qfind-net]

  # ─── Gateway ────────────────────────────────────────────────────────────────

  litellm:
    image: ghcr.io/berriai/litellm:main-v1.40.0
    restart: unless-stopped
    command: ["--config", "/app/config.yaml", "--port", "4000"]
    volumes:
      - ./litellm/config.yaml:/app/config.yaml:ro
    ports:
      - "127.0.0.1:4000:4000"   # loopback only — Caddy proxies externally
    env_file: [.env]
    depends_on:
      - litellm-db
      - litellm-redis
    networks: [qfind-net]

  # ─── Vector store ───────────────────────────────────────────────────────────

  qdrant:
    image: qdrant/qdrant:v1.9.0
    restart: unless-stopped
    volumes:
      - qdrant-data:/qdrant/storage
    ports:
      - "127.0.0.1:6333:6333"
      - "127.0.0.1:6334:6334"
    networks: [qfind-net]

  # ─── Reverse proxy (HTTPS) ──────────────────────────────────────────────────

  caddy:
    image: caddy:2.9.1-alpine
    restart: unless-stopped
    ports:
      - "80:80"
      - "443:443"
    volumes:
      - ./caddy/Caddyfile:/etc/caddy/Caddyfile:ro
      - caddy-data:/data
      - caddy-config:/config
    networks: [qfind-net]

networks:
  qfind-net:

volumes:
  pgdata:
  qdrant-data:
  caddy-data:     # persists TLS certs — never delete this volume
  caddy-config:
```

### Key differences from the local `docker-compose.yml`

| Change | Why |
|---|---|
| `--quantization fp8` on vLLM | FP8 is the production quantization (Blackwell native); AWQ was local-only |
| `--max-model-len 16384` | Larger context window on 32 GB vs 8192 on 4 GB locally |
| `--gpu-memory-utilization 0.70` | Leaves 30% of 32 GB (~9.6 GB) for TEI on GPU; tune up if vLLM needs more |
| `--tensor-parallel-size 1` | Single GPU; remove or set to 2+ if you ever add a second card |
| `--enforce-eager` **removed** | CUDA graph capture is fine on a full Linux server with a proper 5090 driver |
| TEI image `1.5` (not `cpu-1.5`) | GPU image; requires `deploy.resources.reservations.devices` |
| `--max-batch-tokens 16384` on TEI | GPU can handle large batches efficiently |
| All ports bind to `127.0.0.1:` | Services are **not** reachable from the public internet; Caddy + LiteLLM are the only entry points |
| `restart: unless-stopped` everywhere | Services survive reboots and crashes automatically |
| `caddy:2.9.1-alpine` service | New — handles HTTPS termination and TLS cert lifecycle |
| `caddy-data` volume | Persists TLS private keys and certs — must not be deleted |

---

## 8. Author the Caddyfile

Caddy's configuration lives in `deployment/caddy/Caddyfile`. Create it:

```bash
mkdir -p ~/litellm-deploy/deployment/caddy
```

Create `deployment/caddy/Caddyfile`:

```caddyfile
# Replace api.example.com with your real domain (matches the DNS A record from Step 3)
api.example.com {

    # Terminate TLS here; forward plain HTTP to LiteLLM inside the Docker network
    reverse_proxy litellm:4000

    # Rate limiting per source IP — defense-in-depth (§10.3)
    # Limit each IP to 20 requests/second, burst up to 30
    rate_limit {
        zone dynamic {
            key    {remote_host}
            events 20
            window 1s
        }
    }

    # Security headers
    header {
        Strict-Transport-Security "max-age=31536000; includeSubDomains"
        X-Content-Type-Options nosniff
        X-Frame-Options DENY
        -Server
    }

    # Log all requests to stdout (Loki/Promtail will pick them up in Day 4)
    log {
        output stdout
        format json
    }
}
```

> **Two things to customize before saving:**
> 1. Replace `api.example.com` with your actual domain (same as the DNS A record from Step 3).
> 2. The `rate_limit` directive requires the [caddy-ratelimit](https://github.com/mholt/caddy-ratelimit) module. The official `caddy:2.9.1-alpine` image does **not** include it by default. For simplicity, use the version without rate limiting first (add it as part of Day 4 hardening with a custom Caddy image). A starter Caddyfile without the rate-limit module:

```caddyfile
# Minimal Caddyfile for Day 3 — rate limiting added in Day 4
api.example.com {

    reverse_proxy litellm:4000

    header {
        Strict-Transport-Security "max-age=31536000; includeSubDomains"
        X-Content-Type-Options nosniff
        X-Frame-Options DENY
        -Server
    }

    log {
        output stdout
        format json
    }
}
```

Save this minimal version in `deployment/caddy/Caddyfile` for now. You will extend it in Day 4.

---

## 9. Validate the production Compose file + update the .env

### 9a. Add LITELLM_SALT_KEY to .env

The `LITELLM_SALT_KEY` is used by LiteLLM to encrypt virtual key data in Postgres. You generated it in Step 5 — confirm it's in `.env`:

```bash
grep LITELLM_SALT_KEY .env   # should print the variable
```

If it's missing, add it:
```bash
echo "LITELLM_SALT_KEY=$(openssl rand -base64 32)" >> .env
chmod 600 .env
```

### 9b. Validate the Compose file syntax

```bash
cd ~/litellm-deploy/deployment
docker compose -f docker-compose.prod.yml config
```
→ Prints the merged, resolved Compose config with all `${VAR}` substituted from `.env`. Scan it for `change-me` (should be zero instances) and confirm service names look correct.

---

## 10. Re-validate engines on the 5090 — before adding Caddy

Before bringing up the full stack and Caddy, isolate and prove the inference engines on the production GPU first. If something is wrong (driver, FP8, model path), you want to know now — not after fighting Caddy's TLS state at the same time.

### 10a. Start vLLM first, watch it load

```bash
cd ~/litellm-deploy/deployment
docker compose -f docker-compose.prod.yml up -d vllm-chat
docker compose -f docker-compose.prod.yml logs -f vllm-chat
```

Wait for `Application startup complete` (may take 3–5 minutes for the 14B model to load into VRAM). Watch for errors:

**FP8 fails? Use the AWQ fallback:**
If you see an error like `ValueError: FP8 quantization is not supported` or a CUDA error mentioning fp8, the prod driver version may not fully support FP8 on this GPU/model combination yet. Switch to AWQ:

```bash
# Download the AWQ variant of the 14B model
huggingface-cli download Qwen/Qwen2.5-14B-Instruct-AWQ \
  --local-dir model-cache/qwen2.5-14b-instruct-awq

# Edit docker-compose.prod.yml:
# change: --quantization fp8 and --model /models/qwen2.5-14b-instruct
# to:     --quantization awq  and --model /models/qwen2.5-14b-instruct-awq
# update: volumes: ./model-cache/qwen2.5-14b-instruct-awq:/models/qwen2.5-14b-instruct-awq:ro
```

After editing, restart:
```bash
docker compose -f docker-compose.prod.yml up -d --force-recreate vllm-chat
docker compose -f docker-compose.prod.yml logs -f vllm-chat
```

### 10b. Check vLLM on the GPU

Once it shows `Application startup complete`, test from the server itself:
```bash
curl http://localhost:8000/v1/chat/completions -H 'Content-Type: application/json' \
  -d '{"model":"qfind-chat","messages":[{"role":"user","content":"Reply in one word: hello."}]}'
```
Also check VRAM usage:
```bash
nvidia-smi
```
Note the VRAM used by vLLM. The 14B model (FP8) uses roughly **14–16 GB**; AWQ uses roughly **8–10 GB**. Both leave headroom for TEI on the 5090's 32 GB.

### 10c. Start TEI on the GPU

```bash
docker compose -f docker-compose.prod.yml up -d tei-embed tei-rerank
docker compose -f docker-compose.prod.yml logs -f tei-embed   # wait for "Ready"
```

Check TEI:
```bash
curl http://localhost:8001/embed -H 'Content-Type: application/json' \
  -d '{"inputs":"production GPU embedding test"}'

curl http://localhost:8002/rerank -H 'Content-Type: application/json' \
  -d '{"query":"search","texts":["relevant content","unrelated content"]}'
```

Check combined VRAM — both vLLM and TEI on the GPU:
```bash
nvidia-smi
```
Expected total: ~18–22 GB (FP8 14B + BGE-M3 + reranker) out of 32 GB — well within the §11.4 budget.

### 10d. Run the smoke test on prod (direct engine mode)

Copy the smoke test from your local machine if it wasn't included in the rsync:
```bash
# From your laptop:
# rsync -avz ~/litellm-deploy/deployment/scripts/ <user>@<prod-ip>:~/litellm-deploy/deployment/scripts/
chmod +x ~/litellm-deploy/deployment/scripts/smoke-test.sh
~/litellm-deploy/deployment/scripts/smoke-test.sh
```
→ `ALL CHECKS PASSED` for the direct engine checks (no virtual key needed for this first run).

### Check
- vLLM answers chat completions.
- Both TEI endpoints answer.
- VRAM is within budget (< 30 GB total); no OOM errors in any log.
- `smoke-test.sh` direct checks pass.

---

## 11. Start Caddy + the full production stack

### 11a. Start Postgres and Redis

```bash
docker compose -f docker-compose.prod.yml up -d litellm-db litellm-redis
sleep 5   # give Postgres a moment to initialize the cluster on first boot

docker compose -f docker-compose.prod.yml exec litellm-db pg_isready
# → /var/run/postgresql:5432 - accepting connections

docker compose -f docker-compose.prod.yml exec litellm-redis redis-cli ping
# → PONG
```

### 11b. Start LiteLLM

```bash
docker compose -f docker-compose.prod.yml up -d litellm
docker compose -f docker-compose.prod.yml logs -f litellm
# Wait for "LiteLLM: Proxy initialized" — Ctrl+C to stop watching
```

Quick gateway check (still using the master key):
```bash
curl http://localhost:4000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $(grep LITELLM_MASTER_KEY .env | cut -d= -f2)" \
  -d '{"model":"qfind-chat","messages":[{"role":"user","content":"ping"}]}'
```
→ Returns a JSON response.

### 11c. Start Qdrant

```bash
docker compose -f docker-compose.prod.yml up -d qdrant
# Wait for "Qdrant HTTP listening on 0.0.0.0:6333"
docker compose -f docker-compose.prod.yml logs -f qdrant

curl http://localhost:6333/healthz
# → {"title":"qdrant - Vector Search Engine","version":"..."}
```

Re-initialize the Qdrant collection on the prod instance (it has a fresh, empty volume):
```bash
cd ~/litellm-deploy/deployment
pip3 install qdrant-client   # if not already installed
python3 qdrant/init_collection.py
```
→ `Collection 'qfind_docs' created.`

### 11d. Start Caddy

```bash
docker compose -f docker-compose.prod.yml up -d caddy
docker compose -f docker-compose.prod.yml logs -f caddy
```

Watch for these log lines:
```
{"level":"info","msg":"certificate obtained successfully","identifier":"api.example.com"}
{"level":"info","msg":"serving initial configuration"}
```

The first line confirms Let's Encrypt issued a certificate. If it's missing after 2 minutes, see the Troubleshooting table at the end of this guide.

### Check — full stack is running

```bash
docker compose -f docker-compose.prod.yml ps
```
→ All 7 services show `running` (or `Up`): `litellm-db`, `litellm-redis`, `vllm-chat`, `tei-embed`, `tei-rerank`, `litellm`, `qdrant`, `caddy`.

---

## 12. Verify HTTPS from an external network

This is the critical Day 3 exit criterion: confirm the full HTTPS path works from **outside** the server's network — not from the server itself, not from your LAN.

Use your phone (on mobile data, not Wi-Fi), or a cloud machine/VPS.

### 12a. Test HTTPS chat completion (non-streaming)

From an **external device**:
```bash
# Generate a prod virtual key first (run this on the server, not external)
# --- On the server: ---
MASTER_KEY=$(grep LITELLM_MASTER_KEY .env | cut -d= -f2)
curl -X POST http://localhost:4000/key/generate \
  -H "Authorization: Bearer ${MASTER_KEY}" \
  -H "Content-Type: application/json" \
  -d '{
    "models": ["qfind-chat", "qfind-embed", "qfind-rerank"],
    "max_budget": 100.0,
    "rpm_limit": 60,
    "tpm_limit": 100000,
    "budget_duration": "30d",
    "user_id": "prod-test-v1"
  }'
# → Copy the "key": "sk-..." from the output. This is your prod virtual key.
```

Then from the **external device** (phone, laptop on different network):
```bash
curl https://api.example.com/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer sk-YOUR-PROD-VIRTUAL-KEY" \
  -d '{"model":"qfind-chat","messages":[{"role":"user","content":"Hello from external network."}]}'
```
→ Returns a JSON response with the 14B model's answer.

### 12b. Test streaming

```bash
curl https://api.example.com/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer sk-YOUR-PROD-VIRTUAL-KEY" \
  -d '{"model":"qfind-chat","stream":true,"messages":[{"role":"user","content":"Count to three."}]}'
```
→ Returns `data: {...}` SSE events incrementally, ending with `data: [DONE]`.

### 12c. Verify the TLS certificate

```bash
# From external device or your laptop:
curl -v https://api.example.com/v1/models \
  -H "Authorization: Bearer sk-YOUR-PROD-VIRTUAL-KEY" 2>&1 | grep -E "subject:|issuer:|SSL"
```
→ Must show `issuer: C=US; O=Let's Encrypt` and no certificate warnings.

Alternatively, paste `https://api.example.com` into [SSL Labs](https://www.ssllabs.com/ssltest/) for a full certificate grading (A or A+).

### 12d. Run the gateway smoke test

Update the smoke test to point at the prod HTTPS endpoint. Run from the server (using the prod virtual key):
```bash
LITELLM_VIRTUAL_KEY="sk-YOUR-PROD-VIRTUAL-KEY" \
LITELLM_BASE_URL="https://api.example.com" \
~/litellm-deploy/deployment/scripts/smoke-test.sh
```

> The current `smoke-test.sh` hardcodes `localhost:4000`. For a clean prod-targeted run, you can also just test the gateway checks manually with the HTTPS URL at this stage — full smoke-test parameterization is a Day 4/5 polish item.

### Check
- HTTPS chat completion (streaming + non-streaming) succeeds from an external network.
- Certificate is valid, issued by Let's Encrypt, with no warnings.
- Virtual key works; the disallowed model (`gpt-4`) is rejected.

---

## 13. Run the prod smoke test and record VRAM baseline

With the full stack running, capture the production VRAM baseline — the first real measurement of the §11.4 budget.

```bash
nvidia-smi
```

Record the output. Expected approximate allocation on the RTX 5090 (32 GB total):

| Service | Approximate VRAM |
|---|---|
| vLLM — Qwen2.5-14B FP8 | 14–16 GB |
| TEI embed — BGE-M3 | 1.5–2 GB |
| TEI rerank — BGE-reranker-v2-m3 | 1–1.5 GB |
| OS / driver overhead | 0.5–1 GB |
| **Total** | **~17–21 GB of 32 GB** |

This leaves 11–15 GB headroom for KV cache during active inference — sufficient for 20–30 concurrent users at `--max-model-len 16384`.

> **If VRAM is higher than expected:** lower `--gpu-memory-utilization` on vLLM and re-check. If TEI is unexpectedly large, confirm it loaded the correct model (check `docker compose logs tei-embed` for the model path).

---

## 14. Update the .env.example for production

`deployment/.env.example` is the only `.env` file that goes into version control. Update it to document all the new prod variables added today:

```dotenv
# ─── Postgres ────────────────────────────────────────────────────────────────
POSTGRES_USER=litellm
POSTGRES_PASSWORD=change-me          # generate: openssl rand -base64 32
POSTGRES_DB=litellm

# ─── LiteLLM ─────────────────────────────────────────────────────────────────
DATABASE_URL=postgresql://litellm:change-me@litellm-db:5432/litellm

# Master key — admin only, never share, never commit the real value
LITELLM_MASTER_KEY=change-me         # generate: echo "sk-master-$(openssl rand -hex 32)"

# Salt key — used to hash virtual key data in Postgres.
# IMPORTANT: generate once and never change after first run.
LITELLM_SALT_KEY=change-me           # generate: openssl rand -base64 32
```

Commit this update to the repo (it contains only placeholders, no secrets):
```bash
cd ~/litellm-deploy
git add deployment/.env.example
git commit -m "docs: add LITELLM_SALT_KEY and DATABASE_URL to .env.example"
```

---

## 15. End-of-Day 3 Definition of Done

All of the following must be true before Day 4 (monitoring and hardening) begins:

**Server + GPU**
- [ ] RTX 5090 shows in `nvidia-smi` on the prod host with CUDA 12.x.
- [ ] RTX 5090 is visible inside a container (`docker run --gpus all ... nvidia-smi`).

**Stack**
- [ ] All 8 services (`litellm-db`, `litellm-redis`, `vllm-chat`, `tei-embed`, `tei-rerank`, `litellm`, `qdrant`, `caddy`) are `running` in `docker compose -f docker-compose.prod.yml ps`.
- [ ] Every service has `restart: unless-stopped`.
- [ ] Service ports (8000, 4000, 6333) are bound to `127.0.0.1` and not reachable from the public internet.

**DNS, TLS, HTTPS**
- [ ] `dig api.<your-domain>` resolves to the server's public IP.
- [ ] Let's Encrypt certificate issued; Caddy logs confirm.
- [ ] HTTPS chat completion (streaming + non-streaming) succeeds from an **external network** (not localhost, not LAN).
- [ ] Certificate valid; no TLS warnings.

**Firewall**
- [ ] UFW enabled; `sudo ufw status` shows only 22/80/443 open.
- [ ] External port scan confirms no raw service ports are reachable.

**Inference**
- [ ] vLLM running Qwen2.5-14B with FP8 (or AWQ fallback) — confirmed in logs and `nvidia-smi`.
- [ ] TEI embed + rerank running on GPU — confirmed in logs and VRAM reading.
- [ ] VRAM total < 28 GB (leaves 4+ GB headroom); no OOM errors.

**Secrets**
- [ ] Prod `.env` has permissions `600`; contains three fresh secrets (none say "change-me").
- [ ] `LITELLM_SALT_KEY` is present.
- [ ] No `.env` committed to git.

**Smoke test**
- [ ] `smoke-test.sh` direct checks (ports 8000, 8001, 8002) all pass on the prod host.
- [ ] Production virtual key works through HTTPS; disallowed model returns an error.

If all boxes are checked, the production stack is live. Day 4 covers monitoring, alerting, backups, host hardening, and production key management.

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `nvidia-smi` not found on prod server | No driver installed | Follow Step 1c |
| `nvidia-smi` shows old CUDA (< 12.6) | Old driver, incompatible with FP8 | Upgrade to `cuda-drivers-570` as in Step 1c, reboot |
| vLLM OOM on 14B FP8 | `--gpu-memory-utilization` too high, or TEI is consuming more VRAM than expected | Lower `--gpu-memory-utilization` to 0.60; confirm TEI is using expected VRAM |
| FP8 error in vLLM logs | FP8 not stable on this driver/model combo | Switch to AWQ fallback — change `--quantization fp8 --model qwen2.5-14b-instruct` to `--quantization awq --model qwen2.5-14b-instruct-awq`; download the AWQ model |
| Let's Encrypt challenge fails | DNS not propagated, or port 80 blocked | Confirm `dig api.<domain>` returns your IP; confirm UFW allows port 80; Caddy does the HTTP-01 challenge on port 80 before redirecting to 443 |
| Let's Encrypt rate-limited | Too many failed cert requests in 24h | Wait 1 hour before retrying; fix DNS/firewall first; test with `--acme-ca https://acme-staging-v02.api.letsencrypt.org/directory` for staging certs |
| HTTPS request fails with "connection refused" | Caddy not started, or wrong domain in Caddyfile | Confirm `docker compose ps caddy` shows running; confirm the domain in Caddyfile matches the DNS A record exactly |
| HTTPS works from server but not externally | UFW or cloud firewall blocking 443 | Check both UFW (`ufw status`) and any cloud-level security group; confirm 443 is open to the internet |
| LiteLLM "database not ready" | Postgres slow to initialize first time | `docker compose -f docker-compose.prod.yml restart litellm`; Postgres needs a moment on first boot |
| `docker: Error response from daemon: ... nvml error` | NVIDIA Container Toolkit not configured for Docker | Re-run `sudo nvidia-ctk runtime configure --runtime=docker && sudo systemctl restart docker` |
| Virtual key returns 401 despite correct key | `LITELLM_SALT_KEY` mismatch between container and DB | Confirm `LITELLM_SALT_KEY` in `.env` matches what was set when the DB was first initialized; if not, wipe Postgres volume and reinitialize with the correct key |
| Qdrant collection missing on prod | Fresh prod volume; local collection data didn't transfer | Run `python3 qdrant/init_collection.py` from the prod server |

---

## Learnings — what Day 3 teaches

1. **The cutover is a config change, not a rebuild.** Because Days 1–2 used the stable `qfind-chat` alias, the same client code and test scripts work unchanged on production. The model path, quantization flag, and TEI image tag are the only differences. Designing for cutover from Day 1 is what makes Day 3 fast.

2. **FP8 is a production-only feature.** WSL2 doesn't support FP8 — so you never saw it locally. On a native Linux server with driver 570+ and a Blackwell GPU, vLLM's FP8 quantization cuts VRAM usage while preserving quality. If it's unstable on a specific driver/model combo, AWQ is a clean fallback at similar quality. Having the fallback ready before starting is good deployment practice.

3. **Caddy makes HTTPS trivial.** On a traditional stack, TLS certificates require certbot, cron jobs, nginx reload hooks, and a lot of error-prone config. Caddy reduces this to a Caddyfile with the domain name and a volume for cert persistence. Let's Encrypt provisioning is automatic and transparent. The only human requirement is: valid DNS A record pointing to your IP, and port 80 accessible from the internet before starting.

4. **Port binding to `127.0.0.1` is a meaningful security control.** Binding service ports to `127.0.0.1:8000` instead of `0.0.0.0:8000` ensures the OS itself never routes external traffic to that port, even if UFW is misconfigured. This is defense-in-depth: UFW blocks the network, and the bind address blocks the socket. Both layers need to be bypassed for a service to be exposed — much harder to accidentally misconfigure.

5. **`restart: unless-stopped` is the difference between a demo and a service.** Without restart policies, every OS reboot or container crash leaves services down until someone manually intervenes. `unless-stopped` means the stack is self-healing — a crash restarts the container, and a reboot brings everything back up automatically.

6. **The external HTTPS test is non-negotiable.** Testing from `localhost` or the same LAN can mask firewall misconfigurations, DNS propagation issues, and Caddy ACME failures. Testing from a mobile device on cellular data is the only reliable confirmation that the service is actually reachable and the certificate is valid from the perspective of real users.

7. **LITELLM_SALT_KEY must be generated once and never changed.** Unlike the master key (which can be rotated), the salt key is baked into every virtual key record stored in Postgres. Changing it after keys have been issued invalidates all of them. Generate it on Day 3 and treat it like a permanent database encryption key — back it up alongside the Postgres data.

8. **VRAM baseline on Day 3 is the first real capacity data point.** Everything in the execution plan's §11.4 VRAM budget was theoretical until this moment. Record the actual numbers from `nvidia-smi` after the full stack loads. This measurement is what Day 5's concurrency load test builds on — and what sets expectations for when/if the stack needs a second GPU.
