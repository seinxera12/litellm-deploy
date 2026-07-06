# The Docker & Kubernetes Field Guide

### *For developers who want to actually understand what's happening*

  

---

  

## Preface: The Right Mental Model

  

Before you touch a single command, internalize this: Docker is not magic. It's a clever use of **Linux kernel features that have existed for years** — namespaces, cgroups, and union filesystems. Kubernetes is not magic either. It's a distributed system that watches desired state and reconciles it with actual state, forever.

  

If you approach Docker thinking "it's like a VM," you'll be confused constantly. If you approach it thinking "it's a process with walls around it," everything will start to make sense.

  

---

  

## PART ONE: Docker

  

---

  

### Chapter 1 — What Is a Container, Really?

  

**Start here before installing anything.**

  

A container is just a Linux process (or a group of processes) running with three kernel features applied:

  

1. **Namespaces** — what the process *can see*

2. **cgroups** — what the process *can use*

3. **Union filesystem** — what filesystem the process *thinks it has*

  

That's it. There's no hypervisor. No virtual machine. No emulated hardware. When you run a container on Linux, you are running a process on the host kernel — with carefully constructed illusions around it.

  

**Mental notes:**

- A container shares the host OS kernel. A VM does not.

- Two containers on the same host are two processes on the same kernel, isolated from each other.

- On macOS and Windows, Docker *does* spin up a small Linux VM because containers need a Linux kernel. But that's Docker Desktop's job, not the container's.

  

**Things to explore:**

- Run `docker run -it ubuntu bash` and then `ps aux` inside it. You'll see very few processes. Now open another terminal on the host and run `ps aux | grep bash` — you'll see the same bash process. Same process, two views.

- Read about Linux namespaces: PID, NET, MNT, UTS, IPC, USER. Each one isolates a different dimension.

- Look up `unshare` — the raw Linux tool that does what Docker does, manually.

  

---

### Chapter 2 — Images: The Filesystem Blueprint

An image is a **read-only, layered snapshot of a filesystem**. It is not a running thing. Think of it as a template, or a class in OOP — a container is an instance of an image.


**How layers work:**

Docker images are built in layers using a **Union Filesystem** (OverlayFS on most modern systems). Each instruction in a Dockerfile creates a new layer on top of the previous one.

  

```

Layer 4: COPY ./app /app          ← your code

Layer 3: RUN pip install -r ...   ← dependencies installed

Layer 2: RUN apt-get update       ← OS packages

Layer 1: FROM ubuntu:22.04        ← base OS filesystem

```

  

These layers are **stacked**. The filesystem you see inside a container is the union of all these layers. When you write a file inside a running container, it goes into a **thin writable layer on top** — the original image layers are never modified. This is called **Copy-on-Write (CoW)**.

  

**Why this matters:**

- If 10 containers are based on the same `ubuntu:22.04` image, that base layer is stored **once** on disk and shared by all 10.

- Pulling an image only downloads the layers you don't already have.

- Layers are content-addressed by SHA256 hash. The same layer, pulled from different images, is stored once.

  

**Things to explore:**

- Run `docker image inspect <image>` and look at the `Layers` array. Each entry is a SHA256 hash of a layer tarball.

- Run `docker history <image>` to see the commands that created each layer and their sizes.

- Look at `/var/lib/docker/overlay2/` on a Linux Docker host. You'll see the actual layer directories.

- Understand why order of Dockerfile instructions matters for caching. Put things that change rarely (dependencies) before things that change often (your code).

  

---

 

### Chapter 3 — The Dockerfile: Writing Good Blueprints

  

A Dockerfile is a recipe for constructing an image. Each line is an instruction. Each instruction creates a layer.

  

**The most important instructions and what they actually do:**

  

`FROM` — Sets the base image. Every image has a parent except scratch (the empty base).

  

`RUN` — Executes a command during the build process. The result is baked into a new layer. This is NOT runtime — it runs at build time.

  

`CMD` vs `ENTRYPOINT` — This confuses everyone. Think of it this way:

  - `ENTRYPOINT` is the executable. It always runs.

  - `CMD` provides default arguments to the entrypoint.

  - If you use only `CMD`, the whole thing is replaceable by whatever you pass to `docker run`.

  - Common pattern: `ENTRYPOINT ["node"]` + `CMD ["server.js"]` → runs `node server.js`, but you can override with `docker run myimage server-test.js`.

  

