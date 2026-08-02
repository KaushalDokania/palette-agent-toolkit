---
name: diagnose-cluster
description: Diagnose a degraded, error, or unhealthy cloud cluster. Use when a cluster is in an error state, stuck or failing to provision, or behaving unexpectedly. Accepts a cluster name or UID as argument.
---

# Diagnose Cloud Cluster

Perform a structured triage of a Palette cloud cluster. Argument: `$ARGUMENTS` (cluster name or UID — if blank, list clusters and ask the user to pick one).

## Steps

1. **Identify the cluster**
   - If `$ARGUMENTS` is blank: call `read_clusters` and present names + current status. Ask the user which cluster to diagnose.
   - If `$ARGUMENTS` looks like a UID (long alphanumeric string): call `read_clusters` with `uid=$ARGUMENTS` directly to resolve it.
   - If `$ARGUMENTS` looks like a name: call `read_clusters` with `filters={name:{contains:"$ARGUMENTS"}}`. If more than one cluster matches, list the matches (name, project, state) and ask the user to pick before proceeding. Only when exactly one match is found, extract its UID for subsequent calls.
   - Also capture `spec.cloud_type` (e.g. `aws`, `azure`, `gcp`, `eks`, `aks`) from the same `read_clusters` result — needed later to route the kube-level escalation tier (step K3) to the right protocol.

2. **Read cluster status** (requires UID from step 1)
   - Call `read_cluster_status` with the cluster UID and `fields=["status"]`.
   - Surface: overall health, condition messages, last transition time, any error codes.

3. **Triage cluster events** (requires UID from step 1)
   - Call `read_events` with `object_kind="spectrocluster"`, `object_uid=<cluster uid>`, and `limit=20`.
   - Look for events with `severity=Error` and any `reason` beginning with `Failed` — these usually pinpoint the failing reconcile step or pack.
   - Correlate the most recent error events with the condition messages from step 2 to confirm the root cause.
   - **Note:** `read_events` requires a `palette-mcp` binary that exposes the `read_events` tool. If the tool is unavailable in this session, skip this step and rely on the status and observability signals.

4. **Read scan and backup observability** (requires UID from step 1)
   - Call `read_cluster_observability` with the cluster UID and `include=["scans","backup","restore"]`.
   - Surface: compliance scan results (last scan time, pass/fail status), backup status (last backup time, success/failure), restore status if applicable.
   - A failed or overdue scan, or a failed backup, can be a secondary signal of cluster health degradation.

5. **Check attached profiles and packs** (requires UID from step 1)
   - Call `read_attached_profiles_to_cluster` with the cluster UID.
   - Surface: profile names, pack versions, any packs in a failed or pending state.

6. **Synthesise findings**
   - Group findings into: **Blockers** (likely root cause), **Warnings** (contributing factors), **Info** (context).
   - For each blocker, suggest a remediation action based on the error message and pack state.
   - If root cause is unclear, suggest next steps: check cloud account credentials (`read_cloud_accounts`), review pack compatibility.
   - **Let the failing condition point to the check.** `read_cluster_status` condition types are the fastest router to what to look at next — not an exhaustive list, just the common cases observed live:
     - `CloudInfrastructureReady=False` → infra provisioning — `awscluster`/`awsmachine` (or provider equivalent) + cloud-account credentials (K4 Protocol A).
     - `BootstrappingDone` false or `BootstrapReady=False` → the node never finished bootstrapping — node-level cloud-init/kubelet/containerd logs (K6 below).
     - `KubeConfigReady=False` → control-plane isn't up — inspect `kubeadmcontrolplane`.
     - `ImageResolutionDone=False` → image/registry resolution failed.
     - `ImagePullSecretPropagationDone=False` → registry/pack pull-secret propagation failed.
   - **Escalation decision:**
     - If the root cause is identified and actionable at the management-plane level (pack, profile, scan, or config issue) → go to step 7.
     - If conditions/events instead point to an infra/node/pod/provisioning failure (nodes `NotReady`, a machine/machinepool not `Ready`, provisioning stuck, control-plane not available) **and** more detail is needed to pin down the cause → escalate to **Kube-level triage (escalation)** below, then return here to fold those findings back into Blockers/Warnings/Info before step 7.

## Kube-level triage (escalation)

Only reached when step 6 escalates. This tier reads the target cluster's own kube API directly (not just the management plane) to see CAPI/node/pod state. v1 is allowlist-only: it uses the **admin** kubeconfig as-is — there is no read-only credential minting and no write path to the customer cluster. Safety comes entirely from the read-only enforcement in K2.

