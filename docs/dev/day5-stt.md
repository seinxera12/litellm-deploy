# Day 5 Addendum — Server-Side Speech-to-Text (faster-whisper, OpenAI-compatible)

**Context:** Days 1–4 stood up chat (vLLM, fp8 14B on the RTX 5090), embeddings + reranker (TEI, CPU), the LiteLLM gateway, monitoring, and backups. The client currently runs STT locally. This addendum moves transcription to the server so it's centrally hosted, GPU-accelerated, and reachable through the same LiteLLM gateway/API-key scheme as everything else.

**Server topology reminder (from Day 4):** the box has **two GPUs** — an A400 (index 0, currently idle — no service uses it) and the RTX 5090 (index 1, pinned to `vllm-chat` via `CUDA_VISIBLE_DEVICES`). **Put STT on the A400.** It gives transcription a dedicated GPU with zero contention against the chat model's VRAM/compute budget, and avoids re-tuning vLLM's `--gpu-memory-utilization`.

---

## 0. Model and engine choice

- **Engine:** `faster-whisper` (CTranslate2 backend) — 4–8x faster than stock `openai-whisper` on the same hardware, standard choice for GPU-hosted Whisper.
- **Serving layer:** don't hand-roll a FastAPI wrapper. Use **[speaches](https://github.com/speaches-ai/speaches)** (formerly `faster-whisper-server`) — a maintained OpenAI-API-compatible server built directly on `faster-whisper`. It exposes `/v1/audio/transcriptions` and `/v1/audio/translations` with the same request/response shape as OpenAI's API, so LiteLLM and any existing OpenAI-SDK client code work unchanged.
- **Model:** `large-v3` (multilingual). Qfind's target users are Japanese-language (per Day 1 design-doc reference, §8.4) — do **not** use `distil-large-v3`, it's an English-optimized distillation with materially worse non-English accuracy. With a dedicated A400 (16 GB) and `large-v3` needing only ~3 GB at `float16`, there's no VRAM pressure that would justify trading accuracy for size. If concurrent load later becomes an issue, drop to `medium` before considering a distilled/English-only model.
- **Weight format:** faster-whisper needs **CTranslate2-converted** weights, not the raw PyTorch checkpoint. Pull the pre-converted repo `Systran/faster-whisper-large-v3` — pulling `openai/whisper-large-v3` directly will not load.

> **Before deploying:** GHCR image tags and env-var names for `speaches` do change between releases. Run `docker pull ghcr.io/speaches-ai/speaches:latest-cuda-12.4.1` on the server and check `docker run --rm ghcr.io/speaches-ai/speaches:latest-cuda-12.4.1 --help` (or the repo README) to confirm the current tag and config env vars match what's used below before wiring it into Compose. Adjust names if the release has moved on.

---

## 1. Download the model weights

```bash
cd ~/litellm-deploy/deployment
mkdir -p model-cache/whisper-large-v3
pip3 install -U "huggingface_hub[cli]"   # already installed from Day 1, harmless to re-run

huggingface-cli download Systran/faster-whisper-large-v3 \
  --local-dir model-cache/whisper-large-v3
```

### Check
- `model-cache/whisper-large-v3/` contains `model.bin`, `config.json`, `tokenizer.json`, `vocabulary.json`.

---

## 2. Add the service to `docker-compose.prod.yml`

Append under the `# ─── Inference engines ───` block, alongside `vllm-chat` / `tei-embed` / `tei-rerank`:

```yaml
  stt-whisper:
    image: ghcr.io/speaches-ai/speaches:latest-cuda-12.4.1   # verify tag per the note above
    restart: unless-stopped
    environment:
      CUDA_DEVICE_ORDER: PCI_BUS_ID
      CUDA_VISIBLE_DEVICES: "0"        # A400 — dedicated, does not compete with vllm-chat on GPU 1
      WHISPER__MODEL: Systran/faster-whisper-large-v3
      WHISPER__INFERENCE_DEVICE: cuda
      WHISPER__COMPUTE_TYPE: float16
    volumes:
      - ./model-cache/whisper-large-v3:/root/.cache/huggingface/hub/models--Systran--faster-whisper-large-v3:ro
    ports:
      - "127.0.0.1:8003:8000"          # loopback only, same pattern as vLLM/TEI — LiteLLM reaches it via qfind-net
    deploy:
      resources:
        reservations:
          devices:
            - { driver: nvidia, count: all, capabilities: [gpu] }
    networks: [qfind-net]
```

Notes:
- Port **8003** is the next free host port (8000/8001/8002/4000/6333-4/8080/9090/9400/3100/3000 are taken).
- If `speaches`' expected cache path differs from what's shown above (check the README per the Step 0 caveat), mount the model dir wherever its docs specify, or simply omit the volume and let it download the ~3 GB CTranslate2 repo itself into a named volume on first start — trade a slower first boot for one less path to get wrong:
  ```yaml
    volumes:
      - whisper-cache:/root/.cache/huggingface
  ```
  (add `whisper-cache:` to the top-level `volumes:` section if you go this route).

Add nothing to UFW — like every other engine, this is loopback-bound and reached only via `qfind-net` / LiteLLM.

Start it:
```bash
docker compose -f docker-compose.prod.yml up -d stt-whisper
docker compose -f docker-compose.prod.yml logs -f stt-whisper   # wait for the server-ready line
```

### Check
```bash
nvidia-smi   # GPU 0 (A400) now shows a process using ~3-4 GB; GPU 1 (5090) unchanged
curl -s http://localhost:8003/v1/models | python3 -m json.tool
# → lists the loaded model id, e.g. "Systran/faster-whisper-large-v3"
```