`COPY` vs `ADD` — Use `COPY`. It's explicit. `ADD` has magic powers (auto-extracting tarballs, fetching URLs) that make builds unpredictable.

  

`ENV` — Sets environment variables. These are baked into the image and visible at runtime.

  

`ARG` — Build-time variables only. Not available in the running container. Use for things like version pins.

  

`EXPOSE` — Documentation only. It does not actually open a port. It's a signal to whoever runs the image.

  

`WORKDIR` — Sets the working directory for subsequent instructions. Prefer this over `RUN cd`.

  

**Good Dockerfile habits:**

- Combine `RUN` commands with `&&` to reduce layers: `RUN apt-get update && apt-get install -y curl && rm -rf /var/lib/apt/lists/*`

- Use `.dockerignore` just like `.gitignore` — it stops unnecessary files from being sent to the build context

- Use multi-stage builds to keep your final image small:

  

```dockerfile

# Stage 1: Build

FROM node:20 AS builder

WORKDIR /app

COPY package*.json ./

RUN npm ci

COPY . .

RUN npm run build

  

# Stage 2: Run

FROM node:20-alpine

WORKDIR /app

COPY --from=builder /app/dist ./dist

COPY --from=builder /app/node_modules ./node_modules

CMD ["node", "dist/index.js"]

```

  

The final image contains no build tools, no source files — just what's needed to run.

  

---

  

### Chapter 4 — Running Containers: What Actually Happens

  

When you run `docker run nginx`:

  

1. Docker checks if the image `nginx` exists locally

2. If not, it pulls it from Docker Hub (or your configured registry) layer by layer

3. Docker creates a new **container layer** (writable) on top of the image layers

4. Docker sets up **namespaces** for the container (new PID namespace, new network namespace, etc.)

5. Docker applies **cgroup** limits (CPU, memory) if specified

6. Docker sets up the **network** (default: a virtual ethernet pair connecting the container to a bridge network)

7. Docker runs the process specified in `CMD`/`ENTRYPOINT` as PID 1 inside the container

  

**PID 1 is special.** In Linux, PID 1 (init) is responsible for reaping zombie processes. If your app becomes PID 1 and doesn't handle signals properly, you can end up with zombies and improper shutdown behavior. This is why tools like `tini` exist as init systems for containers.

  

**Key flags you should understand deeply:**

  

`-p 8080:80` — Publish port. Maps host port 8080 to container port 80. Docker uses iptables rules to do this routing. The container itself always listens on port 80; the host intercepts traffic on 8080 and routes it in.

  

`-v /host/path:/container/path` — Bind mount. The host directory is mounted into the container. Changes in the container reflect on the host and vice versa. The host filesystem takes precedence — it overlays the container filesystem at that path.

  

`-e MY_VAR=value` — Inject environment variable at runtime.

  

`--network` — Which network to attach to.

  

`-d` — Detached mode. Run in background. Without it, the container is attached to your terminal.

  

`--rm` — Automatically remove the container when it exits.

  

**Things to explore:**

- Run `docker run --rm -it ubuntu bash` and from inside, run `hostname`, `ip addr`, `cat /etc/hosts`. See the isolated network view.

- Run `docker inspect <container_id>` — this is a goldmine. You'll see the entire configuration: mounts, network settings, environment variables, the actual commands run.

- Run `docker stats` to see live CPU/memory usage per container (backed by cgroup data).

  

---

  

### Chapter 5 — Networking: How Containers Talk

  

Docker networking is one of the most important things to truly understand. By default, Docker creates three networks: `bridge`, `host`, and `none`.

  

**Bridge Network (default)**

  

When Docker installs, it creates a virtual network bridge on the host called `docker0`. Each container gets a virtual ethernet interface (`veth`) that is connected to this bridge.

  

```

Host

  ├── docker0 (bridge: 172.17.0.1)

  │     ├── veth1234 ←→ container1 eth0 (172.17.0.2)

  │     ├── veth5678 ←→ container2 eth0 (172.17.0.3)

```

  

