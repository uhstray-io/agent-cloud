# UhhCraft GPU Passthrough — sub-plan

> **Status:** Draft, awaiting user input on §1.
> **Parent plan:** [`WEBSMITH-INTEGRATION-PLAN.md`](./WEBSMITH-INTEGRATION-PLAN.md) Phase 6.
> **Scope:** Provision the two GPU VMs (`inference-comfyui-svc-01` and `inference-hunyuan3d-svc-01`) on the agent-cloud Proxmox cluster with NVIDIA PCIe passthrough.

This document is the single procedure for getting from "the GPU host(s) physically exist in the rack" to "the inference-* deploy playbooks complete successfully and the FastAPI wrappers can `import torch` + `torch.cuda.is_available()`."

It is hardware-coupled. Do not start until §1 is resolved.

---

## 1. Decision: where do the GPUs live? (Resolve before anything else)

UhhCraft's signed SPEC says: *"RTX 5070 AI machines on the same network."* That implies the GPUs are already present, but it doesn't say whether they are:

- **Option A — Already in the Proxmox cluster.** A Proxmox node with the GPU(s) installed, IOMMU enabled in BIOS, and at least one passthrough VM working today.
- **Option B — Physical host exists, not yet in cluster.** Hardware racked, but the box runs bare metal Ubuntu / Windows / nothing-yet and is not joined to the Proxmox cluster.
- **Option C — Greenfield.** No GPU host exists. Hardware needs to be procured / racked / cabled / installed first.

**Until this is answered, the rest of the document branches:** §3 (host config) is a verification pass under A, a full setup under B, and out-of-scope-until-hardware under C.

Capture the answer here, dated and signed:

```text
Decision: (A | B | C)
Host(s): <hostname>, <hostname>
GPUs:    <count> × <model>           e.g., 2 × RTX 5070 (one each in node-X and node-Y)
                                     or 2 × RTX 5070 in a single node
Decided by: <name>, <date>
```

---

## 2. Capacity decision: one GPU host or two?

Each inference service needs ~12GB of dedicated VRAM:
- **ComfyUI + Flux.1 Schnell (fp8):** ~10GB during generation.
- **Hunyuan3D-2-mini:** ~8GB resident + ~2-4GB transient during generation.

An RTX 5070 has 12GB VRAM. **Two services on one GPU is not viable** — they will OOM under concurrent load.

Three topologies are workable; pick one:

| Topology | GPUs needed | VMs | Notes |
|----------|-------------|-----|-------|
| **A. Two hosts, one GPU each** | 1 + 1 (different nodes) | `inference-comfyui-svc-01` on node-X, `inference-hunyuan3d-svc-01` on node-Y | Cleanest blast radius; each service can be rebooted independently |
| **B. One host, two GPUs** | 2 (same node) | Both VMs on the same node, each gets its own PCIe device | Lower hardware cost; node maintenance takes both services down |
| **C. One GPU, time-shared** | 1 | Only one inference service deployed; the other is disabled in inventory | Stopgap only — UhhCraft's flows expect both. Acceptable for dev. |

Record the choice in `vm-specs.example.yml` (and the real `vm-specs.yml` in site-config). For topology B, the two VMs use different `hostpci` entries pointing at different PCIe slots.

---

## 3. Host preparation (Proxmox)

Skip §3 entirely under Option A — assume already configured, jump to §6 (verification) and only return here if something fails.

### 3.1 BIOS / UEFI

In the host firmware:
- **Intel CPU:** Enable `VT-d`.
- **AMD CPU:** Enable `AMD-Vi` / `IOMMU`.
- Disable CSM / enable UEFI boot.
- Disable Secure Boot (it interferes with the in-VM NVIDIA driver signing).

Reboot after BIOS changes.

### 3.2 Proxmox kernel command line

Edit `/etc/default/grub` (or `/etc/kernel/cmdline` if using systemd-boot) to add IOMMU + early VFIO binding. The defaults to merge into `GRUB_CMDLINE_LINUX_DEFAULT`:

```text
intel_iommu=on iommu=pt pcie_acs_override=downstream,multifunction nofb nomodeset video=vesafb:off video=efifb:off
```

(For AMD substitute `amd_iommu=on`.)

