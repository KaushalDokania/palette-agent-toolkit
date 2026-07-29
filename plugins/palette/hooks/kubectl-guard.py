#!/usr/bin/env python3
"""PreToolUse hook: enforce read-only kubectl AND ssh for the
diagnose-cluster skill.

Primary, auto-enforced guardrail (see ../skills/diagnose-cluster/
KUBECTL_GUARDRAILS.md for the full policy, rationale, and honest limits).
Unlike the settings.json permission template (a whole-command prefix/
wildcard match), this hook tokenizes the actual command line, so a global
flag placed before the verb -- `kubectl -n kube-system get secret x` --
doesn't slip past it the way it would a pure string-prefix matcher.

ssh is a bigger risk surface than kubectl (arbitrary remote shell), so the
same read-only philosophy is extended to it: the hook locates the REMOTE
COMMAND in an `ssh [opts] [user@]host <remote-command>` invocation (skipping
ssh's own options/values and the destination), classifies that remote
command -- including compound `a && b` / `a | b` / `a; b` command lines --
against a read/write prefix catalog (PAI-412), and denies anything that
isn't confidently read-only.

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

# Same idea for ssh: options that consume a following value token, so the
# scanner doesn't mistake an option's value for the destination host.
# Best-effort, not exhaustive -- same fail-safe rule as VALUE_FLAGS above.
SSH_VALUE_FLAGS = {
    "-i", "-J", "-o", "-p", "-l", "-F", "-c", "-m", "-w", "-B", "-b",
    "-D", "-E", "-e", "-I", "-L", "-O", "-Q", "-R", "-S", "-W",
}

# Port/tunnel forwarding turns ssh into an open network pipe regardless of
# the remote command -- not read-only under any classification, same spirit
# as kubectl's port-forward/proxy verbs above.
SSH_TUNNEL_FLAGS = {"-L", "-R", "-D", "-w"}

# Wrapper commands that just run their argument as the real command, same
# spirit as Claude Code's own Bash-matcher wrapper stripping.
WRAPPERS = {"timeout", "time", "nice", "nohup", "stdbuf", "sudo", "env"}

SEPARATORS = {"&&", "||", ";", "|", "|&", "&"}

# Read-only / mutating remote-command catalogs for the ssh guard, copied
# verbatim (PAI-412) from the edge-ssh test catalog:
# .../oss-release-hardening/_3_work/task4-mcp-live-testing/_2_work/testing/
# edge-ssh/{read,write}-prefixes.txt
# Plus one addition noted below for the diagnose-cluster node-log step.
SAFE_REMOTE_CATALOG = """
kubectl get
kubectl describe
kubectl logs
kubectl top
kubectl explain
kubectl api-resources
kubectl api-versions
kubectl version
kubectl cluster-info
kubectl auth can-i
grep
cat
ls
ls -
find
stat
df
du
ps
ps -
top
htop
netstat
ss
ss -
ip addr
ip route
ip link
ping
ping -
curl
curl -
wget
systemctl status
systemctl is-active
systemctl is-enabled
systemctl is-failed
systemctl list-units
systemctl list-unit-files
systemctl show
journalctl
lsblk
lscpu
lsof
lspci
free
free -
uname
hostname
id
whoami
env
env
echo
date
uptime
which
type
file
head
tail
wc
sort
uniq
diff
awk
sed
cut
tr
jq
yq
helm list
helm get
helm status
helm history
helm version
docker ps
docker inspect
docker logs
docker images
crictl ps
crictl inspect
crictl logs
crictl images
containerd
nerdctl ps
nerdctl inspect
nerdctl logs
virsh list
virsh info
lvs
pvs
vgs
blkid
mount
mount -
df -
dmesg
last
lastlog
w
who
nslookup
dig
host
traceroute
mtr
iptables -L
iptables -n
nft list
openssl verify
openssl x509
openssl s_client
certutil
rpm -q
rpm -l
dpkg -l
dpkg -L
apt list
apt show
snap list
snap info
test
test -
bridge fdb
timedatectl status
nc -z
# --- PAI-412 addition: diagnose-cluster node-log step needs cloud-init
# status reporting; not in the original catalog above. journalctl -u is
# already covered by the bare "journalctl" entry above.
cloud-init status
"""

WRITE_REMOTE_CATALOG = """
kubectl delete
kubectl apply
kubectl patch
kubectl create
kubectl edit
kubectl label
kubectl annotate
kubectl scale
kubectl rollout
kubectl drain
kubectl cordon
kubectl uncordon
kubectl taint
kubectl exec
kubectl cp
kubectl run
kubectl replace
kubectl expose
kubectl set
kubectl port-forward
kubectl proxy
kubectl certificate
helm install
helm upgrade
helm uninstall
helm delete
helm rollback
helm repo update
rm
rm -
rmdir
mv
mkdir
touch
chmod
chown
chgrp
ln
ln -
cp /etc
cp /var
cp /usr
cp /run
cp /opt
cp /srv
truncate
dd
dd -
mkfs
fdisk
parted
mount -o
umount
systemctl start
systemctl stop
systemctl restart
systemctl reload
systemctl enable
systemctl disable
systemctl mask
systemctl unmask
systemctl reset-failed
service start
service stop
service restart
service reload
apt install
apt remove
apt purge
apt upgrade
apt-get install
apt-get remove
apt-get purge
apt-get upgrade
yum install
yum remove
yum update
dnf install
dnf remove
dnf update
snap install
snap remove
pip install
pip uninstall
npm install
npm uninstall
go install
docker run
docker start
docker stop
docker rm
docker rmi
docker pull
docker build
docker exec
crictl pull
crictl rm
nerdctl run
nerdctl rm
iptables -A
iptables -D
iptables -I
iptables -F
nft add
nft delete
useradd
userdel
usermod
groupadd
groupdel
passwd
visudo
crontab
sysctl -w
sysctl -p
timedatectl set
hostnamectl set
localectl set
reboot
shutdown
poweroff
init
kill
killall
pkill
swapoff
swapon
lvextend
lvreduce
lvcreate
lvremove
vgextend
pvcreate
pvremove
"""


def _parse_catalog(text):
    """Parse a catalog block into a list of word-tuples. A trailing lone
    "-" token (e.g. "ls -") is dropped -- it's redundant since matching on
    the bare command already covers any flags after it (see _matches)."""
    entries = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        words = line.split()
        if words[-1] == "-":
            words = words[:-1]
        if words:
            entries.append(tuple(words))
    return entries


SAFE_REMOTE_ENTRIES = _parse_catalog(SAFE_REMOTE_CATALOG)
WRITE_REMOTE_ENTRIES = _parse_catalog(WRITE_REMOTE_CATALOG)


def _matches(entry, tokens):
    """Does this command's tokens match a catalog entry? Entry words are
    matched exactly, except a word that looks like an absolute path (e.g.
    the "/etc" in "cp /etc") is matched as a prefix, since the real
    argument is a full file path (e.g. "/etc/kubernetes/admin.conf")."""
    if len(tokens) < len(entry):
        return False
    for entry_word, tok in zip(entry, tokens):
        if entry_word.startswith("/"):
            if not tok.startswith(entry_word):
                return False
        elif tok != entry_word:
            return False
    return True


def classify_remote_part(tokens):
    """Classify one (already compound-split) piece of an ssh remote
    command: 'write', 'safe', or 'unknown'. Write is checked first so an
    entry that (incorrectly) matched both catalogs still fails safe."""
    if not tokens:
        return "unknown"
    if tokens == ["cloud-init"]:
        return "safe"  # bare invocation just prints usage, no side effect
    if any(_matches(e, tokens) for e in WRITE_REMOTE_ENTRIES):
        return "write"
    if any(_matches(e, tokens) for e in SAFE_REMOTE_ENTRIES):
        return "safe"
    return "unknown"


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


def _skip_env_and_wrappers(tokens):
    """Advance past leading FOO=bar env assignments and wrapper commands
    (timeout/sudo/env/...), returning the index of the real executable."""
    i, n = 0, len(tokens)
    while i < n and "=" in tokens[i] and not tokens[i].startswith("-"):
        i += 1
    while i < n and tokens[i] in WRAPPERS:
        wrapper = tokens[i]
        i += 1
        while i < n and tokens[i].startswith("-"):
            i += 1  # skip the wrapper's own flags, best-effort
        if wrapper == "timeout" and i < n and not tokens[i].startswith("-"):
            i += 1  # skip `timeout`'s bare DURATION positional argument
    return i


def find_kubectl_verb(tokens):
    """Return (is_kubectl, verb, rest_tokens) for one subcommand's tokens."""
    i, n = _skip_env_and_wrappers(tokens), len(tokens)
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