---

## 3. Test transcription directly (before wiring LiteLLM)

```bash
curl -s http://localhost:8003/v1/audio/transcriptions \
  -H "Content-Type: multipart/form-data" \
  -F file=@sample.wav \
  -F model=Systran/faster-whisper-large-v3 \
  | python3 -m json.tool
```
→ Returns `{"text": "..."}`.

Japanese sanity check (same reasoning as Day 1's embedding check — Japanese is a target-user requirement, not an edge case):
```bash
curl -s http://localhost:8003/v1/audio/transcriptions \
  -H "Content-Type: multipart/form-data" \
  -F file=@sample_ja.wav \
  -F model=Systran/faster-whisper-large-v3 \
  | python3 -m json.tool
```
→ Returns correctly-transcribed Japanese text, no error.

### Check
- Both curls return `200` with non-empty `text`.

---

## 4. Wire it into the LiteLLM gateway

Add to `deployment/litellm/config.yaml`, alongside the existing `qfind-chat` / `qfind-embed` / `qfind-rerank` entries:

```yaml
  - model_name: qfind-stt
    litellm_params:
      model: openai/Systran/faster-whisper-large-v3
      api_base: http://stt-whisper:8000/v1
      api_key: "none"
```

Restart LiteLLM to pick up the config:
```bash
docker compose -f docker-compose.prod.yml up -d --force-recreate litellm
docker compose -f docker-compose.prod.yml logs -f litellm   # wait for startup complete
```

### Check — via LiteLLM, loopback
```bash
MASTER_KEY=$(grep LITELLM_MASTER_KEY .env | cut -d= -f2)
curl -s http://localhost:4000/v1/audio/transcriptions \
  -H "Authorization: Bearer ${MASTER_KEY}" \
  -F file=@sample.wav \
  -F model=qfind-stt \
  | python3 -m json.tool
```
→ Returns `{"text": "..."}` — same shape as the direct test in Step 3, now behind the gateway's auth/budget/rate-limit layer like every other model.

### Check — via Funnel, external
```bash
curl https://ubuntu.tailcd8da4.ts.net/v1/audio/transcriptions \
  -H "Authorization: Bearer sk-YOUR-VIRTUAL-KEY" \
  -F file=@sample.wav \
  -F model=qfind-stt \
  | python3 -m json.tool
```

If you issue scoped virtual keys per Day 4 Step 11, remember to add `"qfind-stt"` to each key's `models` array — a key issued before this addendum won't have it by default and will 403 on this endpoint until re-issued or updated.

---

## 5. Extend the smoke test and monitoring

Add to `scripts/smoke-test.sh` (needs a small sample WAV checked into the repo, e.g. `scripts/sample.wav`, a couple seconds of speech):
```bash
echo "== stt =="
curl -fsS http://localhost:4000/v1/audio/transcriptions \
  -H "Authorization: Bearer ${LITELLM_VIRTUAL_KEY}" \
  -F file=@scripts/sample.wav \
  -F model=qfind-stt > /dev/null && echo OK
```

Optional — add a Prometheus scrape target if `speaches` exposes `/metrics` (check its docs); if not, `dcgm-exporter` already reports per-GPU VRAM for GPU 0, so the existing Grafana dashboard (12239) will show STT's A400 usage with no extra config.

### Check
- `smoke-test.sh` still prints `ALL CHECKS PASSED` with the new STT block included.

---

## Definition of Done

- [ ] `Systran/faster-whisper-large-v3` (CT2 format) downloaded to `model-cache/`.
- [ ] `stt-whisper` service running, pinned to GPU 0 (A400), loopback-bound on `127.0.0.1:8003`.
- [ ] Direct `curl` to port 8003 transcribes English and Japanese audio correctly.
- [ ] `qfind-stt` added to `litellm/config.yaml`; LiteLLM restarted.
- [ ] Transcription works through LiteLLM locally and through Tailscale Funnel externally.
- [ ] Existing/new virtual keys include `qfind-stt` in their `models` scope.
- [ ] `nvidia-smi` shows GPU 0 (A400) in use, GPU 1 (5090/vLLM) VRAM unchanged.
- [ ] `smoke-test.sh` updated and passing.
- [ ] `docker-compose.prod.yml` and `litellm/config.yaml` changes committed.

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `stt-whisper` fails to load model | Downloaded `openai/whisper-large-v3` (raw PyTorch) instead of the CT2 repo | Re-download `Systran/faster-whisper-large-v3` specifically |
| `could not select device driver` on `stt-whisper` start | GPU 0 not visible / NVIDIA Container Toolkit issue | Same fix as Day 1 Step 4: re-run toolkit config, `docker restart` |
| Transcription request 404s through LiteLLM | `model` in the request doesn't match `model_name` in `config.yaml` | Client must send `"model":"qfind-stt"`, not the raw HF repo id |
| Virtual key gets 403 on `/v1/audio/transcriptions` | Key issued before `qfind-stt` existed | Re-issue the key with `qfind-stt` added to its `models` array (Day 4 Step 11) |
| GPU 0 shows 0% usage during a transcription request | `CUDA_VISIBLE_DEVICES` not applied, container silently fell back to CPU | Check `docker compose logs stt-whisper` for a device warning; confirm the env var is set and container was recreated (not just restarted) after adding it |
| First request very slow, then fast | Model lazy-loads into VRAM on first inference, or first-boot download still running | Expected — warm the model with one throwaway request right after `up -d` if request-time latency matters |
