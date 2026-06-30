# Qfind Self-Hosted AI Infrastructure — 1-Week Base Execution Plan

**Source:** `research/qfind_server_deploy.md` (Technical Design Document)
**Team:** 1 developer, full-time, 5 working days (no weekend work assumed)
**Shape:** Phase A (Local Integration & Testing) = Days 1–2 · Phase B (Production Migration & Deployment) = Days 3–5
**Purpose of this document:** a detailed *base plan*. It defines goals, sequenced implementation steps, concrete artifacts, deliverables, and exit criteria per day. A fine-grained task list (with individually checkable sub-tasks) is intended to be derived from this document later.

> **Single-developer constraint (read first).** With one developer, all work within a day is **strictly sequential** — there is no parallel track. This makes the 5-day window aggressive. The plan keeps a 5-day skeleton but explicitly marks **capacity-critical** steps and pushes anything that cannot realistically fit into the "Deferred / Compressed" section. Two scope assumptions make the week feasible: (a) Phase A runs on a GPU comparable to the production RTX 5090, and (b) Qfind's RAG retrieval/embedding code largely **exists** and Day 2 is integration/rewiring, not greenfield development. If either is false, re-baseline before Day 1 (see Assumptions).

**Conventions used below:**
- `api.<company-domain>` — placeholder; real domain is an open question (see Assumptions).
- All services run as containers on a single internal Docker network named `qfind-net`; only Caddy is published to the host's public interface.
- Pinned versions follow the doc's binding constraints: **vLLM v0.17.0+** (Blackwell/SM120 FP8), Ubuntu 22.04/24.04 LTS, CUDA 12.8-class toolchain. Pin LiteLLM, Caddy, Qdrant, TEI, Postgres, Redis to specific tested tags rather than `latest`.
- **Local hardware reality (Phase A):** the local/dev machine is a Windows laptop with an **NVIDIA RTX 4050 (~4–5 GB usable VRAM)** running the stack under WSL2 — far smaller than the production RTX 5090 (32 GB). Phase A therefore runs a **small** chat model (**Qwen2.5-3B-Instruct AWQ**, dropping to **Qwen2.5-1.5B-Instruct AWQ** if VRAM is too tight), served under the stable alias `qfind-chat`, and runs **TEI (embeddings + reranker) on CPU** so they don't compete with vLLM for the scarce GPU. Phase A validates **functionality/plumbing only**. Model quality, FP8, concurrency, and the §11.4 VRAM budget are first validated on the production RTX 5090 in Phase B. On prod, the model becomes **Qwen2.5-14B** and TEI moves back onto the GPU as designed — a config/image change, not an architectural one.

---

## Phase Summary — Document Sections → Phase Mapping

| Phase | Days | Goal | Document sections / components |
|---|---|---|---|
| **A — Local Integration & Testing** | 1–2 | Full stack stood up on local/dev hardware and validated end-to-end as designed | §3 Target Arch · §7 Models · §8 RAG pipeline · §11 VRAM budgets (validated locally) · §17 Dev Roadmap steps 1–10 · §19 Stack |
| **B — Production Migration & Deployment** | 3–5 | Validated stack migrated to remote RTX 5090, hardened, monitored, concurrency-validated for 20–30 users | §9 Deployment Arch · §10 Auth/Keys · §11 Concurrency (re-validated) · §12 Real latency benchmarks · §13 Security · §14 Monitoring · §15.3 Backups · §18 Deployment Roadmap steps 1–12 |

---

## Repository / Artifact Layout (created during the week)

This is the target shape of the deployment repo the week produces. Establishing it early (Day 1) lets every later step land files in a predictable place — and gives the future fine-grained task plan concrete file targets.

```
litellm-deploy/
├── docker-compose.yml              # full stack topology (all services)
├── docker-compose.override.yml     # local-only overrides (Phase A)
├── .env.example                    # documented, committed
├── .env                            # real secrets, gitignored
├── caddy/
│   └── Caddyfile                   # reverse proxy + auto-TLS
├── litellm/
│   └── config.yaml                 # model_list aliases, routing
├── vllm/
│   └── README.md                   # launch flags + quantization notes
├── tei/
│   └── README.md                   # embed + rerank launch flags
├── qdrant/
│   └── config.yaml                 # collection / hybrid search config
├── monitoring/
│   ├── prometheus.yml              # scrape targets
│   ├── loki-config.yml
│   ├── promtail-config.yml
│   └── grafana/                    # provisioning + dashboards (12239)
├── backups/
│   └── backup.sh                   # Postgres dump + Qdrant snapshot → off-site
├── scripts/
│   ├── smoke-test.sh               # curl checks for every endpoint
│   └── loadtest.py                 # 20–30 concurrent request simulator
└── docs/
    └── runbook.md                  # operational runbook (Day 5)
```