def find_ssh_remote(tokens):
    """Return (is_ssh, destination, remote_tokens, tunnel) for one
    subcommand's tokens. remote_tokens is None when there's no remote
    command at all (interactive shell). tunnel is True if a port/tunnel
    forwarding flag (-L/-R/-D/-w) was seen."""
    i, n = _skip_env_and_wrappers(tokens), len(tokens)
    if i >= n:
        return False, None, None, False
    exe = tokens[i].rsplit("/", 1)[-1]
    if exe != "ssh":
        return False, None, None, False
    i += 1
    tunnel = False
    while i < n and tokens[i].startswith("-") and tokens[i] != "-":
        flag = tokens[i]
        i += 1
        if flag in SSH_TUNNEL_FLAGS:
            tunnel = True
        if flag in SSH_VALUE_FLAGS and i < n:
            i += 1
    if i >= n:
        return True, None, None, tunnel
    destination = tokens[i]
    i += 1
    remote_tokens = tokens[i:] if i < n else []
    return True, destination, remote_tokens, tunnel


def mentions_secret(rest):
    for tok in rest:
        for part in tok.split(","):
            if part.strip().lower().startswith("secret"):
                return True
    return False


def classify_kubectl(tokens):
    """Classify one subcommand as kubectl: ('deny'|'safe'|'unknown-kubectl'
    |None, reason). None means this subcommand isn't a kubectl call."""
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