Containers on the same bridge can talk to each other by IP. Docker uses iptables for NAT to allow containers to reach the internet via the host.

  

**User-defined Bridge Networks**

  

The default bridge is limited. Create your own bridge network and you get **automatic DNS resolution** — containers can reach each other by **container name**, not just IP.

  

```bash

docker network create myapp-net

docker run --network myapp-net --name db postgres

docker run --network myapp-net --name api myapi  # can reach db at hostname "db"

```

  

This is how most multi-container setups work in development.

  

**Host Network**

  

`--network host` — the container shares the host's network stack directly. No isolation. The container's port 80 IS the host's port 80. Useful for performance-critical applications or when you need to access the host network directly. Only works natively on Linux.

  

**None Network**

  

`--network none` — completely isolated. No network interfaces except loopback. For security-sensitive workloads.

  

**Container-to-container communication across bridge:**

Containers on the same user-defined network communicate directly. Containers on different networks cannot see each other by default — you'd need to connect a container to both networks or use an overlay network.

  

**Port publishing mechanics:**

When you do `-p 8080:80`, Docker adds iptables DNAT rules that say "traffic arriving at host:8080, redirect to container-ip:80." You can see these with `iptables -t nat -L` on Linux.

  

---

  

### Chapter 6 — Volumes: Persistence Beyond the Container

  

Containers are ephemeral. When a container dies, its writable layer dies with it. To persist data, you use volumes.

  

There are three types:

  

**1. Docker-managed Volumes**

```bash

docker volume create mydata

docker run -v mydata:/var/lib/postgresql/data postgres

```

Docker manages the storage location (typically `/var/lib/docker/volumes/`). These survive container removal. Best for databases and persistent application state.

  

**2. Bind Mounts**

```bash

docker run -v /home/user/project:/app myapp

```

A specific host path is mounted into the container. The host filesystem is the source of truth. Best for development (live code reloading) and when you need to share files between host and container.

  

**3. tmpfs Mounts**

Stored in host memory only. Never written to disk. For sensitive data you don't want persisted (secrets, credentials in transit).

  

**Volume internals:**

With OverlayFS, a bind mount essentially bypasses the union filesystem at that mountpoint. The container sees the host's real directory, not a union-merged view. Writes go directly to the host filesystem.

  

**Things to explore:**

- `docker volume ls` and `docker volume inspect`

- Understand the difference between volume mount and bind mount semantics when the target directory already has data in the image. (Bind mount: host wins. Docker volume: image data is copied in on first use if volume is empty.)

  

---

  

### Chapter 7 — Docker Compose: Multi-Container Orchestration for Development

  

Docker Compose is a tool to define and run multi-container applications with a single YAML file. It's not production orchestration — it's a development and local testing tool.

  

```yaml

# docker-compose.yml

version: "3.9"

  

services:

  db:

    image: postgres:15

    environment:

      POSTGRES_PASSWORD: secret

    volumes:

      - pgdata:/var/lib/postgresql/data

  

  api:

    build: ./api

    ports:

      - "3000:3000"

    environment:

      DATABASE_URL: postgres://postgres:secret@db:5432/mydb

    depends_on:

      - db

  

volumes:

  pgdata:

```

  

**What Compose does under the hood:**

- Creates a dedicated Docker network for the project (e.g., `myproject_default`)

- All services are attached to this network and can resolve each other by service name

- `depends_on` controls start order but NOT readiness (the DB container starting ≠ Postgres is accepting connections)

  

**Key commands:**

- `docker compose up -d` — start everything detached

- `docker compose logs -f api` — follow logs from the api service

- `docker compose exec db psql -U postgres` — exec into a running service

- `docker compose down -v` — stop and remove containers, networks, volumes

  

**Things to master:**

- Health checks in Compose to handle the depends_on readiness problem

- Override files (`docker-compose.override.yml`) for environment-specific config

- The `build` key vs `image` key — when to build locally vs pull

  

---

  

### Chapter 8 — The Docker Daemon and CLI Architecture

  

Understanding the Docker architecture helps demystify what's happening:

  

```

docker CLI  →  REST API  →  dockerd (daemon)  →  containerd  →  runc  →  kernel

```

  

