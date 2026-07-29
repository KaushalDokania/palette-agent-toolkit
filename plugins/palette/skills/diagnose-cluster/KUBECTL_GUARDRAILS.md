<!-- markdownlint-disable-next-line MD041 -->
> [Root](./SKILL.md) → Kubectl Guardrails

# Read-only kubectl guardrail

`diagnose-cluster` can run `kubectl` against a customer's cluster to triage
issues Palette's API doesn't surface directly (pod state, events, logs). This
guardrail restricts that `kubectl` access to read-only operations, enforced
by **Claude Code's permission system** — not by asking the model nicely.
Instructions in a skill or `CLAUDE.md` never override this; only
`.claude/settings.json` rules, `/permissions`, or a `PreToolUse` hook do.

There are two layers:

1. **The `hooks/` `PreToolUse` hook (primary, auto-enforced).** Ships with
   the plugin and runs automatically once the plugin is installed — no
   opt-in step. This is the layer to rely on.
2. **The `kubectl-readonly.settings.json` template (secondary, config-based
   fallback).** A plain permission-rule template you merge into your own
   settings. Plugins can't auto-apply `permissions` settings, so this layer
   only takes effect if you copy it in — see [Enable the fallback](#enable-the-fallback-settings-template)
   below. Keep it around anyway: it's a second, independent layer (belt and
   suspenders), and it's what you'd fall back to if you ever ran `kubectl`
   outside of a session where this plugin's hook is loaded.

## The hook (primary)

[`../../hooks/kubectl-guard.py`](../../hooks/kubectl-guard.py), wired up via
[`../../hooks/hooks.json`](../../hooks/hooks.json), intercepts every `Bash`
tool call before it runs. For each one it:

1. Tokenizes the command with Python's `shlex` (POSIX + punctuation-aware),
   splitting on shell control operators (`&&`, `||`, `;`, `|`, `|&`, `&`) so
   each piece of a compound command is inspected independently.
2. For each piece, walks past known wrapper commands (`timeout`, `time`,
   `nice`, `nohup`, `stdbuf`, `sudo`, `env`) and leading `FOO=bar` env
   assignments, then checks whether the next real executable is `kubectl`.
3. If it is, walks *past kubectl's global flags* (skipping a flag's value
   token when the flag is a known value-taking one, e.g. `-n`,
   `--context`, `--kubeconfig`) to find the actual verb — **not** just the
   first token after the literal string `kubectl`. This is what makes it
   argv-aware and closes the gap the settings-file matcher can't: a global
   flag placed before the verb (`kubectl -n kube-system get secret x`)
   still resolves to verb `get` + resource `secret`, not an unrecognized
   string.