```bash
update-grub
reboot
```

Verify after reboot:

```bash
dmesg | grep -e DMAR -e IOMMU
# Should show "DMAR: IOMMU enabled" or equivalent.
```

### 3.3 Identify the GPU

```bash
lspci -nn | grep -i nvidia
# 01:00.0 VGA compatible controller [0300]: NVIDIA Corporation ... [10de:xxxx] (rev a1)
# 01:00.1 Audio device [0403]: NVIDIA Corporation ... [10de:yyyy] (rev a1)
```

Note both PCIe IDs (`01:00.0` and `01:00.1`). The GPU and its onboard audio device are usually in the same IOMMU group and must be passed through together.

Verify the IOMMU group:

```bash
for d in /sys/kernel/iommu_groups/*/devices/*; do
  n=${d#*/iommu_groups/*}; n=${n%%/*}
  printf 'IOMMU Group %s ' "$n"
  lspci -nns "${d##*/}"
done | grep -i nvidia
```

All NVIDIA devices for one GPU should be in the same group. If they're spread across groups, `pcie_acs_override` (from §3.2) usually fixes it for consumer hardware; on server-grade platforms it's usually fine without.

### 3.4 Bind GPU to VFIO at boot

```bash
# /etc/modprobe.d/vfio.conf
options vfio-pci ids=10de:xxxx,10de:yyyy
softdep nvidia pre: vfio-pci
softdep nouveau pre: vfio-pci
```

Replace `10de:xxxx,10de:yyyy` with the `[10de:xxxx]` IDs from `lspci -nn` above (GPU + audio).

Blacklist the host nvidia / nouveau drivers so the host never claims the GPU:

```bash
# /etc/modprobe.d/blacklist-nvidia.conf
blacklist nouveau
blacklist nvidia
blacklist nvidiafb
blacklist nvidia_drm
```

Regenerate initramfs and reboot:

```bash
update-initramfs -u
reboot
```

Verify after reboot:

```bash
lspci -nnk -s 01:00
# Kernel driver in use: vfio-pci   ← this is what we want
```

If it says `nvidia` or `nouveau`, the bind failed — recheck the modprobe + blacklist files.

---

## 4. VM provisioning

Use `platform/playbooks/provision-vm.yml` driven from the `vm-specs.yml` entries for `inference-comfyui` and `inference-hunyuan3d` (see [`platform/hypervisor/proxmox/vm-specs.example.yml`](../../platform/hypervisor/proxmox/vm-specs.example.yml)).

Two additions vs CPU-only VMs:

### 4.1 Machine type must be q35

PCIe passthrough requires the q35 machine type. The provisioning playbook sets this when `hostpci` is present in `vm-specs.yml`. Verify after creation:

```bash
qm config <VMID> | grep machine
# machine: q35
```

### 4.2 OVMF / UEFI

GPU passthrough is significantly easier with UEFI boot than SeaBIOS. Set `bios: ovmf` on the VM and provision an EFI disk. The Proxmox API call in `provision-vm.yml` includes this when the `hostpci` field is set; verify after creation.

---

## 5. NVIDIA driver placement

**Drivers run inside the VM, not on the host.** The host's job is purely to hand the PCIe device through; the VM's job is to install and own the driver.

Inside each inference VM (post-cloud-init):

```bash
# Ubuntu 24.04
sudo apt update
sudo ubuntu-drivers install nvidia:570
# or whichever version matches the CUDA 12.4 base image used by
# platform/services/inference-comfyui/deployment/Dockerfile

sudo reboot
nvidia-smi    # should list the GPU
```

Then run [`platform/playbooks/tasks/install-nvidia-toolkit.yml`](../../platform/playbooks/tasks/install-nvidia-toolkit.yml) to install the NVIDIA Container Toolkit + CDI so Podman can pass the GPU through to containers.

After both steps, the GPU probe in `install-nvidia-toolkit.yml` should print the GPU name.

---

## 6. Verification

End-to-end success looks like:

