<!-- markdownlint-disable-next-line MD041 -->
> [Root](./SKILL.md) → Kubectl Guardrails

# Read-only kubectl guardrail (standalone-skill fallback)

`diagnose-cluster` can run `kubectl` against a customer's cluster to triage
issues Palette's API doesn't surface directly. If you installed this skill
standalone (`npx skills add .../skills`), you only got this directory — not
the auto-enforcing `PreToolUse` hook, which is a **plugin**-only component
(it lives under `plugins/palette/hooks/` and is auto-discovered when the
plugin is installed, but there's no equivalent auto-discovery mechanism for
a bare skill install).

For a standalone install, the only enforcement available is the
config-based fallback: merge the `permissions` block from
[`kubectl-readonly.settings.json`](./kubectl-readonly.settings.json) into
your own `.claude/settings.json` (project, shared) or `~/.claude/settings.json`
(personal). It restricts `kubectl` to read-only verbs (`get`, `describe`,
`logs`, `top`, `version`, `api-resources`, `explain`, `cluster-info`,
`config view`/`current-context`/`get-contexts`), denies every mutating verb
and escape hatch (`create`, `apply`, `delete`, `patch`, `edit`, `replace`,
`scale`, `annotate`, `label`, `cordon`, `drain`, `taint`, `rollout`, `set`,
`run`, `expose`, `autoscale`, `exec`, `cp`, `port-forward`, `proxy`,
`attach`, `debug`), and specifically denies reading Secrets. Deny always
wins over allow.

**This fallback has a known gap the hook exists to close**: it's a
whole-command prefix/wildcard match, not an argv-aware parser, so a global
flag placed *before* the verb (`kubectl -n kube-system get secret x`) can
slip past the literal secrets rules — we added a broad `kubectl * secret*`
catch-all to narrow that specific gap, at the cost of also blocking reads
of anything with "secret" in its name/namespace (accepted as the safe
failure mode).

**For the stronger, auto-enforced guardrail, install the full `palette`
plugin** (`/plugin install palette@palette-agent-toolkit`) instead of the
standalone skill — see
[`plugins/palette/skills/diagnose-cluster/KUBECTL_GUARDRAILS.md`](https://github.com/spectrocloud/palette-agent-toolkit/blob/main/plugins/palette/skills/diagnose-cluster/KUBECTL_GUARDRAILS.md)
for the full policy, including the `PreToolUse` hook that tokenizes each
command so a flag before the verb no longer bypasses the secrets check.

This is defense-in-depth, not airtight either way — see the linked doc's
"Honest limitation" section for what a determined bypass (aliases,
subshells, kubectl plugins) still gets past both layers. The real fix is a
scoped, non-secret-reading kubeconfig credential, tracked for v1.1.