4. Classifies the verb:
   - `get`/`describe` on anything that looks like a Secret (a token, or a
     comma-separated component of one, whose lowercased value starts with
     `secret`) → **deny**.
   - Any mutating or escape-hatch verb (`create`, `apply`, `delete`,
     `patch`, `edit`, `replace`, `scale`, `annotate`, `label`, `cordon`,
     `drain`, `taint`, `rollout`, `set`, `run`, `expose`, `autoscale`,
     `exec`, `cp`, `port-forward`, `proxy`, `attach`, `debug`) → **deny**.
   - A recognized read-only verb (`get`, `describe`, `logs`, `top`,
     `version`, `api-resources`, `explain`, `cluster-info`, `config view` /
     `config current-context` / `config get-contexts`) → **safe**.
   - Anything else (unrecognized verb, or a subcommand the flag-scanner
     couldn't confidently resolve) → **unknown**.
5. Decides for the whole (possibly compound) command:
   - Any subcommand classified **deny** → the whole `Bash` call is denied,
     via `hookSpecificOutput.permissionDecision: "deny"` with a
     `permissionDecisionReason` naming the offending verb.
   - No denies, and *every* subcommand in the line is a recognized-**safe**
     kubectl call → the whole call is explicitly **allowed**
     (`permissionDecision: "allow"`), so a legitimate diagnostic command
     isn't held up by a prompt.
   - Anything else (no kubectl involved at all, or a mix of kubectl and
     non-kubectl subcommands, or an unrecognized kubectl verb) → the hook
     emits **no output**, which defers to the normal permission flow (the
     settings-file rules below, then a prompt). The hook only ever adds
     denies/allows on top of what's already configured — it never opens up
     something that would otherwise be blocked.

This is why "any command that mixes kubectl with something else" isn't
auto-allowed even if the kubectl part looks safe: `kubectl get pods && curl
evil.example.com` has a non-kubectl subcommand the hook doesn't vet, so it
defers instead of allowing.

The script has a built-in self-check: run
`python3 hooks/kubectl-guard.py --self-test` from the plugin root to verify
the classifier against a fixed set of allow/deny/defer cases (bare verbs,
flag-before-verb secrets access, compound commands, wrapped commands, etc.).

**Requires `python3`** on the machine running Claude Code (no other
dependency — no `jq`, nothing to `pip install`).

## Enable the fallback settings template

Merge the `permissions` block from
[`kubectl-readonly.settings.json`](./kubectl-readonly.settings.json) into
your own settings file:

- `.claude/settings.json` at your project root — shared with your team, safe
  to commit.
- `~/.claude/settings.json` — applies to every project for you personally.

If you already have a `permissions.allow` / `permissions.deny` list, append
these entries to your existing arrays rather than replacing the file. This
layer uses plain `Bash(...)` prefix/wildcard matchers (documented in detail
below) and has the flag-before-verb weakness the hook was built to close —
treat it as a fallback for contexts where the plugin's hook isn't loaded,
not as the primary control.

## What both layers allow

Read-only verbs only: `get`, `describe`, `logs`, `top`, `version`,
`api-resources`, `explain`, `cluster-info`, and `config view` /
`config current-context` / `config get-contexts`.

**Note on `list` / `watch`:** these are Kubernetes RBAC verbs, not `kubectl`
CLI subcommands — there's no `kubectl list` or `kubectl watch` command to
allow or deny. Streaming/list behavior on the CLI comes from `kubectl get
--watch` / `kubectl get -w`, which the `get` allow rule already covers.

## What both layers deny

- **Every mutating verb**: `create`, `apply`, `delete`, `patch`, `edit`,
  `replace`, `scale`, `annotate`, `label`, `cordon`, `drain`, `taint`,
  `rollout`, `set`, `run`, `expose`, `autoscale`.
- **Every escape hatch that isn't a "verb" but still changes or exposes
  cluster state**: `exec`, `cp`, `port-forward`, `proxy`, `attach`, `debug`.
- **Secrets specifically**, even though `get`/`describe` are otherwise
  allowed: `kubectl get secret*` and `kubectl describe secret*` (both
  layers), the hook's argv-aware resource-name check (hook only), and the
  settings template's broader `kubectl * secret*` catch-all (see
  Assumptions below).