---

## Day 1 — Local Foundation: GPU passthrough + inference engines in isolation

**Goal:** On the local/dev laptop (RTX 4050, ~4–5 GB VRAM), the GPU is visible inside containers, and vLLM (small local model, **Qwen2.5-3B-Instruct AWQ**, served as `qfind-chat`) plus TEI (BGE-M3 + BGE-reranker-v2-m3, **on CPU locally**) each answer direct `curl` requests. Repo skeleton and Postgres/Redis are in place. No gateway yet.

### Implementation steps (sequential)
1. **Initialize the deployment repo** with the layout above; commit `.env.example` and a `.gitignore` that excludes `.env`, model caches, and volume data.
2. **Start model-weight downloads first** (they are large and run in the background while host setup proceeds): the **local** chat model **Qwen2.5-3B-Instruct AWQ** (fallback **Qwen2.5-1.5B-Instruct AWQ**), BGE-M3, and BGE-reranker-v2-m3 into a local HuggingFace cache dir that will be bind-mounted into the containers. (Prod downloads Qwen2.5-14B instead — see Cutover Plan.)
3. **Host prep (§17.1):** confirm OS (Ubuntu 22.04/24.04 LTS), install the NVIDIA driver matching a CUDA 12.8-class toolchain, reboot, verify `nvidia-smi` on the host.
4. **Container runtime (§17.2):** install Docker + Docker Compose plugin + NVIDIA Container Toolkit; configure the Docker runtime for GPUs; validate passthrough:
   ```bash
   docker run --rm --gpus all nvidia/cuda:12.8.0-base-ubuntu24.04 nvidia-smi
   ```
5. **Compose scaffold:** create `docker-compose.yml` with the `qfind-net` network and empty service stubs; add Postgres + Redis services (`litellm-db`, `litellm-redis`) with named volumes; bring them up and verify connectivity (`pg_isready`, `redis-cli ping`).
6. **vLLM in isolation (§17.3):** add the `vllm-chat` service pinned to **v0.17.0+**, serving **Qwen2.5-3B-Instruct AWQ** under `--served-model-name qfind-chat`, with tiny-VRAM flags `--gpu-memory-utilization 0.80 --max-model-len 8192 --max-num-seqs 8 --enforce-eager --enable-prefix-caching`, GPU reservation, model cache bind-mount. Validate:
   ```bash
   curl http://localhost:8000/v1/chat/completions -H 'Content-Type: application/json' \
     -d '{"model":"qfind-chat","messages":[{"role":"user","content":"hello"}]}'
   # repeat with "stream": true to confirm SSE streaming
   ```
   If 3B OOMs on the 4050, drop to Qwen2.5-1.5B-Instruct AWQ.
7. **TEI in isolation (§17.4) — on CPU locally:** add `tei-embed` (BGE-M3) and `tei-rerank` (BGE-reranker-v2-m3) using the **CPU** TEI image (no GPU reservation) so they don't consume the 4050's scarce VRAM. Validate `/embed` and `/rerank` with `curl`, including a few representative **Japanese** sentences for a sanity check. (Prod runs both on the GPU per §11.4 — an image/flag swap at cutover.)
8. **Capture a baseline VRAM snapshot** with vLLM (small model) on the GPU and TEI on CPU (`nvidia-smi`) — expect roughly 2–4 GB used on the 4050. This confirms the local plumbing fits; it does **not** represent the prod §11.4 budget, which is validated on the 5090 in Phase B.
9. **Write `scripts/smoke-test.sh`** that runs the chat (stream + non-stream), embed, and rerank `curl`s and exits non-zero on failure — this becomes the reusable validation harness for later days.

### Deliverables
- Committed repo skeleton + Compose file with Postgres/Redis up.
- `nvidia-smi` returns the GPU on host and inside a container.
- vLLM answering `/v1/chat/completions` (streaming + non-streaming).
- TEI answering `/embed` and `/rerank`.
- `scripts/smoke-test.sh` passing against all three engines.