K1. **Preflight**
   - Ensure `kubectl` is available in this session.
   - Fetch the admin kubeconfig via the `read_cluster_kubeconfig` MCP tool with `mode=admin`, `write_path=/tmp/palette-diag-<uid>` (substitute the real cluster UID).
   - **Check the fetch result before probing.** Look for a `written_to` field:
     - Present → the file was actually written; proceed to the reachability probe below using that path (matches `write_path` when the write succeeded).
     - Absent → the kubeconfig was **not** written to disk (check `warnings` — typically the MCP server needs `--allow-write`). Report that the kubeconfig could not be written locally and the kube-tier reachability probe can't run — do **not** run the probe against a path that was never created, and do **not** conclude or imply the cluster itself is unreachable. Fall back to the Tier-0 findings from step 6 and stop — do not proceed to K2.
   - Run a bounded reachability probe (only once `written_to` confirms the file exists): `KUBECONFIG=/tmp/palette-diag-<uid> kubectl --request-timeout=10s get ns`.
   - If *this* probe fails, report that the cluster API is unreachable from here (likely a private/edge cluster without the `spectro-proxy` pack), fall back to the Tier-0 findings from step 6, and stop — do not proceed to K2.

K2. **No automatic enforcement — read-only by convention only**
   - There is no pre-execution hook or any other automatic backstop blocking mutating or secret-reading `kubectl` calls. The intended safety boundary is a **read-only kubeconfig** for the target cluster instead of the admin one — that's not wired up yet. Today this tier runs on the **admin** kubeconfig with no automatic enforcement at all.
   - See [`KUBECTL_GUARDRAILS.md`](./KUBECTL_GUARDRAILS.md) for the full picture, including the optional (opt-in, not auto-applied) permission-template layer.
   - The commands in K4 must still be *chosen* to be read-only by convention/discipline before running them — there's no backstop catching a mistake.

K3. **Route on managed vs. self-managed control plane** (`cloud_type` captured in step 1)
   - Managed node pools — `eks`, `aks`, **`gke`** → **Protocol B**.
   - Infra / self-managed control plane — `aws`, `azure`, `gcp` **as IaaS** → **Protocol A**. This includes plain `gcp` as a cloud_type: a bare `gcp` cluster (no managed designation) is GCP IaaS and routes to Protocol A. Only `gke` specifically is the managed offering and routes to Protocol B — don't conflate the two.

**Pivot check (before K4):** for Protocol A, check whether the failure is **pre-pivot** or **post-pivot** before running K4's commands: if the first control-plane node never came up, the cluster's CAPI resources still live in the **management-plane/PCG kubeconfig**, not the workload cluster's — K4 run against the workload kubeconfig will look empty. Once the first control-plane node is up, CAPI resources have pivoted into the **workload cluster's own** kubeconfig — the normal case this skill already assumes. If K4's commands return "no resources found" unexpectedly, that's often a sign of pointing at the wrong kubeconfig for the failure phase, not proof the cluster has no CAPI objects at all.

