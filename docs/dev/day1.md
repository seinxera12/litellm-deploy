# Day 1 — Local Foundation (Beginner Implementation Guide, Windows + WSL2)

**Who this is for:** a developer who has **never used Docker or done a deployment before**, working on a **Windows machine** that has **Ubuntu available through WSL2**. Every concept is explained the first time it appears.

**Day 1 goal in one sentence:** by the end of today, your Ubuntu-on-WSL2 environment runs the three AI engines inside *containers* — the chat model on the GPU, the embedder and reranker on the CPU (a local trick for the small 4050) — and you can talk to each one with a simple test command.

**What you are NOT doing today:** no gateway (LiteLLM), no website/HTTPS, no production server. Today is only about proving the engines run locally inside WSL2.

> **Where the files live.** The deployment stack lives in the `deployment/` folder of this project (`deployment/docker-compose.yml`, `deployment/.env`, etc.). All `docker compose` commands below are run **from inside that folder**. See Step 1 for an important note about *which filesystem* to keep the project on under WSL2.

> **Your local GPU is small — that's expected.** This machine is a laptop with an **RTX 4050 (~4–5 GB usable VRAM)**. The production server has an **RTX 5090 (32 GB)**. The production chat model (Qwen2.5-14B) will **not** fit here, so locally we run a much smaller model (**Qwen2.5-3B-Instruct AWQ**) and we run the embedding + reranker **on the CPU** to keep the GPU free. Today's job is to prove the *plumbing* works — not to measure speed or quality. Those are validated on the real 5090 later (Phase B). Everything you build is identical to production **except** the model size and where TEI runs, so the later migration stays a small config change.

---

## 0. Concepts you need before you start (5-minute read)