### Validation / exit criteria
- `smoke-test.sh` exits 0: streaming + non-streaming chat succeed; `/embed` returns a vector; `/rerank` returns ordered scores; Japanese inputs produce sensible output.
- All three engines co-resident under the configured VRAM cap with no OOM.

### Risks (from §16)
- *Local GPU is a 4050, not a 5090* — local **cannot** validate FP8, the §11.4 VRAM budget, concurrency, or real model quality; it only proves the plumbing. Mitigation: keep the stack byte-identical except for model size/quant and TEI placement (CPU→GPU); treat Day 3 as the first real GPU validation.
- *First-deployment learning curve* (most of it is in steps 1–6). Mitigation: background the weight downloads; timebox driver/toolkit setup.

---

## Day 2 — Local Gateway + RAG Pipeline + End-to-End Validation

**Goal:** LiteLLM fronts both engines with a scoped virtual key; the RAG retrieval pipeline is wired through Qdrant; the Qfind client points at local LiteLLM; full document-chat works end-to-end locally; a 20–30 concurrent load test holds VRAM budgets.

### Implementation steps (sequential)
1. **Stand up LiteLLM (§17.7):** add the `litellm` service backed by Postgres + Redis; author `litellm/config.yaml`:
   ```yaml
   model_list:
     - model_name: qfind-chat
       litellm_params: { model: openai/Qwen2.5-14B, api_base: http://vllm-chat:8000/v1, api_key: "none" }
     - model_name: qfind-embed
       litellm_params: { model: openai/BGE-M3, api_base: http://tei-embed:80, api_key: "none" }
     - model_name: qfind-rerank
       litellm_params: { model: openai/BGE-reranker-v2-m3, api_base: http://tei-rerank:80, api_key: "none" }
   general_settings:
     master_key: ${LITELLM_MASTER_KEY}
     database_url: ${DATABASE_URL}
   ```
2. **Generate a test virtual key** via `/key/generate`, scoped to `qfind-chat/embed/rerank` with a `max_budget` and `rpm/tpm`; confirm the Day-1 `curl`s succeed **through LiteLLM** (port 4000) using the virtual key instead of hitting engines directly. Extend `smoke-test.sh` with a gateway-routed variant.
3. **Stand up Qdrant:** add the `qdrant` service + volume; create the collection with hybrid (dense + sparse) config matching BGE-M3's output (`qdrant/config.yaml`).
4. **Embedding pipeline (§17.5, §8.5):** wire Qfind's existing file-watch/index hook to (a) chunk with structure-aware + Japanese-sentence-safe boundaries (§8.4), (b) batch-call `qfind-embed`, (c) upsert to Qdrant with metadata (source path, section, mtime) and a stable chunk ID for idempotent re-embed-on-change.
5. **Hybrid retrieval + rerank (§17.6, §8.3):** implement the four-stage pipeline as a standalone, testable component — Lucene BM25 → Qdrant dense → score fusion (RRF or native hybrid) → `qfind-rerank` top-N. Unit-test retrieval quality independent of the chat UI.
6. **Rewire the Qfind client (§17.8):** point the existing OpenAI-compatible HTTP client at the local LiteLLM base URL with the virtual key; remove the Jina/Groq key-entry UI; confirm streaming SSE renders incrementally in the desktop client.
7. **End-to-end test (§17.9):** run the full path (retrieval → rerank → prompt assembly with source-file tagging → streamed chat) from the actual Qfind client against Japanese documents; confirm answers cite source files.
8. **Local load test (§17.10) — plumbing only:** run `scripts/loadtest.py` at a **small** local concurrency (e.g., 3–5) just to prove the gateway handles parallel requests without errors. The 4050 cannot represent 20–30 users; the real 20–30 concurrent test runs on the 5090 (Day 5).

### Deliverables
- `litellm/config.yaml` with the three aliases; a working scoped virtual key.
- `qdrant/config.yaml` + a populated collection from the embedding pipeline.
- Embedding pipeline + hybrid retrieval/rerank component (tested in isolation).
- Qfind client rewired to LiteLLM (Jina/Groq UI removed).
- `scripts/loadtest.py` + a small-concurrency local plumbing report (full 20–30 validation deferred to prod, Day 5).