K4. **Protocol A — infra/IaaS clusters** (VERIFIED LIVE — CAPI resources are namespaced under `cluster-<uid>`, so use `-A` to see them regardless of exact namespace)
   ```
   kubectl get spc -A
   kubectl get cluster,machinedeployment,machinepool,machine,kubeadmcontrolplane -A
   kubectl get awscluster,awsmachine -A          # or azurecluster/azuremachine, gcpcluster/gcpmachine per cloud_type
   kubectl describe machine <not-ready-machine> -n cluster-<uid>   # check InfrastructureReady / BootstrapReady conditions + provisioning order via creationTimestamp
   kubectl describe node <NotReady node>
   kubectl get events -A --sort-by=.lastTimestamp
   kubectl get pods -A --field-selector=status.phase!=Running       # esp. kube-system, CNI, CAPI controllers
   ```

   **Protocol B — EKS/AKS/GKE managed pools** — run ONLY the block matching `cloud_type`; the other providers' CRDs are not installed and will error (`the server doesn't have a resource type ...`).
   ```
   kubectl get spc -A
   kubectl get machinepool -A -o wide
   # cloud_type=eks (CAPA):
   kubectl get awsmanagedcontrolplane,awsmanagedmachinepool -A
   kubectl describe awsmanagedcontrolplane,awsmanagedmachinepool -A
   # cloud_type=aks (CAPZ):
   kubectl get azuremanagedcontrolplane,azuremanagedmachinepool -A
   kubectl describe azuremanagedcontrolplane,azuremanagedmachinepool -A
   # cloud_type=gke (CAPG):
   kubectl get gcpmanagedcontrolplane,gcpmanagedcluster,gcpmanagedmachinepool -A
   kubectl describe gcpmanagedcontrolplane,gcpmanagedcluster,gcpmanagedmachinepool -A
   # plus node/pod health as in Protocol A
   ```

   **Check the CAPI controller-manager's own logs.** For Protocol B, the CR status/conditions above often don't show the actual cloud-API rejection (quota exceeded, IAM/permission denied, bad parameter) — that surfaces in the controller-manager's logs instead. Run the one block matching the routed `cloud_type`:
   ```
   kubectl -n capa-system logs deploy/capa-controller-manager --tail=200   # eks
   kubectl -n capz-system logs deploy/capz-controller-manager --tail=200   # aks
   kubectl -n capg-system logs deploy/capg-controller-manager --tail=200   # gke
   ```

K5. **Synthesise + wipe**
   - Fold kube-level findings into the Blockers/Warnings/Info from step 6.
   - Updated ceiling disclaimer: "This is kube-API-level triage — it shows WHAT/WHERE is stuck (CAPI/machine/node/pod state + events). Node-level logs (cloud-init/kubelet/containerd) are now in scope for Protocol A via K6 below, when the signals point there. What's still out of reach: it needs the user's own SSH key and network reach to the node or its bastion, and Protocol B (managed EKS/AKS) nodes are not SSH-diagnosed here — there's no customer-side node to SSH into."
   - Wipe the fetched admin kubeconfig: `rm -f /tmp/palette-diag-<uid>`.
   - **Escalate further?** Only for Protocol A (K3): if the findings point at a node/bootstrap problem — node `NotReady`, `BootstrapReady=False`, or a machine stuck with `InfrastructureReady=True` but never progressing — continue to **K6** below. Otherwise, return to step 7.

K6. **Node-level triage (Protocol A only, SSH)**

   Reached when K4/K5 findings point at a node or bootstrap problem: node `NotReady`, `BootstrapReady=False`, or a machine stuck with `InfrastructureReady=True` but never progressing to `Ready`. Not applicable to Protocol B (managed EKS/AKS/GKE) — those nodes aren't SSH-reachable/owned the same way.

   - **How cloud-init works (brief):** on first boot, a node runs cloud-init, which executes the Palette/kubeadm bootstrap — installs the kubelet + container runtime, then joins the cluster. A node that provisioned (`InfrastructureReady=True`) but never went `Ready` usually failed somewhere in that sequence: cloud-init itself, or the kubelet/containerd startup that follows it. That's exactly what the logs below show.
   - **Ask the user for the SSH key.** This step needs the path to the node's SSH private key (e.g. `~/.ssh/id_rsa`). Ask for the **path** — never ask the user to paste the key content into chat, and never read the key file yourself. If they don't have it or decline, stop the node step here and report the K4/K5 kube-level findings only.
   - **Resolve the node address.**
     - `kubectl get machine -A -o wide` / `kubectl get nodes -o wide` to find the target node's address.
     - Cluster nodes are typically on private IPs. Get the bastion IP from the `awscluster` resource: `kubectl get awscluster -A -o jsonpath='{.items[*].status.bastion}'` (check `.spec.bastion` too if `.status` is empty).
     - For a private node, SSH via the bastion using ProxyJump: `ssh -i <key> -J <user>@<bastion-ip> <user>@<node-private-ip> "<read-only command>"`.
   - **Collect logs** (read-only by convention — there is no automatic enforcement over SSH either; nothing blocks a mutating or sensitive-path command from being issued, the operator running this skill is trusted to run only the listed read-only log commands below and not deviate):
     ```
     sudo cloud-init status --long
     sudo cat /var/log/cloud-init-output.log
     sudo cat /var/log/cloud-init.log
     sudo journalctl -u kubelet --no-pager | tail -n 200
     sudo journalctl -u containerd --no-pager | tail -n 200
     ```
   - **Lower-risk first attempt:** the mgmt-plane log bundle (`spectro_logs.zip`, downloadable from the Palette UI) may already contain the same cloud-init logs without needing SSH at all — worth checking before reaching for SSH access.
   - **No guardrail note:** as with K2/K4, there is no hook or other backstop denying non-read-only commands or reads of sensitive paths (SSH/kube-PKI/cloud-credential files, `/etc/shadow`, etc.) issued over SSH. The commands above are read-only because they're the only ones this step lists — not because anything would stop a different command.
   - Fold node-level findings into the Blockers/Warnings/Info synthesis, then return to step 7.

7. **Ask if user wants to act**
   - If write tools are available in this session, offer to proceed with any applicable remediation.
   - Otherwise, summarise findings and link to relevant Palette docs where applicable.
