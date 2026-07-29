#!/usr/bin/env python3
"""PreToolUse hook: enforce read-only kubectl for the diagnose-cluster skill.

Primary, auto-enforced guardrail (see ../skills/diagnose-cluster/
KUBECTL_GUARDRAILS.md for the full policy, rationale, and honest limits).
Unlike the settings.json permission template (a whole-command prefix/
wildcard match), this hook tokenizes the actual command line, so a global
flag placed before the verb -- `kubectl -n kube-system get secret x` --
doesn't slip past it the way it would a pure string-prefix matcher.

Any command this script can't confidently classify is left alone (no
stdout) so it falls through to the normal permission flow (settings.json
rules, then a prompt) -- this hook only ever *adds* denies/allows, it never
weakens what's already configured.
"""
import json
import shlex
import sys

SAFE_VERBS = {
    "get", "describe", "logs", "top", "version",
    "api-resources", "explain", "cluster-info",
}
SAFE_CONFIG_SUBCOMMANDS = {"view", "current-context", "get-contexts"}

DENY_VERBS = {
    "create", "apply", "delete", "patch", "edit", "replace", "scale",
    "annotate", "label", "cordon", "drain", "taint", "rollout", "set",
    "run", "expose", "autoscale",
    "exec", "cp", "port-forward", "proxy", "attach", "debug",
}

# Global kubectl flags known to consume a following value token, so the
# scanner doesn't mistake a flag's value for the verb (e.g. `-n foo get`).
# Best-effort, not exhaustive: an unlisted value-flag just means we fail
# safe (classify as unknown -> defer to the normal ask/permission flow),
# never fail open to an auto-allow.
VALUE_FLAGS = {
    "-n", "--namespace", "--context", "--kubeconfig", "-s", "--server",
    "--token", "--cluster", "--user", "--username", "--password",
    "--as", "--as-group", "--as-uid", "--request-timeout",
    "--certificate-authority", "--client-certificate", "--client-key",
    "--tls-server-name", "--cache-dir", "--profile", "--profile-output",
    "-v", "--v", "--vmodule", "--log-dir", "--log-file",
    "--log-backtrace-at", "--stderrthreshold",
}

# Wrapper commands that just run their argument as the real command, same
# spirit as Claude Code's own Bash-matcher wrapper stripping.
WRAPPERS = {"timeout", "time", "nice", "nohup", "stdbuf", "sudo", "env"}

SEPARATORS = {"&&", "||", ";", "|", "|&", "&"}


def split_subcommands(command: str):
    """Split a shell command line into subcommand token lists on shell
    control operators, respecting quoting."""
    lex = shlex.shlex(command, posix=True, punctuation_chars=True)
    lex.whitespace_split = True
    try:
        tokens = list(lex)
    except ValueError:
        # Unbalanced quotes etc. -- don't guess, let it fall through.
        return []
    subs, current = [], []
    for tok in tokens:
        if tok in SEPARATORS:
            if current:
                subs.append(current)
            current = []
        else:
            current.append(tok)
    if current:
        subs.append(current)
    return subs


def find_kubectl_verb(tokens):
    """Return (is_kubectl, verb, rest_tokens) for one subcommand's tokens."""
    i, n = 0, len(tokens)
    while i < n and "=" in tokens[i] and not tokens[i].startswith("-"):
        i += 1  # skip leading FOO=bar env assignments
    while i < n and tokens[i] in WRAPPERS:
        wrapper = tokens[i]
        i += 1
        while i < n and tokens[i].startswith("-"):
            i += 1  # skip the wrapper's own flags, best-effort
        if wrapper == "timeout" and i < n and not tokens[i].startswith("-"):
            i += 1  # skip `timeout`'s bare DURATION positional argument
    if i >= n:
        return False, None, []
    exe = tokens[i].rsplit("/", 1)[-1]
    if exe != "kubectl":
        return False, None, []
    i += 1
    while i < n and tokens[i].startswith("-"):
        flag = tokens[i]
        i += 1
        if "=" not in flag and flag in VALUE_FLAGS and i < n:
            i += 1
    if i >= n:
        return True, None, []
    return True, tokens[i], tokens[i + 1:]


def mentions_secret(rest):
    for tok in rest:
        for part in tok.split(","):
            if part.strip().lower().startswith("secret"):
                return True
    return False


def classify(tokens):
    """Classify one subcommand: ('deny'|'safe'|'unknown-kubectl'|None, reason).
    None means this subcommand isn't a kubectl call at all."""
    is_kubectl, verb, rest = find_kubectl_verb(tokens)
    if not is_kubectl:
        return None, None
    if verb is None:
        return "deny", "bare `kubectl` invocation with no recognizable verb"
    if verb in ("get", "describe") and mentions_secret(rest):
        return "deny", f"kubectl {verb} on a Secret is never allowed"
    if verb in DENY_VERBS:
        return "deny", f"kubectl {verb} is a mutating/escape-hatch verb"
    if verb == "config":
        if rest and rest[0] in SAFE_CONFIG_SUBCOMMANDS:
            return "safe", None
        return "unknown-kubectl", None
    if verb in SAFE_VERBS:
        return "safe", None
    return "unknown-kubectl", None


def decide(command: str):
    """Return (decision, reason). decision is 'deny', 'allow', or None
    (defer -- no kubectl involved, or a compound command we can't fully
    vet, e.g. mixed with a non-kubectl subcommand)."""
    subs = split_subcommands(command)
    results = [classify(s) for s in subs]
    kinds = [k for k, _ in results]
    if not any(k is not None for k in kinds):
        return None, None
    for kind, reason in results:
        if kind == "deny":
            return "deny", reason
    if kinds and all(k == "safe" for k in kinds):
        return "allow", "read-only kubectl (diagnose-cluster guardrail)"
    return None, None


def main():
    payload = json.load(sys.stdin)
    if payload.get("tool_name") != "Bash":
        return
    command = payload.get("tool_input", {}).get("command", "")
    decision, reason = decide(command)
    if decision is None:
        return  # no output -> defer to the normal permission flow
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": decision,
            "permissionDecisionReason": reason,
        }
    }))


def self_test():
    cases = [
        ("kubectl get pods", "allow"),
        ("kubectl get secret db-creds", "deny"),
        ("kubectl -n kube-system get secret db-creds", "deny"),
        ("kubectl describe secrets", "deny"),
        ("kubectl get secrets,configmaps", "deny"),
        ("kubectl delete pod x", "deny"),
        ("kubectl -n foo delete pod x", "deny"),
        ("kubectl exec -it pod -- sh", "deny"),
        ("kubectl proxy", "deny"),
        ("kubectl get pods && kubectl delete pod x", "deny"),
        ("kubectl get pods && curl https://example.com", None),
        ("kubectl config get-contexts", "allow"),
        ("kubectl config use-context prod", None),
        ("echo hi", None),
        ("kubectl version --client", "allow"),
        ("timeout 30 kubectl get pods", "allow"),
        ("kubectl get configmap myapp -o jsonpath={.data.key}", "allow"),
    ]
    failures = []
    for cmd, expected in cases:
        got, _ = decide(cmd)
        if got != expected:
            failures.append((cmd, expected, got))
    if failures:
        for cmd, expected, got in failures:
            print(f"FAIL: {cmd!r} expected={expected!r} got={got!r}", file=sys.stderr)
        sys.exit(1)
    print(f"self_test: {len(cases)} cases OK")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--self-test":
        self_test()
    else:
        main()