### Validation / exit criteria
- Virtual-key-authenticated chat completion through LiteLLM uses the local embed + LLM endpoints.
- End-to-end document chat from the Qfind client streams a source-cited answer on a Japanese document.
- At small local concurrency (3–5): no errors, no OOM. Full 20–30 concurrent validation is deferred to the prod 5090 (§11.4, Day 5).

### Risks (from §16)
- *RAG pipeline scope (biggest single-dev risk)* — §17.5–17.6 are real development; serial with everything else. Mitigation: confirm code largely exists pre-Day 1; if greenfield, descope fusion/rerank polish to a minimal dense+rerank path and move full hybrid tuning to Deferred.
- *VRAM contention / OOM* — set per-process caps before the load test; alert threshold reused on Day 4.

---

## Day 3 — Cutover to Production: Infra + Inference live on RTX 5090

**Goal:** The validated stack is deployed on the remote RTX 5090 Linux server, both engines are **re-validated on the actual production GPU**, and HTTPS is reachable from outside the network. This is the Local → Production cutover (see Cutover Plan).

### Implementation steps (sequential)
1. **Server access + GPU driver state:** SSH to the remote host; verify (or install + reboot) the NVIDIA driver + CUDA 12.8-class toolchain; confirm `nvidia-smi` shows the RTX 5090.
2. **Host runtime:** install Docker + Compose + NVIDIA Container Toolkit; re-run the GPU passthrough test container.
3. **DNS (§18.1):** create an `A` record `api.<company-domain>` → server public IP; confirm it resolves externally (`dig`) before requesting TLS.
4. **Firewall (§18.2):** configure UFW to allow only 80, 443, and a restricted-source SSH port; deny everything else; verify with an external port scan that no service ports are reachable.
5. **Prod secrets:** generate **fresh** secrets for prod (LiteLLM master key, `LITELLM_SALT_KEY`, Postgres password) into the prod `.env` — never reuse local dev values.
6. **Engine re-validation on the 5090 (§17.1–17.4 re-run):** bring up `vllm-chat` + `tei-embed` + `tei-rerank`; run vLLM v0.17.0+ with **FP8** (AWQ fallback if unstable); re-tune `--gpu-memory-utilization` for the 5090's 32GB; run `smoke-test.sh` on the prod host.
7. **Deploy Caddy (§18.3):** author `caddy/Caddyfile` reverse-proxying `api.<company-domain>` → `litellm:4000`; bring Caddy up; confirm Let's Encrypt issues a cert and HTTPS works **from an external network** (not localhost).
8. **Deploy the full stack (§18.4):** bring up LiteLLM + Postgres + Redis + Qdrant with `restart: unless-stopped` on every service; reload the embedding collection / re-point Qdrant data as needed.

### Deliverables
- RTX 5090 visible in a container on the prod host; engines re-validated on the 5090.
- DNS `A` record live; UFW locked to 80/443/SSH.
- `caddy/Caddyfile`; valid Let's Encrypt cert; external HTTPS working.
- Full Compose stack running on prod with restart policies; fresh prod secrets.

### Validation / exit criteria
- External (off-LAN) HTTPS chat completion through Caddy → LiteLLM → vLLM succeeds with the prod virtual key.
- Certificate issued and valid; external scan confirms only 80/443/SSH reachable.
- FP8 (or AWQ fallback) confirmed stable on the 5090; `smoke-test.sh` passes on prod.

### Risks (from §16)
- *Blackwell driver/vLLM compatibility — now for real* (High; blocks deploy). Mitigation: pinned v0.17.0+, tested driver+CUDA+vLLM combo, AWQ fallback on standby.
- *Domain/DNS not owned or not propagated* — blocks TLS. Mitigation: confirm ownership pre-Day 1; create the record first.
- *Unknown remote driver state* — may cost time (install + reboot). Mitigation: treat driver verification as the first action.

---

## Day 4 — Production Hardening: Monitoring, Security, Backups, Key Management

**Goal:** Production-grade configuration applied — monitoring live with real data, alerting routed to a monitored channel, backups tested with a restore, host hardened, and real scoped virtual keys issued.

