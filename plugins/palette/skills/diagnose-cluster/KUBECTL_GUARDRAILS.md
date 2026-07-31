<!-- markdownlint-disable-next-line MD041 -->
> [Root](./SKILL.md) → Kubectl Guardrails

# Read-only kubectl guardrail

`diagnose-cluster` can run `kubectl` against a customer's cluster to triage
issues Palette's API doesn't surface directly (pod state, events, logs).

## There is no enforcement hook

An earlier version of this skill shipped an automatic pre-execution hook
that tokenized every `Bash` command and auto-blocked mutating or
secret-reading `kubectl`/`ssh` calls. It was removed: it failed open under ordinary
conditions (missing `python3`, a parse error, a newline-separated compound
command), it was bypassable (`kubectl get --raw`, `kubectl config view
--raw`, `ssh -o ProxyCommand=...`), and — because Claude Code hooks match on
tool name, not on which skill is active — it fired on *every* `Bash` call in
*every* project you had open, not just this one. There is nothing in this
repo that automatically inspects or blocks `kubectl` commands before they
run.

## The intended safety boundary: a read-only kubeconfig (not yet wired up)

The plan is for kube-tier triage to fetch a **read-only kubeconfig** for the
target cluster instead of the admin one, so that even a command that slips
past every guardrail below still can't mutate the cluster or read
Secrets — the credential itself wouldn't permit it. That work is in
progress; see [`SKILL.md`](./SKILL.md)'s K1 step, which is being rewritten
separately to fetch and use it.

**Until that lands, be aware kube-tier triage currently uses the admin
kubeconfig, with no automatic enforcement at that layer at all** — the
only thing standing between a stray command and the cluster right now is
whatever's below in this file, which (see "Honest limitation") is not a
real substitute for a scoped credential.

## Optional layer: `kubectl-readonly.settings.json`

[`kubectl-readonly.settings.json`](./kubectl-readonly.settings.json) is a
plain permission-rule template, restricting `kubectl` to read-only verbs via
**Claude Code's own permission system** — not the skill, not a hook, and not
by trusting the model.

It is **opt-in and does nothing on its own.** Plugins can't auto-apply
`permissions` settings (only hooks can auto-install, which is exactly the
risk that got the hook removed), so this template only takes effect if you
merge its `permissions` block into your own settings:

- `.claude/settings.json` at your project root — shared with your team, safe
  to commit.
- `~/.claude/settings.json` — applies to every project for you personally.

If you already have a `permissions.allow` / `permissions.deny` list, append
these entries to your existing arrays rather than replacing the file. If you
don't merge it in, there is no enforcement at this layer at all — commands
just go through Claude Code's normal permission flow (typically a prompt).

### What it allows

Read-only verbs only: `get`, `describe`, `logs`, `top`, `version`,
`api-resources`, `explain`, `cluster-info`, and `config view` /
`config current-context` / `config get-contexts`.

### What it denies

- **Every mutating verb**: `create`, `apply`, `delete`, `patch`, `edit`,
  `replace`, `scale`, `annotate`, `label`, `cordon`, `drain`, `taint`,
  `rollout`, `set`, `run`, `expose`, `autoscale`.
- **Every escape hatch that isn't a "verb" but still changes or exposes
  cluster state**: `exec`, `cp`, `port-forward`, `proxy`, `attach`, `debug`.
- **Secrets specifically**: `kubectl get secret*`, `kubectl describe
  secret*`, and a broader `kubectl * secret*` catch-all.

Deny always wins over allow in Claude Code's permission system — see
[Configure permissions](https://code.claude.com/docs/en/permissions), section
"Rule precedence."

### Known gap: flag-before-verb

This template matches on the whole command string
(`Bash(kubectl get*)`-style prefix/wildcard matching), not an argv-aware
parse of `kubectl`'s flags. A global flag placed *before* the verb —
`kubectl -n kube-system get secret db-creds` — doesn't start with `kubectl
get secret`, so the literal secrets rule misses it. The broader `kubectl *
secret*` catch-all narrows this for secrets specifically (at the cost of
also blocking reads of anything with "secret" in its name or namespace —
accepted as the safe failure mode), but the same gap applies to the
mutating-verb deny rules with no catch-all: `kubectl -n foo delete pod x`
matches neither the allow list nor a deny rule and falls through to
Claude Code's default behavior (typically a prompt), not a hard deny.

## Honest limitation (this is defense-in-depth, not airtight)

A command-string / settings-based layer is not a substitute for a real RBAC
boundary. Even with the settings template merged in, known ways this
guardrail can still be evaded or produce a false sense of safety:

- **Aliases**: `alias k=kubectl` produces a command whose executable token
  is `k`, not `kubectl` — the literal `kubectl` prefix match misses it.
- **Subshells**: `bash -c "kubectl get secret foo"` puts the real command
  inside a string argument, not a token the matcher inspects directly.
- **kubectl plugins**: `kubectl-neat` or any krew plugin invoked as
  `kubectl <plugin>` isn't distinguished by verb matching at all.
- **False positives**: the secret-name checks match on substrings, so a
  legitimate command whose namespace or `-o jsonpath={...}` output happens
  to contain "secret" can be denied even though it isn't reading a Secret.

None of this is new information relative to when the hook existed — the
hook had its own version of every one of these gaps (see git history on
this file if you want the details). The difference now is that this
guardrail is opt-in rather than auto-applied, and it was never intended to
be the thing actually protecting the cluster. **A read-only kubeconfig is
the real control** — enforced by the cluster's own RBAC, not by string
matching, so it would hold even when every layer in this file is bypassed
or simply never enabled — but that credential isn't in use yet (see
above). Until it lands, treat kube-tier `kubectl` access as running with
admin privileges and no automatic guardrail; the settings template above
is the only thing you can opt into today.
