```bash
user@Ubuntu:~$ lspci | grep -i nvidia
01:00.0 VGA compatible controller: NVIDIA Corporation GA107GL [RTX A400] (rev a1)
01:00.1 Audio device: NVIDIA Corporation GA107 High Definition Audio Controller (rev a1)
09:00.0 VGA compatible controller: NVIDIA Corporation GB202 [GeForce RTX 5090] (rev a1)
09:00.1 Audio device: NVIDIA Corporation GB202 High Definition Audio Controller (rev a1)
```

```bash
user@Ubuntu:~$ dpkg -l | grep -i nvidia-driver
ii  nvidia-driver-595-open                          595.58.03-0ubuntu0.24.04.1                       amd64        NVIDIA driver (open kernel) metapackage
```

```bash
user@Ubuntu:~$ cat /proc/driver/nvidia/version
NVRM version: NVIDIA UNIX Open Kernel Module for x86_64  595.58.03  Release Build  (dvs-builder@U22-I3-AM25-28-3)  Tue Mar 17 19:55:10 UTC 2026
GCC version:  gcc version 13.3.0 (Ubuntu 13.3.0-6ubuntu2~24.04.1)
user@Ubuntu:~$
```

```bash
user@Ubuntu:~$ nvidia-smi --query-gpu=name --format=csv,noheader
NVIDIA RTX A400
NVIDIA GeForce RTX 5090
```

```bash
user@Ubuntu:~$ dpkg -l | grep nvidia-container-toolkit
user@Ubuntu:~$
user@Ubuntu:~$ which nvidia-ctk
user@Ubuntu:~$
user@Ubuntu:~$ cat /etc/docker/daemon.json 2>/dev/null
user@Ubuntu:~$
```

```bash
user@Ubuntu:~$ docker info | grep -i runtime
 Runtimes: io.containerd.runc.v2 runc
 Default Runtime: runc
```

## Newtork

```bash
user@Ubuntu:~$ curl -4 ifconfig.me
218.219.100.1
```

Allowing SSH
```bash
user@Ubuntu:~$ sudo ufw allow ssh
Skipping adding existing rule
Skipping adding existing rule (v6)
user@Ubuntu:~$ sudo ufw allow in on tailscale0
Rule added
Rule added (v6)
user@Ubuntu:~$ sudo ufw allow 80/tcp
Rule added
Rule added (v6)
user@Ubuntu:~$ sudo ufw allow 443/tcp
Rule added
Rule added (v6)
user@Ubuntu:~$ sudo ufw default deny incoming
Default incoming policy changed to 'deny'
(be sure to update your rules accordingly)
user@Ubuntu:~$ sudo ufw default allow outgoing
Default outgoing policy changed to 'allow'
(be sure to update your rules accordingly)
user@Ubuntu:~$ sudo ufw enable
Command may disrupt existing ssh connections. Proceed with operation (y|n)? y
Firewall is active and enabled on system startup
```