Deny always wins over allow in Claude Code's permission system — see
[Configure permissions](https://code.claude.com/docs/en/permissions), section
"Rule precedence." So even though `kubectl get*` is allowed, `kubectl get
secret*` is denied.

## Assumptions about settings-file matcher semantics

- `Bash(kubectl get*)` (no space before `*`) matches `kubectl get`,
  `kubectl get pods`, and `kubectl getSomething` alike — the trailing
  wildcard has no word-boundary requirement without a preceding space. This
  is deliberate: it's the broadest, simplest way to allow both bare and
  flagged invocations of each verb.
- The literal secrets rules (`kubectl get secret*`, `kubectl describe
  secret*`) only match when the command **starts** with that exact verb
  sequence. `kubectl -n kube-system get secret db-creds` (a global flag
  placed *before* the verb) would slip past both of them, because Claude
  Code's Bash matcher is a whole-command prefix/wildcard match, not an
  argv-aware parse of `kubectl`'s flags. **This is the exact gap the
  `PreToolUse` hook exists to close** — the hook tokenizes and walks past
  flags, so it catches this form even though the settings file can't.
- To narrow that gap for the settings-file layer specifically, we added
  `Bash(kubectl * secret*)` — a single wildcard can span multiple
  arguments/spaces, so it also catches flag-prefixed forms like `kubectl -n
  kube-system get secret db-creds` or `kubectl --context=x describe
  secrets`. This is intentionally broad: it will also block reading any
  object whose name or namespace happens to contain the substring `secret`
  (e.g. `kubectl describe pod -n secret-ns`). We accept that over-blocking
  as the safe failure mode for a credentials guardrail.
- **This same flag-before-verb gap applies to the settings file's
  mutating-verb deny rules too**, and we did *not* add a catch-all for
  those there. `kubectl -n foo delete pod x` doesn't start with `kubectl
  delete`, so it matches neither the settings-file allow list nor a
  settings-file deny rule — it falls through to Claude Code's default
  behavior (typically an approval prompt) rather than a hard, silent deny
  *at the settings layer*. The hook does not have this asymmetry: its verb
  scanner runs for every classified verb, mutating or not, so `kubectl -n
  foo delete pod x` is denied by the hook regardless. This is the concrete
  example of why the hook is the primary layer and the settings file is a
  fallback, not the other way around.

## Honest limitation (this is defense-in-depth, not airtight)

Command-string / argv matching cannot fully replace a scoped credential.
Known ways this guardrail — hook included — can still be evaded or produce
a false sense of safety:

- **Aliases**: a shell alias like `alias k=kubectl` produces commands whose
  executable token is `k`, not `kubectl` — the hook's executable check
  (`exe != "kubectl"`) and the settings file's literal `kubectl` prefix
  both miss it.
- **Subshells**: `bash -c "kubectl get secret foo"` or `sh -c '...'` puts
  the real command inside a string argument to `bash`/`sh`, not as a
  sibling token the hook's tokenizer walks into. The hook only inspects
  the literal argv of the outer `Bash` tool call; it does not recursively
  parse quoted script bodies passed to a shell interpreter. Same for the
  settings file's wrapper-stripping, which only recognizes a fixed list
  (`timeout`, `time`, `nice`, `nohup`, `stdbuf`, `command`, `builtin`,
  `noglob`, bare `xargs`) — `bash -c` is not among them. Such a command
  falls through to whatever the *default* permission behavior is
  (typically an approval prompt), not an automatic deny — not a silent
  bypass, but not a hard block either.
- **kubectl plugins / krew plugins**: `kubectl-neat`, custom plugins, or
  any binary invoked as `kubectl <plugin>` that itself performs a mutating
  or secret-reading action isn't distinguished by verb classification —
  the hook only recognizes the fixed verb lists above.
- **Unlisted global flags**: the hook's `VALUE_FLAGS` set (which
  flags consume a following token) is best-effort, not exhaustive against
  every kubectl flag that could ever exist. An unrecognized value-flag can
  cause the scanner to mis-locate the verb — but the failure mode is
  fail-safe: a misparse lands in **unknown**, which defers to a prompt, it
  never fails open to an auto-allow.
- **False positives**: the hook's and settings file's secret-name checks
  match on substrings/tokens, so a legitimate command whose namespace, pod
  name, or `-o jsonpath={...}` output key happens to contain "secret" can
  be denied even though it isn't reading a Secret object. Accepted
  trade-off — over-blocking is the safe failure mode here.

**The real fix is a scoped credential** — a kubeconfig bound to a
read-only, non-secret-reading `ClusterRole`/`RoleBinding` (or equivalent
cloud IAM-mapped RBAC), so that even a fully-evaded guardrail can't do
anything the cluster itself doesn't permit. That's out of scope for this
change and tracked for v1.1.