### Implementation steps (sequential)
1. **Monitoring stack (§18.5):** add `prometheus`, `grafana`, `dcgm-exporter`, `loki`, `promtail` services; author `monitoring/prometheus.yml` scraping vLLM, TEI, LiteLLM, and DCGM; import NVIDIA Grafana dashboard **ID 12239**; confirm dashboards populate with live data.
2. **Alerting (§18.6, §14.3):** configure Grafana alerts for VRAM sustained >90%, vLLM/TEI down or failing health checks, LiteLLM error-rate spike, and low host disk; route to a channel the team actually monitors; force a test condition to confirm an alert fires.
3. **Host hardening (§13.5):** enable automatic OS security updates; install + configure `fail2ban` on SSH; confirm every internal service binds only to `qfind-net` (no public port other than Caddy); record the disk-encryption (LUKS) decision (host-level — see Deferred).
4. **Backups (§18.7, §15.3):** author `backups/backup.sh` (Postgres dump + Qdrant snapshot) to off-site/object storage via Restic or cron+rsync; schedule it; **perform a test restore** into a scratch location to prove the backup is usable.
5. **Production key management (§18.8, §10):** issue production virtual keys scoped to `qfind-chat/embed/rerank`, each with `max_budget`, `rpm_limit`/`tpm_limit`, `budget_duration`, and a `user_id`; confirm the master key is stored securely and never distributed; verify **immediate revocation** of a key without affecting others.
6. **Defense-in-depth (§10.3):** add Caddy-level per-source-IP connection rate limiting in the Caddyfile.

### Deliverables
- Live Grafana dashboards (GPU via 12239, latency, errors); at least one alert proven to fire.
- Hardened host (fail2ban, auto-updates, internal-only service binding).
- `backups/backup.sh` scheduled + a **verified restore**.
- Production scoped virtual keys + demonstrated revocation; secured master key.

### Validation / exit criteria
- DCGM dashboard shows real VRAM/utilization; forced test condition triggers an alert to the team channel.
- Restore reproduces Postgres + Qdrant state.
- A scoped key calls only its allowed models and is rejected on others; revocation is immediate.

### Risks (from §16)
- *Monitoring setup time* (§14.4: "a few focused days") vs. part of one day, single dev. Mitigation: start from prebuilt dashboard 12239; defer custom dashboards.
- *LiteLLM bugs, no SLA* — pin a tested version; document the "point Qfind directly at vLLM/TEI" fallback in the runbook.
- *Secrets handling* — master key / DB creds in restricted-permission `.env` outside version control (§10.4).

---

## Day 5 — Concurrency Validation, Pilot Launch, External Access, Go-Live

**Goal:** Production validated for 20–30 concurrent users on the real 5090; internal pilot launched and monitored; external access enabled with confirmed security posture; runbook written; go-live checklist signed off.

### Implementation steps (sequential)
1. **Production load test (§18.9 prep, §17.10 on prod):** run `scripts/loadtest.py` at 20–30 concurrent against the prod endpoint on the real 5090; capture **real** TTFT and tokens/sec to replace §12's qualitative estimates; confirm VRAM stable and no OOM under the §11.4 budget.
2. **Internal pilot launch (§18.9):** distribute pilot keys to the identified internal pilot group via the team's secure channel; begin active monitoring of dashboards.
3. **External access (§18.10):** re-confirm firewall + (optional) Cloudflare Tunnel/Tailscale posture (§13.1) **before** enabling external traffic; enable external access only after the prod load test passes.
4. **Runbook (§18.11):** write `docs/runbook.md` covering: restart a failed service, roll back a model upgrade, rotate the LiteLLM master key, restore from backup, and the LiteLLM→direct-engine fallback.
5. **Maintenance cadence (§18.12):** document the routine — OS security patches, pinned Docker image upgrades tested off-prod first, periodic dashboard/usage review.

### Deliverables
- Prod load-test report (20–30 concurrent on the 5090) with measured TTFT/throughput.
- Internal pilot running with real keys under active monitoring.
- External access enabled with confirmed posture.
- `docs/runbook.md` + maintenance cadence.

### Validation / exit criteria
- See the Go-Live Checklist below.

### Risks (from §16)
- *Pilot can't be "several days" inside Day 5* — go-live = pilot **launched + monitored**; full external rollout gated on a post-Day-5 observation window (see Deferred).
- *External-user latency from network distance* (§12.3) — set expectations per §12.4; evaluate Cloudflare proxying as a partial mitigation.
- *Scope creep toward Kubernetes/multi-node* — out of scope; follow §11.6 escalation order only when the GPU is the proven bottleneck.