- `docker` CLI: The client. Just converts your commands to REST API calls.

- `dockerd`: The Docker daemon. Manages images, networks, volumes. Receives API calls.

- `containerd`: The actual container runtime. Manages the container lifecycle.

- `runc`: The low-level OCI runtime. Does the actual `clone()` syscall, namespace creation, cgroup setup.

- The kernel: Does the real work.

  

This layered architecture means you can replace parts of the stack (e.g., use Podman instead of dockerd, or containerd directly in Kubernetes).

  

**OCI (Open Container Initiative):** The standard that defines image format and runtime spec. Docker images are OCI-compliant. This is why Kubernetes can run Docker images without Docker.

  

---

  

### Chapter 9 — Security Mental Model

  

Running a container as root inside the container means running as root on the host (unless User Namespaces are configured). This is a significant security consideration.

  

**Key concepts:**

- `USER` instruction in Dockerfile — always run your app as a non-root user

- Read-only root filesystem (`--read-only`) — prevents the app from writing to its own filesystem

- Dropping capabilities — containers get a subset of Linux capabilities by default; you can drop more with `--cap-drop`

- Secrets management — never bake secrets into images; use environment variables, Docker secrets, or a vault

  

---

  

### Chapter 10 — Docker in Production: What Changes

  

Docker itself isn't typically used alone in production. But understanding what production adds:

  

- **Image registries:** Your own registry (AWS ECR, Google GCR, self-hosted Harbor) instead of Docker Hub

- **Image scanning:** Scan images for vulnerabilities before deploying (Trivy, Snyk)

- **Logging drivers:** Container stdout → centralized logging (Fluentd, CloudWatch, etc.)

- **Resource limits:** Always set `--memory` and `--cpus` in production to prevent one container starving others

- **Health checks:** `HEALTHCHECK` in Dockerfile or at runtime — orchestrators use this to determine if a container is healthy

  

---

  

## PART TWO: Kubernetes

  

---

  

### Chapter 11 — Why Kubernetes Exists

  

Once you have containers, you have a new set of problems:

- How do I run 50 instances of my API container?

- What happens when a container dies?

- How do I update to a new version without downtime?

- How do I route traffic to healthy containers only?

- How do I distribute containers across multiple servers?

  

Docker Compose answers these for one machine. Kubernetes answers them for a **fleet of machines**.

  

**The core Kubernetes philosophy:** You describe the *desired state* of the world in YAML. Kubernetes continuously watches the actual state and works to make it match your desired state. This is called the **control loop** or **reconciliation loop**.

  

---

  

### Chapter 12 — The Cluster: Nodes and Architecture

  

A Kubernetes cluster has two types of machines:

  

**Control Plane (Master) Nodes:**

- `kube-apiserver` — the heart. All communication goes through this REST API. `kubectl` talks to this.

- `etcd` — the brain. A distributed key-value store. The entire cluster state lives here.

- `kube-scheduler` — decides which node a new pod should run on.

- `kube-controller-manager` — runs control loops (Deployment controller, ReplicaSet controller, etc.)

  

**Worker Nodes:**

- `kubelet` — the agent on each worker. Receives pod specs, ensures containers are running. Talks to the container runtime.

- `kube-proxy` — manages network rules on each node for Service routing.

- **Container runtime** — containerd or CRI-O; actually runs containers.

  

```

┌─────────────────────────────────────┐

│          Control Plane               │

│  APIServer ← etcd                    │

│  Scheduler   ControllerManager       │

└────────────┬────────────────────────┘

             │ (watch/notify)

     ┌───────┴──────────────────┐

     │                          │

┌────▼──────┐           ┌──────▼──────┐

│  Worker 1  │           │  Worker 2   │

│  kubelet   │           │  kubelet    │

│  kube-proxy│           │  kube-proxy │

│  containerd│           │  containerd │

│ [Pod][Pod] │           │ [Pod][Pod]  │

└────────────┘           └────────────┘

```

  

---

  

### Chapter 13 — The Core Primitives

  

Everything in Kubernetes is an **object** — a record of desired state stored in etcd. You declare objects in YAML and apply them.

  

**Pod**

