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
   - Run a bounded reachability probe: `KUBECONFIG=/tmp/palette-diag-<uid> kubectl --request-timeout=10s get ns`.
   - If the probe fails, report that the cluster API is unreachable from here (likely a private/edge cluster without the `spectro-proxy` pack), fall back to the Tier-0 findings from step 6, and stop — do not proceed to K2.

K2. **Read-only enforcement**
   - All commands in K4 are safe read-only calls: the palette plugin's `PreToolUse` hook (`kubectl-guard`) auto-blocks any mutating or secret-reading `kubectl` invocation before it runs. See [`KUBECTL_GUARDRAILS.md`](./KUBECTL_GUARDRAILS.md) for how that enforcement works. Do not attempt to work around it.

K3. **Route on `cloud_type`** (captured in step 1)
   - Infra / self-managed (`aws`, `azure`, `gcp`) → **Protocol A**.
   - Managed node pools (`eks`, `aks`) → **Protocol B**.

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

   **Protocol B — EKS/AKS managed pools** — run ONLY the block matching `cloud_type`; the other provider's CRDs are not installed and will error (`the server doesn't have a resource type ...`).
   ```
   kubectl get spc -A
   kubectl get machinepool -A -o wide
   # cloud_type=eks (CAPA):
   kubectl get awsmanagedcontrolplane,awsmanagedmachinepool -A
   kubectl describe awsmanagedcontrolplane,awsmanagedmachinepool -A
   # cloud_type=aks (CAPZ):
   kubectl get azuremanagedcontrolplane,azuremanagedmachinepool -A
   kubectl describe azuremanagedcontrolplane,azuremanagedmachinepool -A
   # plus node/pod health as in Protocol A
   ```

K5. **Synthesise + wipe**
   - Fold kube-level findings into the Blockers/Warnings/Info from step 6.
   - Updated ceiling disclaimer: "This is kube-API-level triage — it shows WHAT/WHERE is stuck (CAPI/machine/node/pod state + events). Node-level logs (cloud-init/kubelet/containerd) are now in scope for Protocol A via K6 below, when the signals point there. What's still out of reach: it needs the user's own SSH key and network reach to the node or its bastion, and Protocol B (managed EKS/AKS) nodes are not SSH-diagnosed here — there's no customer-side node to SSH into."
   - Wipe the fetched admin kubeconfig: `rm -f /tmp/palette-diag-<uid>`.
   - **Escalate further?** Only for Protocol A (K3): if the findings point at a node/bootstrap problem — node `NotReady`, `BootstrapReady=False`, or a machine stuck with `InfrastructureReady=True` but never progressing — continue to **K6** below. Otherwise, return to step 7.

K6. **Node-level triage (Protocol A only, SSH)**

   Reached when K4/K5 findings point at a node or bootstrap problem: node `NotReady`, `BootstrapReady=False`, or a machine stuck with `InfrastructureReady=True` but never progressing to `Ready`. Not applicable to Protocol B (managed EKS/AKS) — those nodes aren't SSH-reachable/owned the same way.

   - **How cloud-init works (brief):** on first boot, a node runs cloud-init, which executes the Palette/kubeadm bootstrap — installs the kubelet + container runtime, then joins the cluster. A node that provisioned (`InfrastructureReady=True`) but never went `Ready` usually failed somewhere in that sequence: cloud-init itself, or the kubelet/containerd startup that follows it. That's exactly what the logs below show.
   - **Ask the user for the SSH key.** This step needs the path to the node's SSH private key (e.g. `~/.ssh/id_rsa`). Ask for the **path** — never ask the user to paste the key content into chat, and never read the key file yourself. If they don't have it or decline, stop the node step here and report the K4/K5 kube-level findings only.
   - **Resolve the node address.**
     - `kubectl get machine -A -o wide` / `kubectl get nodes -o wide` to find the target node's address.
     - Cluster nodes are typically on private IPs. Get the bastion IP from the `awscluster` resource: `kubectl get awscluster -A -o jsonpath='{.items[*].status.bastion}'` (check `.spec.bastion` too if `.status` is empty).
     - For a private node, SSH via the bastion using ProxyJump: `ssh -i <key> -J <user>@<bastion-ip> <user>@<node-private-ip> "<read-only command>"`.
   - **Collect logs** (read-only; the `kubectl-guard` `PreToolUse` hook enforces this over SSH the same way it does for `kubectl`):
     ```
     sudo cloud-init status --long
     sudo cat /var/log/cloud-init-output.log
     sudo cat /var/log/cloud-init.log
     sudo journalctl -u kubelet --no-pager | tail -n 200
     sudo journalctl -u containerd --no-pager | tail -n 200
     ```
   - **Guardrail note:** the hook denies any non-read-only command or read of a sensitive path (SSH/kube-PKI/cloud-credential files, `/etc/shadow`, etc.) issued over SSH — only the log reads above go through. Do not attempt to work around it.
   - Fold node-level findings into the Blockers/Warnings/Info synthesis, then return to step 7.

7. **Ask if user wants to act**
   - If write tools are available in this session, offer to proceed with any applicable remediation.
   - Otherwise, summarise findings and link to relevant Palette docs where applicable.