---

## Cutover Plan — Local → Production (executed Day 3, hardened Days 4–5)

| Action | Items |
|---|---|
| **Migrated as-is (definitions, not state)** | `docker-compose.yml` topology; vLLM/TEI model choices + launch flags; LiteLLM `model_list` alias schema; `Caddyfile` template; monitoring/alert definitions; RAG pipeline code; `scripts/` harnesses |
| **Reconfigured, NOT copied** | **Local small chat model (Qwen2.5-3B-Instruct AWQ) → production Qwen2.5-14B** (same `qfind-chat` served alias, so configs/tests are unchanged); **TEI moved from CPU (local) to GPU (prod)**; quantization **AWQ on local/WSL2 → FP8 with AWQ fallback on prod**; tiny-VRAM vLLM flags (`--max-model-len`, `--max-num-seqs`, `--enforce-eager`) relaxed/removed for the 5090; all secrets **regenerated** for prod (master key, `LITELLM_SALT_KEY`, Postgres password) — never reuse dev secrets; prod `.env`; real domain + DNS `A` record; Caddy auto-TLS for the real domain; UFW rules; NVIDIA driver/CUDA for the actual 5090; `--gpu-memory-utilization` re-tuned for 32GB; production **virtual keys regenerated** for real users (dev key discarded); Prometheus scrape targets → prod services; off-site backup credentials |
| **Re-validated on prod hardware before "production-grade" is declared** | vLLM + TEI on Blackwell/5090 (FP8 or AWQ fallback); end-to-end HTTPS from an external network; 20–30 concurrent load test on the real GPU; backup **restore** test; alert firing; key scoping + revocation |

Production is **not** a copy of the local checklist: it adds TLS, firewalling, real key issuance, monitoring/alerting, backups, host hardening, and concurrency validation that local testing did not require.

---

## Go-Live Checklist (end of Day 5) — tied to §9, §10, §13, §14

**Deployment (§9)**
- [ ] Full Compose stack running on the 5090 host with `restart: unless-stopped` on every service.
- [ ] Caddy is the only public-facing service; all others bound to `qfind-net`.
- [ ] HTTPS works end-to-end from an external network; Let's Encrypt cert issued and auto-renewing.
- [ ] vLLM + TEI re-validated on the actual RTX 5090.

**Auth & Key Management (§10)**
- [ ] Master key stored securely, never distributed, not in version control.
- [ ] Production virtual keys issued, scoped to allowed models, with `max_budget` + `rpm/tpm` + `budget_duration`.
- [ ] Immediate key revocation verified.

**Security (§13)**
- [ ] UFW: only 80/443 + restricted-source SSH reachable; verified by external scan.
- [ ] `fail2ban` + automatic OS security updates enabled.
- [ ] External-access posture (firewall / optional Cloudflare Tunnel / Tailscale) re-confirmed before external traffic enabled.
- [ ] TLS in transit confirmed; secrets in restricted-permission `.env`.

**Monitoring (§14)**
- [ ] Grafana populated with live GPU (DCGM 12239), latency, and error data.
- [ ] Alerts configured + proven to fire, routed to a monitored channel.
- [ ] Backups to off-site storage with a **tested restore**.

**Capacity & Readiness**
- [ ] 20–30 concurrent load test passed on the real GPU; VRAM stable, no OOM.
- [ ] Real TTFT/throughput benchmarked and recorded (replacing §12 estimates).
- [ ] Internal pilot launched and under active monitoring.
- [ ] `docs/runbook.md` complete (restart · roll back model · rotate master key · restore backup · direct-engine fallback).

> **Go-live definition for this week:** internal pilot launched + production validated. Per §18.9, full external rollout is gated on a multi-day pilot observation window beyond Day 5.

---

## Deferred / Compressed (and why)