The smallest deployable unit. A pod is one or more containers that share a network namespace and storage. Containers in the same pod communicate over localhost and share volumes.

  

Never create pods directly in production. Use higher-level controllers.

  

**Deployment**

Declares: "I want 3 replicas of this pod running at all times." The Deployment controller creates a ReplicaSet, which creates/deletes pods to match the desired count. If a pod dies, the controller creates a new one. Rolling updates and rollbacks are Deployment features.

  

```yaml

apiVersion: apps/v1

kind: Deployment

metadata:

  name: api

spec:

  replicas: 3

  selector:

    matchLabels:

      app: api

  template:

    metadata:

      labels:

        app: api

    spec:

      containers:

      - name: api

        image: myapi:1.2.3

        ports:

        - containerPort: 3000

        resources:

          requests:

            memory: "64Mi"

            cpu: "250m"

          limits:

            memory: "128Mi"

            cpu: "500m"

```

  

**Service**

Pods are ephemeral; their IPs change. A Service gives a stable virtual IP (ClusterIP) that load-balances to a set of pods selected by labels. kube-proxy on each node maintains iptables rules to implement this.

  

Types:

- `ClusterIP` — internal to the cluster only

- `NodePort` — opens a port on every node, accessible from outside

- `LoadBalancer` — provisions a cloud load balancer (in cloud environments)

  

**Ingress**

A layer of routing rules in front of Services. Routes HTTP/HTTPS traffic to different services based on host/path. Needs an Ingress Controller (nginx-ingress, Traefik, etc.) to actually implement the rules.

  

**ConfigMap & Secret**

- `ConfigMap` — non-sensitive configuration (config files, env vars)

- `Secret` — sensitive data (passwords, tokens, TLS certs) — base64 encoded, not encrypted by default (encryption at rest is a cluster configuration)

  

Both can be mounted as volumes or injected as environment variables into pods.

  

**Namespace**

A virtual cluster within a cluster. Used for multi-tenant isolation, environment separation, and resource quota management.

  

---

  

### Chapter 14 — Labels, Selectors, and How Everything Connects

  

Kubernetes objects are connected through **labels and selectors**, not by name or explicit references.

  

A Deployment's selector says "manage pods that have label `app: api`."

A Service's selector says "send traffic to pods that have label `app: api`."

  

These are independent. The Deployment and Service don't reference each other — they both happen to select the same pods. This loose coupling is intentional and powerful.

  

```

Deployment ──(creates)──→ ReplicaSet ──(creates)──→ Pods [app: api]

                                                          ↑

Service ──────────────────────(selects by label)──────────┘

```

  

---

  

### Chapter 15 — Storage in Kubernetes

  

**PersistentVolume (PV)** — A piece of storage provisioned in the cluster (NFS mount, EBS volume, etc.)

  

**PersistentVolumeClaim (PVC)** — A request for storage by a user. "I need 10Gi of storage." Kubernetes binds PVCs to PVs.

  

**StorageClass** — Enables dynamic provisioning. Instead of pre-creating PVs, define a StorageClass and PVCs can trigger automatic PV creation on demand (e.g., auto-create an EBS volume in AWS).

  

Pods reference PVCs, not PVs directly. This abstracts the actual storage backend.

  

---

  

### Chapter 16 — The Scheduler: How Pods Land on Nodes

  

When a pod needs to be scheduled, the scheduler:

  

1. **Filters** nodes that don't meet hard requirements (resource requests, node selectors, taints/tolerations)

2. **Scores** remaining nodes (prefers nodes with more free resources, spreads replicas across zones)

3. **Binds** the pod to the winning node

  

**Resources requests vs limits:**

- `requests` — what the scheduler uses to find a fitting node. Guaranteed minimum.

- `limits` — the hard cap. CPU is throttled at the limit. Memory is OOM-killed at the limit.

  

**Node affinity** — Rules about which nodes a pod can or prefers to run on (based on node labels).

  

**Taints and Tolerations** — Taints on nodes repel pods. Only pods with matching tolerations can land on tainted nodes. Used for dedicated node pools (GPU nodes, spot instances, etc.)

  

---

  

### Chapter 17 — Rolling Updates, Rollbacks, and Zero-Downtime Deploys

  

