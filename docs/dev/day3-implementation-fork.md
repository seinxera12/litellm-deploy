# Day 3 — Implementation Fork: What Actually Happened

**Purpose of this document:** a factual record of how the Day 3 deployment diverged from the plan in `docs/dev/day3.md`. Written for the Day 4 planner agent so it can build the next day's plan against the actual system state, not the original spec.

**Reading convention:**
- **Planned** = what `day3.md` specified.
- **Actual** = what was executed and what state was confirmed.
- **Open item** = unresolved work or decisions that Day 4 must address.
- **Consequence** = downstream impact on Day 4+ planning.

---

## 1. Network topology — fundamental architecture change

**Planned:** server has a direct public IP; Caddy binds `0.0.0.0:80/443`, obtains a Let's Encrypt cert via HTTP-01 challenge; `api.<company-domain>` or `litellm.seinxera.com` points at that IP via a standard A record.

**Actual:** the server is a physical device behind NAT (`wlp7s0`, private IP `192.168.1.226`, DHCP-leased). No router admin access was confirmed. Port forwarding to the public IP was never available. Public-IP-facing DNS was not viable. The entire Caddy + Let's Encrypt + custom domain path was replaced with **Tailscale Funnel**.

- Funnel exposes LiteLLM port 4000 directly to the public internet.
- TLS is Tailscale-managed — no local certificate files, no ACME protocol.
- Public endpoint is a `ts.net` subdomain: `https://ubuntu.tailcd8da4.ts.net`
- External chat completions (streaming + non-streaming) and embeddings confirmed working from a genuinely external network.

**Open item:** `litellm.seinxera.com` or any custom domain is **not** in use. Funnel does not support custom domains. If a custom domain is a hard business requirement, that path requires revisiting port-forwarding (router access + static/DDNS IP) — both remain unconfirmed. Flag this explicitly for the Day 4 planner: is the custom domain a hard requirement or was it incidental to the plan?

**Consequence for Day 4:** all documentation, runbook entries, and smoke-test URLs must reference `https://ubuntu.tailcd8da4.ts.net`, not `api.<company-domain>`. Monitoring stack (Prometheus, Grafana) targets should use the Funnel URL or internal Docker network addresses. Day 4's Caddyfile rate-limiting work (per the original plan §8) is currently inapplicable — see item #2.

---

## 2. Caddy — removed from the active traffic path

**Planned:** Caddy is the sole public-facing reverse proxy, terminating TLS and forwarding to LiteLLM.

**Actual:** Caddy is not in the active traffic path. Funnel connects directly to LiteLLM on port 4000. The `caddy` service is currently running in Docker but serves no live traffic.

**Current state of `Caddyfile`:** the file at `deployment/caddy/Caddyfile` reflects the real domain that was configured (`litellm.seinxera.com`), confirming deployment did begin down the Caddy path before pivoting:
```caddyfile
litellm.seinxera.com {
    reverse_proxy litellm:4000
    header { ... }
    log { output stdout; format json }
}
```
This file is no longer functionally relevant but remains on disk.

**Action item (not yet done):** stop the idle `caddy` container (`docker compose -f docker-compose.prod.yml stop caddy`) to avoid future confusion about what is actually serving traffic.

**Open decision for Day 4:** two options —
- **Option A — Retire Caddy:** remove or comment out the `caddy` service from `docker-compose.prod.yml`; document that Funnel handles ingress.
- **Option B — Reintroduce Caddy as an internal layer:** keep Caddy as a local reverse proxy sitting between Funnel and LiteLLM, enabling per-IP rate limiting (the `caddy-ratelimit` module deferred in Day 3 §8) and security headers. Funnel would forward to Caddy → Caddy to LiteLLM. This restores the hardening originally planned for Day 4, at the cost of one more hop.

Option B is architecturally cleaner for Day 4 hardening if rate limiting is required. The planner should pick one and commit.

---

## 3. UFW firewall rules — may need revisiting

**Planned:** UFW allows 22 (SSH), 80, 443, default-deny.

**Actual:** UFW was configured exactly as planned — SSH, 80/tcp, 443/tcp allowed. **However,** an additional rule was added that was not in the Day 3 plan:

```bash
sudo ufw allow in on tailscale0
```

This rule permits all inbound traffic on the Tailscale interface (`tailscale0`), which is how Funnel's relay traffic reaches the host. Without it, Funnel would not have worked.