```bash
# 1. On the Proxmox host: GPU bound to VFIO.
lspci -nnk -s 01:00 | grep -i 'kernel driver'
# Kernel driver in use: vfio-pci

# 2. Inside the inference VM: nvidia-smi works.
ssh inference-comfyui-svc-01 nvidia-smi --query-gpu=name --format=csv,noheader
# NVIDIA GeForce RTX 5070

# 3. Inside a probe container: nvidia-smi works.
ssh inference-comfyui-svc-01 \
  podman run --rm --device nvidia.com/gpu=all \
    docker.io/nvidia/cuda:12.4.1-base-ubuntu22.04 nvidia-smi --query-gpu=name --format=csv,noheader
# NVIDIA GeForce RTX 5070

# 4. Run the actual deploy playbook.
ansible-playbook -i inventory/local.yml platform/playbooks/deploy-inference-comfyui.yml
# Should complete cleanly through Phase 3 verification.

# 5. Hit the wrapper.
curl http://<inference-comfyui-vm-ip>:8189/health
# {"status":"ok","version":"...","uptime_seconds":...}
```

If step 4's `tasks/install-nvidia-toolkit.yml` fails with "GPU probe FAILED", recheck §3 + §5 — the host is not handing the GPU through to the VM.

---

## 7. Recovery / rebuild

Both GPU VMs are stateless except for:

- **Hunyuan3D weights** on the host at `/srv/hunyuan3d/weights` (mounted into the VM via the compose `volumes:` block). Downloading once and keeping on host disk means a VM rebuild does not re-download ~5GB.
- **MinIO data volumes** inside each VM (`hunyuan3d-minio`, `comfyui-minio` — Podman volumes). These store generated assets and can be repopulated if needed by re-running generation jobs.

Rebuild procedure:

```bash
# 1. Backup the MinIO volume if you care about generated history.
ssh <vm> 'podman run --rm -v <vm>-minio:/data -v /tmp:/backup alpine \
  tar czf /backup/<vm>-minio-$(date +%F).tar.gz -C /data .'

# 2. Destroy the VM (Proxmox UI or qm destroy <VMID>).
# 3. Re-provision via provision-vm.yml.
# 4. Re-run install-nvidia-toolkit + deploy-inference-<svc>.yml.
# 5. (Optional) restore MinIO from backup.
```

For Hunyuan3D weights, the host directory `/srv/hunyuan3d/weights` is independent of the VM and survives rebuilds.

---

## 8. Open questions

These all surfaced during plan drafting. None block §1 from being answered:

1. **GPU-only-on-demand** — should the inference VMs auto-shut-down when idle to save power? Not in v1 (River jobs need a warm sidecar). Revisit at the 3-month mark.
2. **Cross-VM GPU sharing** — Proxmox supports `mediated devices` (vGPU) on some hardware, allowing a single GPU to be split. The RTX 5070 does not support vGPU. Skipping.
3. **Hot-add / hot-remove** — PCIe hot-add of the GPU is possible on Proxmox but flaky for consumer NVIDIA cards. Treat each VM as needing a reboot to gain or lose the GPU.
4. **Backup of model weights** — Hunyuan3D weights are 5GB and live on host disk. If the host disk fails, re-download from HuggingFace (~30 min). Documented; not automated.
5. **Driver version pinning** — `ubuntu-drivers install nvidia:570` may pull a different version over time. Pin once verified working, then bump deliberately with smoke tests.

---

## 9. Definition of done

The sub-plan is complete when **all** of the following hold:

- [ ] §1 decision recorded (A/B/C, host names, GPU count + model, date, signoff).
- [ ] §2 topology recorded in `vm-specs.example.yml` (and the real `vm-specs.yml`).
- [ ] §3 host preparation done on each GPU host (or verified pre-existing under Option A).
- [ ] §4 VMs provisioned and report `machine: q35` + `bios: ovmf`.
- [ ] §5 NVIDIA driver installed inside each VM; `nvidia-smi` works.
- [ ] §6 end-to-end verification: probe container sees the GPU; deploy playbook completes; `/health` returns 200.
- [ ] §7 recovery procedure exercised at least once (test rebuild of one VM).

When all boxes are checked, Phase 6 of [`WEBSMITH-INTEGRATION-PLAN.md`](./WEBSMITH-INTEGRATION-PLAN.md) can be marked complete and Phase 7 (Semaphore templates) can proceed.