A Deployment rolling update by default:

1. Creates a new ReplicaSet with the new image version

2. Gradually scales up the new RS and scales down the old one

3. Controlled by `maxSurge` (extra pods allowed) and `maxUnavailable` (how many can be down)

  

```yaml

strategy:

  type: RollingUpdate

  rollingUpdate:

    maxSurge: 1

    maxUnavailable: 0

```

  

With `maxUnavailable: 0`, Kubernetes never terminates an old pod until a new one is healthy (readiness probe passing). Zero downtime.

  

**Rollback:** `kubectl rollout undo deployment/api` — Kubernetes keeps a history of ReplicaSets. Rollback just swaps which RS is scaled up.

  

**Readiness vs Liveness probes:**

- `readinessProbe` — Is the pod ready to serve traffic? If failing, removed from Service endpoints.

- `livenessProbe` — Is the pod alive? If failing repeatedly, the kubelet restarts the container.

- `startupProbe` — Has the app finished starting? Prevents liveness from killing a slow-starting container.

  

---

  

### Chapter 18 — Kubernetes Networking Deep Dive

  

Every pod gets a unique IP address. Pods can communicate with any other pod by IP without NAT. This is the **flat network model**.

  

This is implemented differently per CNI plugin (Calico, Flannel, Cilium, etc.) but the contract is always the same: pod-to-pod communication anywhere in the cluster.

  

**How Services work (kube-proxy iptables mode):**

1. You create a Service with ClusterIP `10.96.0.1`

2. kube-proxy watches for Service and Endpoint changes

3. For every Service, kube-proxy writes iptables rules: "traffic to `10.96.0.1:80` → randomly pick one of these backend pod IPs"

4. These rules live on every single node

  

When Cilium/eBPF mode is used, this is done with eBPF programs in the kernel instead of iptables, which is significantly faster at scale.

  

**DNS in Kubernetes:**

CoreDNS runs in the cluster. Every service gets a DNS name: `<service-name>.<namespace>.svc.cluster.local`. Pods can reach services by just their short name within the same namespace.

  

---

  

### Chapter 19 — Configuration Management and Secrets

  

**The 12-factor app principle:** Configuration that varies between environments should come from the environment, not the code.

  

In Kubernetes, this means:

- Use `ConfigMaps` for non-sensitive config

- Use `Secrets` for sensitive data (but note: Secrets are only base64 encoded in etcd by default — use Sealed Secrets or Vault for true encryption)

- Inject both as env vars or volume mounts

  

**External Secrets Operator** and **HashiCorp Vault** are the production-grade solutions for secrets management. Worth knowing they exist.

  

---

  

### Chapter 20 — The kubectl Mental Model

  

`kubectl` is to Kubernetes what `docker` is to Docker — the CLI client. It talks to the kube-apiserver REST API.

  

**The most useful commands to internalize:**

  

```bash

# Apply/update any resource

kubectl apply -f deployment.yaml

  

# See what's running

kubectl get pods -n mynamespace -o wide

  

# Deep inspect any object

kubectl describe pod <pod-name>

  

# See logs

kubectl logs <pod-name> -f --previous

  

# Execute into a running container

kubectl exec -it <pod-name> -- bash

  

# See recent events (great for debugging)

kubectl get events --sort-by='.lastTimestamp'

  

# Port forward for local testing

kubectl port-forward svc/api 3000:3000

  

# Watch resources in real time

kubectl get pods -w

  

# Delete and let the controller recreate

kubectl delete pod <pod-name>

```

  

**Tip:** `kubectl explain <resource>.<field>` gives inline documentation for any field. `kubectl explain deployment.spec.strategy` is faster than Googling.

  

---

  

## Learning Path & Recommended Order

  

### Phase 1: Docker Foundations (2–3 weeks)

1. Install Docker Desktop. Set up a Linux VM or use a DigitalOcean droplet to also see Docker on real Linux.

2. Read about Linux namespaces and cgroups (just conceptually — 30 min articles)

3. Understand images, layers, OverlayFS

4. Write Dockerfiles for your own projects — start with a language you know

5. Master `docker run` flags by using them, not memorizing them