def classify_ssh(tokens):
    """Classify one subcommand as ssh: ('deny'|'safe'|'unknown-ssh'|None,
    reason). None means this subcommand isn't an ssh call at all."""
    is_ssh, dest, remote_tokens, tunnel = find_ssh_remote(tokens)
    if not is_ssh:
        return None, None
    if tunnel:
        return "deny", "ssh port/tunnel forwarding (-L/-R/-D/-w) is not read-only"
    if dest is None:
        return "deny", "malformed ssh invocation (no destination found)"
    if not remote_tokens:
        return "deny", "ssh with no remote command opens an interactive shell"
    remote_subs = split_subcommands(" ".join(remote_tokens))
    if not remote_subs:
        return "deny", "ssh remote command could not be parsed"
    kinds = [classify_remote_part(s) for s in remote_subs]
    if any(k == "write" for k in kinds):
        return "deny", "ssh remote command includes a mutating/write action"
    if all(k == "safe" for k in kinds):
        return "safe", None
    return "unknown-ssh", None


def classify(tokens):
    """Classify one subcommand, trying kubectl then ssh. Returns
    (kind, reason); kind is None if this subcommand is neither."""
    kind, reason = classify_kubectl(tokens)
    if kind is not None:
        return kind, reason
    return classify_ssh(tokens)


def decide(command: str):
    """Return (decision, reason). decision is 'deny', 'allow', or None
    (defer -- no kubectl/ssh involved, or a compound command we can't fully
    vet, e.g. mixed with a non-kubectl/non-ssh subcommand)."""
    subs = split_subcommands(command)
    results = [classify(s) for s in subs]
    kinds = [k for k, _ in results]
    if not any(k is not None for k in kinds):
        return None, None
    for kind, reason in results:
        if kind == "deny":
            return "deny", reason
    if kinds and all(k == "safe" for k in kinds):
        return "allow", "read-only command (diagnose-cluster guardrail)"
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
        # ssh cases (PAI-412)
        ('ssh -i k user@h "cat /var/log/cloud-init-output.log"', "allow"),
        ('ssh h "rm -rf /"', "deny"),
        ('ssh -J bastion user@node "journalctl -u kubelet"', "allow"),
        ("ssh host", "deny"),
        ('ssh h "cat x && systemctl restart y"', "deny"),
        ("ssh user@host cat /var/log/syslog", "allow"),
        ("ssh -p 2222 user@host cloud-init status", "allow"),
        ("ssh user@host cloud-init", "allow"),
        ("ssh user@host cloud-init clean", None),
        ("ssh -L 8080:localhost:80 user@host cat /etc/hosts", "deny"),
        ("ssh user@host", "deny"),
        ("ssh user@host 'cat /etc/shadow'", "allow"),
        ('ssh h "cat /var/log/x; mkdir /tmp/y"', "deny"),
        ("ssh h 'cat a | grep b'", "allow"),
        ("ssh -o StrictHostKeyChecking=no user@host uname -a", "allow"),
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