| Item | Disposition | Reason |
|---|---|---|
| Multi-day internal pilot (§18.9) | Launched Day 5, observation continues post-week | Doc requires "several days"; can't complete in a 5-day build week |
| Full external rollout (§18.10) | Gated on the pilot window | Depends on pilot results |
| Full hybrid-search tuning (§8.3) | Minimal path in Day 2; tuning deferred | Single-dev capacity; ship dense+rerank first if hybrid fusion is greenfield |
| SGLang re-evaluation (§6.1) | Deferred | Explicitly a post-launch optimization against real traffic |
| STT/TTS (Faster-Whisper, Kokoro/Qwen3-TTS) (§7.4) | Out of scope | Doc marks as optional future work |
| Cloudflare Tunnel / Tailscale Funnel (§9.4, §13.1) | Evaluate Day 5, optional | Optional hardening, not required for initial deployment |
| Disk encryption / LUKS (§13.3) | Flag as host decision | May require host reinstall; confirm before Day 1 if required |
| HashiCorp Vault (§10.4) | Deferred to `.env` + restricted perms | Future upgrade, not needed at this scale |

---

## Single-Developer Capacity Notes

Because there is no second developer, the following are the realistic pressure points; the future fine-grained task plan should treat them as the schedule's critical path:
- **Day 2 is the densest day** (gateway + Qdrant + embedding pipeline + retrieval + client rewire + end-to-end + load test). If Day 2 slips, push the local load test (step 8) into Day 3 morning before cutover.
- **Day 3 mixes networking and GPU re-validation**, which a single dev runs serially — budget for a possible driver install + reboot eating the morning.
- **Day 4 monitoring** is the most likely overflow against §14.4's "few focused days"; the prebuilt dashboard 12239 is the compression lever.
- **Local hardware can't prove capacity.** Because the 4050 only validates plumbing, Days 3 and 5 carry the *real* inference-validation load (FP8/AWQ on the 5090, VRAM budget, 20–30 concurrency). Don't let either get squeezed.
- If two of these slip, **drop full external rollout to post-week** (already deferred) and protect the internal-pilot + production-validation outcome as the week's minimum viable go-live.

---

## Assumptions & Open Questions (confirm BEFORE Day 1)

Gaps in or inferences beyond the source document. Several can break the timeline if wrong.

1. **Local dev GPU is a laptop RTX 4050 (~4–5 GB VRAM) — confirmed, not open.** Phase A runs a small model (Qwen2.5-3B-Instruct AWQ, served as `qfind-chat`) with **TEI on CPU**, validating *plumbing/functionality only*. Consequence: model quality, the §11.4 VRAM budget, FP8, and 20–30 concurrency are first validated on the production 5090 (Days 3 & 5), **not** locally. This is expected, not a defect to fix — but it shifts the real inference-validation weight onto Day 3 (engine re-validation) and Day 5 (concurrency). Keep the stack identical except model size/quant and TEI placement so the cutover stays a config change.
2. **Remote server access.** SSH credentials, sudo rights, and reachability to the 5090 server are not specified. *Confirm before Day 3.*
3. **Domain ownership & DNS control.** §18.1 assumes a company domain and the ability to create records. Ownership/registrar access/current DNS state are not given. *Confirm who controls `api.<company-domain>`.*
4. **Remote GPU driver / CUDA state.** Whether the Blackwell-capable driver + CUDA 12.8 toolchain is preinstalled is unknown; a clean install + reboot may consume Day 3 morning.
5. **Qfind codebase state (scope risk for Day 2).** §17.8 frames the client change as configuration, but §17.5–17.6 (embedding pipeline, hybrid retrieval + rerank) are real development. Plan assumes this scaffolding **largely exists**. *If greenfield, Day 2 overflows — re-baseline.*
6. **vLLM version inconsistency in the source doc.** §16/§6.1 require **v0.17.0+** for Blackwell; §19 says **v0.5.0+**. Plan treats v0.17.0+ as binding. *Confirm the intended pin.*
7. **Off-site backup target.** §15.3/§18.7 assume off-site/object storage exists. Provider, bucket, credentials not specified. *Provision before Day 4.*
8. **No existing CI/CD.** Doc describes manual Compose deployment; plan assumes manual deploys, no pipeline work included.
9. **Model weight download bandwidth.** Qwen2.5-14B + BGE models are large; download time on local and remote networks is unknown. Plan front-loads downloads (Day 1 / Day 3 morning).
10. **Pilot group + secure key distribution channel.** §18.8 assumes "whatever secure channel the team already uses." Pilot user list + channel not specified. *Identify before Day 5.*
11. **Cloudflare/Tailscale accounts.** Optional ingress hardening (§9.4) assumes accounts exist if pursued. Not required for go-live; Day 5 evaluation only.