- **Windows host:** your actual Windows OS. The GPU physically belongs to it.
- **WSL2 (Windows Subsystem for Linux 2):** a real Linux kernel running alongside Windows. It gives you a genuine Ubuntu terminal on your Windows machine, and — importantly — it can use your NVIDIA GPU.
- **Ubuntu (in WSL):** the Linux distribution we run inside WSL2. This is where Docker and the engines live. It mirrors the production Linux server, which is why we use it instead of running things on Windows directly.
- **GPU:** your NVIDIA card. Locally that's an **RTX 4050 (~4–5 GB VRAM)**; in production it's an RTX 5090 (32 GB). AI models run fast on it.
- **NVIDIA driver:** software that lets the OS use the GPU. **Under WSL2 this is installed on *Windows*, not inside Ubuntu** (this is the #1 thing beginners get wrong — more in Step 2).
- **CUDA:** NVIDIA's GPU math toolkit. The RTX 5090 needs a recent version (CUDA 12.8-class).
- **Container:** a sealed box holding a program plus everything it needs, so it runs the same anywhere. "A shipping container for software."
- **Image:** the blueprint a container is created from. You "pull" (download) an image, then "run" it.
- **Docker:** the tool that downloads and runs containers. We run it **inside Ubuntu/WSL2**.
- **Docker Compose:** describes several containers in one file (`docker-compose.yml`) so you start them all with one command.
- **NVIDIA Container Toolkit:** the bridge that lets a container use the GPU. Installed **inside Ubuntu/WSL2**.
- **vLLM:** serves the chat model. Locally a small **Qwen2.5-3B-Instruct (AWQ)**; in production **Qwen2.5-14B**.
- **TEI (Text Embeddings Inference):** serves the embedding model (BGE-M3) and the reranker (BGE-reranker-v2-m3).
- **Postgres / Redis:** a database and a fast cache, needed by the gateway (added Day 2). Started today so they're ready.
- **`curl`:** a command-line tool that sends a web request, to test an engine without a browser.
- **Port:** a numbered "door" a program listens on (e.g., vLLM on `8000`). You send requests to `localhost:<port>`.

> **Two golden rules for today:**
> 1. **Do everything inside the Ubuntu/WSL2 terminal**, not Windows PowerShell (except the one Windows driver step). 
> 2. **After every step there is a Check. Do not advance until it passes.** This turns a scary 12-service system into small, individually-proven pieces.

---

## 1. Set up WSL2 and the project folder

### 1a. Make sure WSL2 + Ubuntu are installed and up to date
Open **Windows PowerShell** (just for this sub-step) and run:
```powershell
wsl --install -d Ubuntu     # installs Ubuntu if you don't have it; skip if already installed
wsl --update                # ensures a recent WSL kernel (required for GPU support)
wsl --version               # confirm "WSL version" is 2.x and a recent kernel
```
Then launch Ubuntu (Start menu → "Ubuntu"). **Every command from here on is typed in the Ubuntu terminal** unless it explicitly says PowerShell.

### 1b. (Recommended) Give WSL2 enough memory
Models are big. Create/edit `C:\Users\<you>\.wslconfig` (in Windows) so WSL2 can use enough RAM:
```ini
[wsl2]
memory=24GB
swap=8GB
```
Then in PowerShell: `wsl --shutdown` and reopen Ubuntu.

### 1c. Choose where the project lives (important for speed)
WSL2 can read your Windows drives at `/mnt/c`, `/mnt/d`, etc. **But reading large files (model weights, container data) from `/mnt/...` is very slow** because it crosses the Windows↔Linux boundary.

- **Recommended:** keep the working copy inside the **Linux filesystem**, e.g. `~/litellm-deploy`. It's dramatically faster for Docker and model files. You can still edit it from Windows VS Code using the **"WSL" remote extension**, and browse it from Explorer at `\\wsl$\Ubuntu\home\<you>\litellm-deploy`.
- **If you must keep it on `D:\` (`/mnt/d/...`):** it will work, but at minimum keep `model-cache/` and Docker's data on the Linux side (Step 2/4 handle Docker's data automatically when Docker runs inside WSL).

> This guide assumes the project is at `~/litellm-deploy` inside Ubuntu, with the stack in `~/litellm-deploy/deployment`. Adjust paths if you kept it on `/mnt/d`.

### 1d. Initialize the repo and folders (inside Ubuntu)
```bash
cd ~/litellm-deploy            # or wherever your repo is
git init
cd deployment
mkdir -p caddy litellm vllm tei qdrant monitoring/grafana backups scripts docs runbook
```
Confirm `.gitignore` (at the repo root) excludes secrets and big files:
```gitignore
.env
*.env
model-cache/
**/volumes/
__pycache__/
```
Your `deployment/.env.example` already exists (safe to commit). Copy it to a real, gitignored `.env` and set a real password:
```bash
cp .env.example .env
# edit .env: set POSTGRES_PASSWORD to a real value
```

### Check
- `wsl --version` (PowerShell) showed version 2.x.
- In Ubuntu, `pwd` shows a Linux path (ideally under `/home/...`, not `/mnt/...`).
- `ls` in `deployment/` shows your folders; `git status` runs cleanly.

> **Why this matters:** WSL2's filesystem choice is the difference between model loads that take seconds vs. minutes. Getting this right on Day 1 saves pain all week.

---

## 2. Set up the GPU for WSL2 (the part that's different from plain Linux)

This is the most important WSL2-specific section. **Read it before typing.**

Under WSL2 the GPU is shared from Windows. So:
- The NVIDIA **driver is installed on Windows** (not in Ubuntu).
- Inside Ubuntu you install **only** the GPU *libraries/toolkit*, **never a Linux NVIDIA driver** — installing a Linux driver inside WSL will break GPU passthrough.

### 2a. Install the NVIDIA driver on Windows
On the **Windows** side, install the latest NVIDIA driver for your GPU (GeForce/Studio driver for the RTX 5090). Modern drivers include WSL2 GPU support automatically. Reboot Windows if prompted. *(Nothing to type in Ubuntu for this sub-step.)*

### 2b. Confirm the GPU is visible inside Ubuntu
In the **Ubuntu** terminal:
```bash
nvidia-smi
```
- **Success =** you see a table listing your GPU and a CUDA version (12.x). This works through a passthrough stub Windows places at `/usr/lib/wsl/lib/` — you did **not** install a driver in Ubuntu, and that's correct.
- If "command not found": your Windows driver is too old or WSL isn't updated. Update the Windows driver and run `wsl --update` (PowerShell), then `wsl --shutdown` and reopen.

### 2c. Install the WSL-specific CUDA toolkit (no driver) inside Ubuntu
```bash
wget https://developer.download.nvidia.com/compute/cuda/repos/wsl-ubuntu/x86_64/cuda-keyring_1.1-1_all.deb
sudo dpkg -i cuda-keyring_1.1-1_all.deb
sudo apt-get update
sudo apt-get install -y cuda-toolkit-12-8
```
> Note the repo is the **`wsl-ubuntu`** variant. This installs CUDA *without* a driver, which is exactly what WSL2 needs.

### Check
- `nvidia-smi` inside Ubuntu prints the GPU table with CUDA **12.x** (need 12.8-class for the RTX 5090).
- You did **not** install `nvidia-driver-xxx` inside Ubuntu (correct — that would break WSL GPU).

> **Why this matters:** "driver on Windows, toolkit in Ubuntu, never a driver in Ubuntu" is the rule that makes GPUs work under WSL2. Misunderstanding it is the single most common WSL GPU failure.

---

## 3. Start downloading the model weights (let it run in the background)

**Model weights** are the large files (several GB each) that *are* the AI model. Start them now so they download while you set up Docker. Store them in one shared `model-cache/` folder (on the **Linux** filesystem for speed) that containers reuse.

### Steps (inside Ubuntu)
```bash
cd ~/litellm-deploy/deployment
mkdir -p model-cache
pip install -U "huggingface_hub[cli]"   # if pip is missing: sudo apt-get install -y python3-pip

huggingface-cli download Qwen/Qwen2.5-3B-Instruct-AWQ   --local-dir model-cache/qwen2.5-3b-awq
huggingface-cli download BAAI/bge-m3                     --local-dir model-cache/bge-m3
huggingface-cli download BAAI/bge-reranker-v2-m3         --local-dir model-cache/bge-reranker-v2-m3
```
> We download the **small** local chat model (Qwen2.5-3B-Instruct AWQ), not the 14B production model — it has to fit the 4050's ~4–5 GB. If 3B still won't fit later, swap to the even smaller `Qwen/Qwen2.5-1.5B-Instruct-AWQ`. If a repo id 404s, search the model on huggingface.co and use the exact id. We use **AWQ** quantization on purpose — see Step 6's WSL note.

### Check
- Each `model-cache/<name>/` fills with files (`config.json`, `*.safetensors`, …).
- These keep downloading while you continue; they don't block the next steps.

> **Why this matters:** one shared cache means models download once and every container reuses them — the standard pattern for GPU serving.

---

## 4. Install Docker and the NVIDIA Container Toolkit (inside Ubuntu)

We run Docker **inside Ubuntu/WSL2**. (Docker Desktop for Windows with the WSL2 backend also works, but installing Docker Engine directly in Ubuntu mirrors the production Linux server more closely — better for learning deployment.)

### Steps (inside Ubuntu)
1. Install Docker Engine + Compose plugin:
   ```bash
   curl -fsSL https://get.docker.com | sudo sh
   ```
2. Run Docker without `sudo` every time (then close and reopen the Ubuntu terminal):
   ```bash
   sudo usermod -aG docker $USER
   ```
3. WSL2 doesn't use `systemd` to auto-start services the same way a full Linux box does. Start Docker (recent WSL versions start it automatically; if `docker ps` errors with "cannot connect", run):
   ```bash
   sudo service docker start
   ```
4. Confirm Docker works:
   ```bash
   docker run --rm hello-world
   ```
5. Install the **NVIDIA Container Toolkit** (the GPU bridge), inside Ubuntu:
   ```bash
   curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey | sudo gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
   curl -s -L https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list | \
     sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' | \
     sudo tee /etc/apt/sources.list.d/nvidia-container-toolkit.list
   sudo apt-get update && sudo apt-get install -y nvidia-container-toolkit
   sudo nvidia-ctk runtime configure --runtime=docker
   sudo service docker restart
   ```

### Check (the most important check of the day)
Run the GPU inside a container:
```bash
docker run --rm --gpus all nvidia/cuda:12.8.0-base-ubuntu24.04 nvidia-smi
```
- **Success =** the same GPU table from Step 2, printed from *inside a container*. That proves containers in WSL2 can use the GPU.
- If "could not select device driver ... [[gpu]]": the toolkit step didn't take — re-run step 5 and `sudo service docker restart`.

> **Why this matters:** `--gpus all` is the whole point of the toolkit. Once this passes inside WSL2, every GPU service (vLLM, TEI) works the same way.

---

## 5. Create the Compose scaffold and start Postgres + Redis

We build `deployment/docker-compose.yml` gradually. Today it gets a shared network plus the database and cache.

### Steps (inside Ubuntu, in `deployment/`)
1. Put this in `docker-compose.yml`:
   ```yaml
   services:
     litellm-db:
       image: postgres:16
       environment:
         POSTGRES_USER: ${POSTGRES_USER}
         POSTGRES_PASSWORD: ${POSTGRES_PASSWORD}
         POSTGRES_DB: ${POSTGRES_DB}
       volumes:
         - pgdata:/var/lib/postgresql/data
       networks: [qfind-net]

     litellm-redis:
       image: redis:7
       networks: [qfind-net]

   networks:
     qfind-net:

   volumes:
     pgdata:
   ```
   - **`image`** = which blueprint to download. **`environment`** = settings; `${...}` reads from `.env`.
   - **`volumes`** = data that survives container deletion (so the DB isn't wiped on restart). With Docker running in WSL2, this lives safely in the Linux filesystem.
   - **`networks`** = a private network so containers find each other by name (e.g., `litellm-db`).
2. Start them:
   ```bash
   docker compose up -d
   ```
   (`up` = start; `-d` = run in the background.)

### Check
```bash
docker compose ps                                  # both show "running"
docker compose exec litellm-db pg_isready          # "accepting connections"
docker compose exec litellm-redis redis-cli ping   # "PONG"
```

> **Why this matters:** you just learned the core Compose loop for the whole week — edit YAML → `up -d` → check `ps`/`exec`. `exec` runs a command *inside* a running container.

---

## 6. Run vLLM (the chat model) by itself

Add the chat engine to the same Compose file.

> **Local model choice (important).** Your laptop's **RTX 4050 has only ~4–5 GB of usable VRAM**. The production model (Qwen2.5-14B) will **not** fit. For local development we use a much smaller model from the same family — **Qwen2.5-3B-Instruct (AWQ)** — purely to validate that the *plumbing* works (requests, streaming, routing). Local quality and speed are **not** representative; those are validated on the production 5090 in Phase B. If 3B still runs out of memory, drop to **Qwen2.5-1.5B-Instruct (AWQ)**.

> **WSL2 quantization note (design doc §6.1):** FP8 is native-Linux-only, so locally we use **AWQ**. FP8 is validated later on the production server, not here.

> **Why TEI goes on the CPU (Step 7).** With only 4–5 GB, we can't fit a chat model *and* two embedding models on the GPU. So locally we give the GPU almost entirely to vLLM and run the embedder/reranker on the CPU. On the production 5090 they all share the GPU as designed — that's a one-line image/flag change at cutover.

### Steps
1. Add under `services:`:
   ```yaml
     vllm-chat:
       image: vllm/vllm-openai:v0.17.0   # pinned for RTX 5090 / Blackwell support (same image locally)
       command: >
         --model /models/qwen2.5-3b-awq
         --quantization awq
         --served-model-name qfind-chat
         --gpu-memory-utilization 0.80
         --max-model-len 8192
         --max-num-seqs 8
         --enforce-eager
         --enable-prefix-caching
       volumes:
         - ./model-cache/qwen2.5-3b-awq:/models/qwen2.5-3b-awq:ro
       ports:
         - "8000:8000"
       deploy:
         resources:
           reservations:
             devices:
               - { driver: nvidia, count: all, capabilities: [gpu] }
       networks: [qfind-net]
   ```
   - **`--served-model-name qfind-chat`** gives the model a *stable API name*. Your test commands say `"model":"qfind-chat"` and will work **unchanged on production**, even though prod serves the 14B model underneath. This is a deliberate migration-friendliness trick.
   - **`--max-model-len 8192`** caps context length so the KV cache stays small — essential on 4–5 GB.
   - **`--max-num-seqs 8`** limits how many requests batch together at once (another memory control).
   - **`--enforce-eager`** turns off CUDA-graph capture, trading a little speed for less VRAM — worth it on a tiny GPU.
   - **`--gpu-memory-utilization 0.80`** caps vLLM at 80% of the card; lower to `0.70` if Windows/display needs more.
   - **`--enable-prefix-caching`** speeds up repeated prompt prefixes (design doc §8.8).
   - **`ports: "8000:8000"`** opens door 8000. Thanks to WSL2's localhost forwarding, you can `curl localhost:8000` from Ubuntu or Windows — but run checks from Ubuntu.
2. Start it and watch it load (a minute or two for a 3B model):
   ```bash
   docker compose up -d vllm-chat
   docker compose logs -f vllm-chat        # wait for "Application startup complete"; Ctrl+C stops watching, not the container
   ```

### Check
1. Non-streaming:
   ```bash
   curl http://localhost:8000/v1/chat/completions -H 'Content-Type: application/json' \
     -d '{"model":"qfind-chat","messages":[{"role":"user","content":"Say hello in one word."}]}'
   ```
2. Streaming:
   ```bash
   curl http://localhost:8000/v1/chat/completions -H 'Content-Type: application/json' \
     -d '{"model":"qfind-chat","stream":true,"messages":[{"role":"user","content":"Count to five."}]}'
   ```
   → a stream of `data: {...}` lines ending in `data: [DONE]`.

> **If vLLM crashes with an out-of-memory error:** lower `--gpu-memory-utilization` to `0.70`, reduce `--max-model-len` to `4096`, or switch the model to `Qwen2.5-1.5B-Instruct-AWQ`. Confirm TEI is **not** on the GPU (Step 7 keeps it on CPU).

> **Why this matters:** you served a real LLM on your laptop GPU through WSL2 and confirmed both reply modes. The `qfind-chat` stable name means the exact same request works against production later. Streaming is what makes Qfind's chat feel responsive (§8.7).

---

## 7. Run TEI (embeddings + reranker) by itself — on CPU locally

Two copies of TEI: one for embeddings (BGE-M3), one for reranking (BGE-reranker-v2-m3). **Locally we run them on the CPU** (note the `cpu-` image tag and the absence of any `deploy:`/GPU block) so they leave the 4050's scarce VRAM for vLLM. CPU embedding is slower than GPU but perfectly fine for development sanity checks. On the production 5090 these run on the GPU as designed — a one-line image/flag change at cutover.

### Steps
1. Add to `docker-compose.yml`:
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
         - ./model-cache/bge-reranker-v2-m3:/models/bge-reranker-v2-m3:ro
       ports: ["8002:80"]
       networks: [qfind-net]
   ```
   (TEI listens on port 80 *inside* the container; we map embed→8001, rerank→8002 on the host. The `--max-batch-tokens` flag is a GPU tuning knob — omit it on the CPU image.)
2. Start them:
   ```bash
   docker compose up -d tei-embed tei-rerank
   docker compose logs -f tei-embed     # wait for "Ready" (first start also loads the model into RAM)
   ```

### Check
1. Embeddings (returns a list of numbers = the vector):
   ```bash
   curl http://localhost:8001/embed -H 'Content-Type: application/json' \
     -d '{"inputs":"Qfind searches your files."}'
   ```
2. Reranking (returns relevance scores):
   ```bash
   curl http://localhost:8002/rerank -H 'Content-Type: application/json' \
     -d '{"query":"file search","texts":["a tool that searches files","a recipe for soup"]}'
   ```
3. **Japanese sanity check** (§17.4):
   ```bash
   curl http://localhost:8001/embed -H 'Content-Type: application/json' \
     -d '{"inputs":"ファイルを検索します。"}'
   ```
   → returns a vector, no error.

> **Why this matters:** these endpoints power the "retrieval" half of the system. Confirming Japanese now avoids a nasty surprise later (target users are Japanese-language, §8.4).

---

## 8. Take a VRAM baseline snapshot

Only vLLM (the small chat model) is on the GPU now — TEI is on the CPU. Confirm the GPU isn't overcommitted.

### Steps
```bash
nvidia-smi      # look at "Memory-Usage"
```

### Check
- vLLM should use roughly **2–4 GB** (Qwen2.5-3B AWQ weights + a small KV cache) on your ~4–5 GB card, with some headroom left. No out-of-memory errors in `docker compose logs vllm-chat`.
- Record this number in `docs/` as your local baseline.

> **Important — this number does NOT predict production.** Locally you're measuring a 3B model alone on a 4 GB laptop GPU. Production runs the **14B** model **plus** GPU-resident TEI on a 32 GB 5090; that VRAM budget (§11.4) is validated separately on the real hardware in Phase B. Today only proves "it fits and runs here."

> **WSL2 note:** if numbers look off, confirm no heavy Windows GPU app (a game, another ML job) is competing for VRAM.

---

## 9. Save a reusable smoke test

Save all the checks so you can re-run them with one command (you'll reuse this on the production server in Day 3).

### Steps
Create `scripts/smoke-test.sh`:
```bash
#!/usr/bin/env bash
set -euo pipefail
echo "== chat (non-stream) =="
curl -fsS http://localhost:8000/v1/chat/completions -H 'Content-Type: application/json' \
  -d '{"model":"qfind-chat","messages":[{"role":"user","content":"hi"}]}' > /dev/null && echo OK
echo "== embed =="
curl -fsS http://localhost:8001/embed -H 'Content-Type: application/json' \
  -d '{"inputs":"hello"}' > /dev/null && echo OK
echo "== rerank =="
curl -fsS http://localhost:8002/rerank -H 'Content-Type: application/json' \
  -d '{"query":"x","texts":["x","y"]}' > /dev/null && echo OK
echo "ALL CHECKS PASSED"
```
Run it:
```bash
chmod +x scripts/smoke-test.sh
./scripts/smoke-test.sh
```

### Check
- Prints `ALL CHECKS PASSED`. (`-f` makes curl fail on HTTP errors; `set -e` stops on first failure — so green really means healthy.)

> **Why this matters:** automating checks is a core deployment habit. The same script runs unchanged on the production Linux server later — proving your local WSL2 work transfers.

---

## End-of-Day 1 Definition of Done

- [ ] WSL2 is v2.x with a recent kernel; Ubuntu launches.
- [ ] `nvidia-smi` works **inside Ubuntu** (driver on Windows, toolkit in Ubuntu, no driver installed in Ubuntu).
- [ ] `nvidia-smi` works **inside a container** (`docker run --gpus all ...`).
- [ ] Postgres + Redis running (`pg_isready`, `PONG`).
- [ ] vLLM answers chat completions — **streaming and non-streaming** (Qwen2.5-3B AWQ, served as `qfind-chat`).
- [ ] TEI answers `/embed` and `/rerank` (**on CPU**), including a Japanese input.
- [ ] vLLM fits under the 4050's VRAM with headroom; baseline recorded.
- [ ] `scripts/smoke-test.sh` prints `ALL CHECKS PASSED`.
- [ ] Repo committed; `git status` clean of secrets and model files.

If every box is checked, you're ready for Day 2 (LiteLLM gateway + RAG pipeline).

---

## Common Day-1 problems and fixes (WSL2-aware)

| Symptom | Likely cause | Fix |
|---|---|---|
| `nvidia-smi` not found **in Ubuntu** | old Windows driver or stale WSL kernel | update the **Windows** NVIDIA driver; `wsl --update` then `wsl --shutdown` (PowerShell) |
| You installed `nvidia-driver-xxx` in Ubuntu and GPU broke | a Linux driver must **not** be installed in WSL | remove it; rely on the Windows driver + `wsl-ubuntu` CUDA toolkit only |
| `docker: Cannot connect to the Docker daemon` | Docker service not started in WSL2 | `sudo service docker start` |
| `docker: permission denied` | user not in `docker` group yet | reopen Ubuntu after `usermod -aG docker $USER`, or use `sudo` |
| `could not select device driver "" with [[gpu]]` | NVIDIA Container Toolkit not configured | re-run Step 4.5, `sudo service docker restart` |
| vLLM CUDA/FP8 error | tried FP8 on WSL2 (unsupported, §6.1) | use **AWQ** locally (as configured); FP8 is validated on the prod Linux server |
| vLLM "out of memory" at startup (likely on a 4–5 GB 4050) | model/KV cache too big, or TEI also on GPU | lower `--gpu-memory-utilization` to `0.70`; reduce `--max-model-len` to `4096`; drop to `Qwen2.5-1.5B-Instruct-AWQ`; confirm TEI uses the **`cpu-`** image (not GPU); close GPU-heavy Windows apps; raise `.wslconfig` memory |
| Everything is painfully slow; huge disk reads | project/model-cache on `/mnt/d` (Windows fs) | move repo + `model-cache/` into the Linux filesystem (`~/...`) |
| `curl: connection refused` | engine still loading, or wrong port | `docker compose logs -f <service>` until ready; verify `ports:` mapping |

---

## Learnings — what Day 1 teaches

Day 1 is the "foundations" day. Completing it on Windows + WSL2 teaches a beginner:

1. **The WSL2 GPU model.** The NVIDIA **driver lives on Windows**; Ubuntu gets only the **WSL-specific CUDA toolkit** and the **NVIDIA Container Toolkit**; you **never install a Linux driver inside WSL**. `nvidia-smi` working in Ubuntu, then inside a container, are the two checkpoints that prove the chain end to end.
2. **The container mental model.** *Image* = blueprint, *container* = running instance, *volume* = data that outlives the container, *network* = how containers find each other by name. These four ideas explain most of the week.
3. **The Compose workflow loop.** Edit `docker-compose.yml` → `docker compose up -d` → check with `ps` / `logs -f` / `exec`. You'll repeat this every day.
4. **WSL2's real-world gotchas.** Keep files on the **Linux filesystem** (not `/mnt/d`) for speed; give WSL2 enough RAM via `.wslconfig`; start the Docker service with `service docker start`. These aren't in most "plain Linux" guides and are exactly where WSL beginners lose hours.
5. **Local ≠ production, and the gap is designed around.** Your 4050 can't run the real model, so you used a **small Qwen2.5-3B** served under the stable name `qfind-chat`, ran **TEI on CPU** to spare VRAM, and capped context/batch with `--max-model-len`/`--max-num-seqs`/`--enforce-eager`. The stack is identical to production *except* model size and TEI placement — so the migration is a config change, and the real model/VRAM/FP8/concurrency numbers are proven on the 5090 later. Knowing *what a small local box can and cannot prove* is itself a deployment skill.
6. **Test components in isolation before integrating.** Each engine was proven alone with `curl` before any gateway or pipeline exists. Later failures are easier to locate because these pieces are already trusted.
7. **VRAM is a hard budget, and you have levers for it.** When the GPU is small, you (a) shrink the model, (b) offload non-critical models to CPU (TEI), and (c) cap memory with `--gpu-memory-utilization`, `--max-model-len`, `--max-num-seqs`, and `--enforce-eager`. On the big production GPU the same levers let the 14B chat model and GPU-resident TEI *share* one card (§11.4). Either way you tune against real `nvidia-smi` numbers, not estimates.
8. **Operational hygiene from minute one.** Secrets in a gitignored `.env`, models in a shared cache, checks captured in a reusable script that runs identically on the production box. These habits are what make a deployment survive a restart — and a migration.