6. Learn Docker Compose by running a real multi-service app (e.g., a web app + postgres + redis)

7. Deep dive: `docker inspect`, `docker image history`, `docker system df`

  

### Phase 2: Docker Mastery (1–2 weeks)

1. Multi-stage builds

2. Networking deep dive: create networks, see how DNS works between containers

3. Volume management: understand bind mount vs named volume semantics

4. Write a non-trivial `docker-compose.yml` with health checks, depends_on, overrides

5. Read through a production-grade open-source project's Dockerfile

  

### Phase 3: Kubernetes Fundamentals (3–4 weeks)

1. Run a local cluster with `minikube` or `kind` (Kubernetes in Docker)

2. Master the core objects: Pod, Deployment, Service, ConfigMap, Secret

3. Understand labels and selectors deeply

4. Deploy something real — a stateless API service

5. Break things: delete pods, watch controllers react

6. Learn `kubectl` deeply — don't copy-paste, type commands

  

### Phase 4: Kubernetes Depth (2–3 weeks)

1. Ingress and ingress controllers

2. Persistent storage (PVC, PV, StorageClass)

3. Rolling updates, readiness probes, zero-downtime deploys

4. Resource requests and limits

5. Namespaces, RBAC basics

6. ConfigMaps and Secrets (and their limitations)

  

### Phase 5: Real World (ongoing)

1. Deploy a full application to a managed cluster (EKS, GKE, or k3s on a VPS)

2. Set up monitoring (Prometheus + Grafana)

3. Set up centralized logging (Loki or ELK)

4. Learn Helm for packaging Kubernetes apps

5. Explore GitOps with ArgoCD

  

---

  

## Best Resources

  

### Docker

- **Official docs** — docker.com/docs is genuinely good

- **"Docker Deep Dive" by Nigel Poulton** — short, clear, mental-model focused

- **Liz Rice's talks on containers from scratch** — builds a container from Linux primitives in Go. Nothing will make namespaces click faster.

- **Dive** (tool) — visually inspect image layers: `github.com/wagoodman/dive`

  

### Kubernetes

- **"Kubernetes Up & Running" (O'Reilly)** — the standard reference

- **Killer.sh / KodeKloud** — hands-on labs, practice environments

- **kubectl explain** — use it constantly

- **CNCF landscape** — once you're comfortable, browse this to see what the ecosystem looks like

  

---

  

## Debugging Cheat Sheet

  

### Docker

| Symptom | Command |

|---|---|

| Container exits immediately | `docker logs <id>` — look at the last lines |

| Can't connect to container port | `docker inspect <id>` → check PortBindings |

| Out of disk space | `docker system df` then `docker system prune` |

| Image won't build | `docker build --no-cache` to rule out caching issues |

| Performance is slow | `docker stats` — check CPU/memory pressure |

  

### Kubernetes

| Symptom | Command |

|---|---|

| Pod stuck in Pending | `kubectl describe pod` → check Events, look for resource or scheduler issues |

| Pod in CrashLoopBackOff | `kubectl logs <pod> --previous` — see last run's output |

| Service not routing | `kubectl get endpoints <svc>` — are any pods selected? |

| Container OOMKilled | `kubectl describe pod` → check last state reason |

| General confusion | `kubectl get events --sort-by='.lastTimestamp'` |

  

---

  

## Final Mental Notes

  

- **Immutability is the principle.** Don't SSH into containers to fix things. Fix the Dockerfile or the config, redeploy. Containers you've shelled into and modified are untrustworthy.

- **Logs go to stdout/stderr.** That's it. No log files in containers. The container runtime captures stdout and makes it available via `docker logs` or `kubectl logs`.

- **One process per container** is a guideline, not a law. But if you find yourself needing two unrelated processes, ask if they should be two containers.

- **Stateless is easier.** Containers running stateless apps (APIs, workers) are easy to scale, replace, and move. Stateful containers (databases) are harder and often better served by managed cloud services until you really know what you're doing.

- **Don't run databases in Kubernetes in production until you understand Kubernetes well.** Use managed RDS/CloudSQL/etc. first.

- **The `latest` tag is a lie.** Always pin image versions in production. `nginx:latest` today is not `nginx:latest` in three months.