**Current UFW state (as configured):**
```
22/tcp        ALLOW    Anywhere
80/tcp        ALLOW    Anywhere
443/tcp       ALLOW    Anywhere
tailscale0    ALLOW IN Anywhere
```

**Issue:** ports 80 and 443 are open to the public internet but nothing is listening on them (Caddy is idle). These rules are now unnecessary and represent minor unnecessary exposure.

**Action item for Day 4:** run `sudo ufw status verbose` to confirm current state, then decide whether to close 80 and 443. If Caddy is retired (Option A from item #2), they should be closed. If Caddy is reintroduced (Option B), 443 should remain. Port 80 is only needed for Let's Encrypt HTTP-01 challenges — since Funnel handles TLS, port 80 has no current function and can be closed regardless.

**Status:** not yet verified post-pivot. UFW was last confirmed during setup, not re-audited after the Funnel decision.

---

## 4. NVIDIA driver — confirmed, no deviations

**Planned:** verify driver; install `cuda-drivers-570` if needed; confirm RTX 5090 visible with CUDA ≥ 12.6.

**Actual:** driver was already installed and no changes were needed.

Confirmed state:
```
nvidia-driver-595-open   595.58.03   (Ubuntu 24.04 package)
NVRM version: 595.58.03 — Tue Mar 17 19:55:10 UTC 2026
```

Note: driver version is **595**, not the 570 specified in the plan. 595 is newer and fully supports Blackwell/SM120/FP8. This is not a problem — it is strictly better.

**Consequence:** the plan's `cuda-drivers-570` install command in §1c is not what's running. Runbook entries should reference the actual driver (595-open) and Ubuntu 24.04 (not 22.04 as the plan hedged on).

---

## 5. Dual-GPU server — unplanned, requires explicit pinning

**Planned:** single-GPU assumption throughout; no GPU selection logic in the Compose file.

**Actual:** the server has **two GPUs**:
```
01:00.0  NVIDIA RTX A400      (GA107GL — display/console GPU)
09:00.0  NVIDIA GeForce RTX 5090  (GB202 — target compute GPU)
```

This was never anticipated. vLLM was confirmed to have selected the RTX 5090 (verified via `nvidia-smi` output during testing), but this was verified after the fact, not engineered. The current `docker-compose.prod.yml` uses `count: all` with no device ordering constraint:
```yaml
deploy:
  resources:
    reservations:
      devices:
        - { driver: nvidia, count: all, capabilities: [gpu] }
```

**Risk:** a future driver update or reboot could change the device enumeration order, causing vLLM to bind to the A400 instead of the 5090. The A400 is a workstation card with ~12 GB VRAM — insufficient for Qwen2.5-14B FP8.

**Action item for Day 4:** add explicit GPU pinning to the `vllm-chat` service in `docker-compose.prod.yml`:
```yaml
environment:
  - CUDA_DEVICE_ORDER=PCI_BUS_ID
  - CUDA_VISIBLE_DEVICES=1   # RTX 5090 is at PCI bus 09:00.0; verify device index
```
Confirm the correct device index by running `nvidia-smi -L` and cross-referencing with `lspci` output on the server.

---

## 6. vLLM FP8 — confirmed working, no AWQ fallback needed

**Planned:** attempt FP8 first; fall back to AWQ if unstable.

**Actual:** FP8 quantization on the RTX 5090 confirmed working with driver 595 and vLLM v0.17.0. No fallback was triggered.

Current `docker-compose.prod.yml` vLLM command (as deployed):
```yaml
command: >
  --model /models/qwen2.5-14b-instruct
  --quantization fp8
  --served-model-name qfind-chat
  --gpu-memory-utilization 0.70
  --max-model-len 16384
  --tensor-parallel-size 1
  --enable-prefix-caching
```

**Note:** a driver-order warning was present in vLLM logs due to the dual-GPU situation (item #5), but it did not affect functionality. This warning would be eliminated by adding explicit `CUDA_VISIBLE_DEVICES` pinning.

---

## 7. Port 8000 conflict — stray process found and killed

**Planned:** no mention of pre-existing processes; assumed clean server state.

**Actual:** a pre-existing Python process (from a `ChatbotAPI` project, running before this deployment) was bound to `0.0.0.0:8000`, blocking vLLM's port binding. This also meant port 8000 had been publicly exposed (not loopback-restricted) for an extended period before the deployment began.

**Resolution:** process was killed; no systemd or cron auto-restart was tied to it. Port was freed for vLLM.

**Consequence for future deployments:** the server was not in a provably clean state when Day 3 began. The assumption of a fresh server was wrong. **For any future redeployment or Day 4 setup work, run `ss -tlnp` before bringing up new services to audit all host-level bound ports.** Add this check to the runbook.

---

## 8. TEI (embeddings + reranker) — GPU support gap, running on CPU

**Planned:** both `tei-embed` and `tei-rerank` run on the GPU in production using the GPU-variant TEI image (`ghcr.io/huggingface/text-embeddings-inference:1.5`, no `cpu-` prefix).

**Actual:** two distinct failures blocked GPU TEI:

**Failure A — Blackwell compute capability not supported by TEI GPU images:**
The RTX 5090 (SM120 / Blackwell architecture) is not supported by any tested TEI GPU image tag (`1.5`, `cuda-1.9`). Both failed with:
```
Runtime compute cap 120 is not compatible with compile time compute cap 80
```
This is a known, unresolved gap in the HuggingFace TEI ecosystem, tracked in [huggingface/text-embeddings-inference#652](https://github.com/huggingface/text-embeddings-inference/issues/652). No verified working GPU image tag for SM120 was found.

**Failure B — Reranker model format mismatch (independent of GPU issue):**
The pre-downloaded reranker model directory (`model-cache/bge-reranker-v2-m3-onnx`) contained only ONNX-format weights. TEI's Candle backend (used by both CPU and GPU images) expects raw `safetensors` format. This caused the reranker to fail regardless of whether GPU or CPU image was used.

**Resolution:** both `tei-embed` and `tei-rerank` fell back to `cpu-1.5`. The volume mount for `tei-rerank` was pointed at `bge-reranker-v2-m3-onnx` (the ONNX directory). Whether the CPU image successfully loaded the ONNX model or whether raw safetensors were downloaded separately needs explicit confirmation — the reranker's working state via LiteLLM has **not yet been end-to-end verified externally** (see item #12).

**Current `docker-compose.prod.yml` state for TEI services (as deployed):**
```yaml
tei-embed:
  image: ghcr.io/huggingface/text-embeddings-inference:cpu-1.5
  command: ["--model-id", "/models/bge-m3"]
  volumes:
    - ./model-cache/bge-m3:/models/bge-m3:ro
  ports: ["8001:80"]
  networks: [qfind-net]

tei-rerank:
  image: ghcr.io/huggingface/text-embeddings-inference:cpu-1.5
  command: ["--model-id", "/models/bge-reranker-v2-m3"]
  volumes:
    - ./model-cache/bge-reranker-v2-m3-onnx:/models/bge-reranker-v2-m3:ro
  ports: ["8002:80"]
  networks: [qfind-net]
```

Differences from the Day 3 plan's intended prod config:
- `cpu-1.5` image instead of `1.5` (GPU image)
- No `deploy.resources.reservations.devices` block (CPU needs none)
- No `restart: unless-stopped` on either TEI service (these were omitted from the current file)
- No `--max-batch-tokens` or `--max-concurrent-requests` flags (GPU tuning flags, not needed on CPU)
- Port bindings are `8001:80` / `8002:80` (not `127.0.0.1:8001:80`) — **TEI ports are not loopback-restricted, unlike vLLM and LiteLLM**

**Capacity assessment:** the host CPU (Ryzen 9, 128 GB RAM) is assessed as sufficient for BGE-M3 + reranker at expected steady-state request volumes. Both models are small; RAM is not a constraint. GPU TEI would only matter for high-throughput bulk corpus re-embedding, not query-time latency at 20–30 concurrent users.

**Open items for Day 4:**
1. **Reranker verification:** confirm `tei-rerank` is actually serving requests correctly (the ONNX vs. safetensors ambiguity is unresolved). Run a direct `curl http://localhost:8002/rerank` on the server and verify a scored response.
2. **Add `restart: unless-stopped`** to both TEI services.
3. **Loopback-restrict TEI ports** (change `"8001:80"` → `"127.0.0.1:8001:80"` and same for 8002) to match the security posture of the other services.
4. **Track [TEI issue #652](https://github.com/huggingface/text-embeddings-inference/issues/652):** re-evaluate moving TEI to GPU once a verified SM120-compatible image tag is released. This is not blocking production but is a meaningful performance and VRAM budget divergence from the original design.

---

## 9. Qdrant — pinned version not applied

**Planned:** `qdrant/qdrant:v1.9.0` (pinned version).

**Actual:** `qdrant/qdrant:latest` is in `docker-compose.prod.yml`. This was carried over from `docker-compose.yml` (the local dev file) without being updated for production.

**Action item for Day 4:** pin to a specific version. Check what version is currently running (`docker inspect qdrant | grep -i image`), pin that version tag in `docker-compose.prod.yml`.

**Status:** Qdrant collection has not been initialized on the production server. `qdrant/init_collection.py` has not been run against the prod Qdrant instance. No documents have been ingested.

---

## 10. Secrets and `.env.example` — partially complete

**Planned:** generate three fresh secrets (POSTGRES_PASSWORD, LITELLM_MASTER_KEY, LITELLM_SALT_KEY); chmod 600 .env; update `.env.example` with all three documented.

**Actual:** secrets were generated and `.env` was created with correct permissions. However:
- `.env.example` still reflects the old format — it documents `LITELLM_VIRTUAL_KEY` (a generated virtual key, which does not belong in `.env.example`) and is missing `LITELLM_SALT_KEY`.
- Current `.env.example`:
  ```dotenv
  POSTGRES_USER=litellm
  POSTGRES_PASSWORD=your-password
  POSTGRES_DB=litellm
  DATABASE_URL=postgresql://litellm:your-password@litellm-db:5432/litellm
  LITELLM_MASTER_KEY=sk-master-key
  LITELLM_VIRTUAL_KEY=sk-virtual-key   # ← wrong: virtual key is runtime-generated, not config
  # missing: LITELLM_SALT_KEY
  ```

**Action item for Day 4:** update `.env.example` to match the Day 3 plan's §14 template — add `LITELLM_SALT_KEY`, remove `LITELLM_VIRTUAL_KEY`, add generation instructions as comments.

---

## 11. Compose file — missing `restart: unless-stopped` on TEI services

**Planned:** every service gets `restart: unless-stopped`.

**Actual:** `restart: unless-stopped` is present on `litellm-db`, `litellm-redis`, `vllm-chat`, `litellm`, `qdrant`, and `caddy`. It is **absent** from `tei-embed` and `tei-rerank`.

**Action item for Day 4:** add `restart: unless-stopped` to both TEI services in `docker-compose.prod.yml`.

---

## 12. Validation checklist — partially complete

Items confirmed working:

| Item | Status |
|---|---|
| RTX 5090 visible in `nvidia-smi` on host | ✅ Confirmed |
| RTX 5090 visible inside a container | ✅ Confirmed (implied by vLLM GPU use) |
| vLLM FP8 on RTX 5090 | ✅ Confirmed |
| vLLM chat completions — non-streaming | ✅ Confirmed (direct + via LiteLLM) |
| vLLM chat completions — streaming | ✅ Confirmed (direct + via LiteLLM) |
| External access via Funnel (HTTPS) | ✅ Confirmed |
| LiteLLM embeddings — external | ✅ Confirmed |
| UFW active | ✅ Confirmed (with Tailscale rule added) |
| Fresh prod secrets generated | ✅ Confirmed |
| Docker + NVIDIA Container Toolkit | ✅ Confirmed |

Items **not yet confirmed:**

| Item | Gap |
|---|---|
| Reranker (`tei-rerank`) end-to-end via LiteLLM | ONNX vs. safetensors ambiguity unresolved; no external curl test recorded |
| Qdrant collection initialized | `init_collection.py` not run on prod |
| Qdrant with real ingested data | No documents embedded/ingested |
| Full smoke-test (`smoke-test.sh`) on prod | Not run against prod Funnel URL |
| UFW rule audit post-pivot | Not re-checked after switching to Funnel |
| Caddy stopped/removed | Still running idle |
| `restart: unless-stopped` on TEI services | Missing |
| TEI ports loopback-restricted | Currently `0.0.0.0`-bound |
| Qdrant version pinned | Still on `latest` |
| `CUDA_VISIBLE_DEVICES` pinned in Compose | Not hardened |
| `.env.example` updated | Outdated format |

---

## 13. TLS and certificate management — no local certs, Tailscale-managed

**Planned:** Caddy-managed Let's Encrypt certs stored in the `caddy-data` Docker volume; backup this volume as part of the secrets/backup routine.

**Actual:** TLS is fully managed by Tailscale for the Funnel endpoint. There are no local certificate files, no `caddy-data` volume content to back up, and no cert renewal process to maintain.

**Consequence:** the Day 4 backup plan should **not** include `caddy-data`. It should include only `pgdata` (LiteLLM key/usage/budget state) and Qdrant storage (once documents are ingested). The Day 3 guide's §14 (`caddy-data` note: "persists TLS certs — never delete this volume") is irrelevant to the current setup.

---

## 14. Funnel setup — not covered in original plan, needs documenting

The Day 3 guide has no section for Tailscale Funnel. The following is the actual setup sequence used, for the runbook and Day 4 troubleshooting reference:

**Prerequisites completed (before Funnel worked):**
1. HTTPS certificates enabled on the tailnet (Tailscale admin console → DNS → Enable HTTPS).
2. Funnel ACL grant confirmed for the node in the tailnet policy.
3. `tailscale funnel --bg 4000` run on the server.
4. Verified with `tailscale funnel status` — confirmed active.
5. Confirmed with `curl https://ubuntu.tailcd8da4.ts.net/v1/models` from external network.

**Troubleshooting entries for Funnel (not in the original guide):**

| Symptom | Likely cause | Fix |
|---|---|---|
| `tailscale funnel status` shows "No serve config" | Funnel not started | Re-run `tailscale funnel --bg 4000` |
| HTTPS cert not available | HTTPS certs not enabled on tailnet | Tailscale admin console → DNS → Enable HTTPS → wait for propagation |
| External curl returns 403 or "Funnel not enabled" | Funnel ACL grant missing for this node | Tailscale admin console → Access Controls → confirm node has Funnel permission |
| Funnel stops after reboot | `--bg` flag not persistent across reboots by default | Add `tailscale funnel --bg 4000` to a systemd unit or cron `@reboot` |
| External request reaches Funnel but LiteLLM returns 401 | Virtual key not valid or expired | Re-generate a virtual key; confirm `LITELLM_MASTER_KEY` is correct in `.env` |

**Open item:** Funnel persistence across reboots has not been verified or automated. Since `restart: unless-stopped` brings Docker services back up after a reboot, but the `tailscale funnel` process may not resume automatically, a reboot would leave the stack running but externally unreachable. **Action item for Day 4:** add a `@reboot tailscale funnel --bg 4000` cron entry or a systemd service to ensure Funnel resumes automatically.

---

## Summary — What Day 4 inherits

**Working:**
- Full Docker Compose stack running on the RTX 5090 server (vLLM FP8 + TEI CPU + LiteLLM + Postgres + Redis + Qdrant + Caddy idle).
- External HTTPS access via Tailscale Funnel (`https://ubuntu.tailcd8da4.ts.net`).
- Chat completions (streaming + non-streaming) and embeddings confirmed externally.
- UFW active with Tailscale interface allowed.

**Needs immediate action before Day 4 work begins:**
1. Stop idle Caddy container; decide retire vs. reintroduce (§2).
2. Verify reranker (`tei-rerank`) is actually serving (§8).
3. Run `sudo ufw status verbose`; close ports 80/443 if Caddy is retired (§3).
4. Run `qdrant/init_collection.py` on prod to initialize the collection (§9).
5. Confirm `tailscale funnel --bg 4000` persists across reboots (§14).

**Structural gaps to fix in `docker-compose.prod.yml`:**
- Add `restart: unless-stopped` to `tei-embed` and `tei-rerank`.
- Add loopback bind to TEI ports (`127.0.0.1:8001:80`, `127.0.0.1:8002:80`).
- Add `CUDA_VISIBLE_DEVICES` pinning to `vllm-chat`.
- Pin Qdrant image version (replace `latest`).

**Deferred from Day 3 (carry into Day 4 planning):**
- Monitoring stack (Prometheus, Grafana, DCGM, Loki) — not started.
- Backups (`pgdata` dump routine) — not yet operationalized.
- Production virtual key issuance for real users — not done.
- Reranker GPU support — blocked on upstream TEI Blackwell support; track issue #652.
- Custom domain (`litellm.seinxera.com`) — blocked on router access; open business decision.
- `.env.example` update.
- Full smoke-test run against prod Funnel URL.
