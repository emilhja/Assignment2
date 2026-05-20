import re


# Read-only commands the agent is allowed to run. Anything not on this list is
# blocked by the allowlist layer before the blocklist below even runs.
ALLOWED_COMMANDS = frozenset(
    {
        "ls",
        "cat",
        "grep",
        "head",
        "tail",
        "wc",
        "find",
        "pwd",
        "echo",
        "printf",
        "sort",
        "uniq",
        "cut",
        "awk",
        "sed",
        "true",
        "false",
    }
)


# Check the user's plain-English request before any shell command exists.
# Example: "delete everything" is blocked before the model can turn it into rm -rf.
FORBIDDEN_INTENT_PATTERNS = [
    (
        re.compile(r"(?i)\b(delete|remove)\s+everything\b"),
        "I cannot help delete everything.",
    ),
    (
        re.compile(r"(?i)\b(delete|remove)\s+all\b"),
        "I cannot help with broad deletion.",
    ),
    (
        re.compile(r"(?i)\bwipe\b"),
        "I will not wipe files from here",
    ),
    (
        re.compile(r"(?i)\bclear\s+the\s+directory\b"),
        "I cannot clear a whole directory.",
    ),
    (
        re.compile(r"(?i)\brm\s+-[^\n]*r[^\n]*f\b|\brm\s+-[^\n]*f[^\n]*r\b"),
        "rm -rf is too destructive for this agent.",
    ),
    (
        re.compile(r"(?i)(^\s*|\b(run|execute|use)\b[^\n]*)docker\s+compose\b"),
        "Run Docker on the host machine instead",
    ),
    (
        re.compile(r"(?i)(^\s*|\b(run|execute|use)\b[^\n]*)docker-compose\b"),
        "Run Docker on the host machine instead",
    ),
    (
        re.compile(r"(?i)\binstall\s+docker\b"),
        "I cannot install Docker from inside the agent",
    ),
]

# Check the actual shell command after the model writes it.
# Example: "rm -rf ." is blocked even if the original request sounded harmless.
DANGEROUS_PATTERNS = [
    (re.compile(r"(?i)\b(Action|Command|Final Answer|Observation)\s*:"), "Do not put ReAct labels inside shell commands"),
    (re.compile(r"(?i)(^|[/\s\"'])\.env([.\s\"']|$)"), "I will not reference environment files."),
    (re.compile(r"(?i)(^|[\s\"'=])/data(/|[\s\"']|$)"), "I will not reference internal data files."),
    (re.compile(r"(?i)/proc/[^ \t\n;|&]+/environ\b"), "I will not reference process environments."),
    (re.compile(r"(?i)\b(os\.environ|process\.env)\b"), "I will not expose process environments."),
    (re.compile(r"(?i)(^|[\s;|&])env(\s|$)"), "I will not expose environment variables."),
    (re.compile(r"(?i)(^|[\s;|&])printenv(\s|$)"), "I will not expose environment variables."),
    (re.compile(r"(?i)(^|[\s;|&])export\s*($|[;|&])"), "I will not expose shell environment variables."),
    (re.compile(r"(?i)(^|[\s;|&])set\s*($|[;|&])"), "I will not expose shell variables."),
    (re.compile(r"(?i)(^|[\s;|&])declare\s+-[^\n;|&]*p\b"), "I will not expose shell variables."),
    (re.compile(r"(?i)\$\{?[A-Z0-9_]*(KEY|TOKEN|SECRET|PASSWORD|PASS|AUTH)[A-Z0-9_]*\}?"), "I will not expose sensitive environment variables."),
    (re.compile(r"(?i)(^|[\s;|&])cat\b[^\n;|&]*(^|[/\s\"'])\.env([.\s\"']|$)"), "I will not read environment files."),
    (re.compile(r"(?i)(^|[\s;|&])cat\b[^\n;|&]/data(/|[\s\"']|$)"), "I will not read internal data files."),
    (re.compile(r"(?i)(^|[\s;|&])cat\b[^\n;|&]/proc/[^ \t\n;|&]+/environ\b"), "I will not read process environments."),
    (re.compile(r"(?i)(^|[\s;|&])(ls|grep|sed|awk|head|tail|less|more|wc|stat|file)\b[^\n;|&]*(^|[/\s\"'])\.env([.\s\"']|$)"), "I will not inspect environment files."),
    (re.compile(r"(?i)(^|[\s;|&])(ls|grep|sed|awk|head|tail|less|more|wc|stat|file)\b[^\n;|&]/data(/|[\s\"']|$)"), "I will not inspect internal data files."),
    (re.compile(r"(?i)(^|[\s;|&])(grep|sed|awk|head|tail|less|more|wc|stat|file)\b[^\n;|&]/proc/[^ \t\n;|&]+/environ\b"), "I will not inspect process environments."),
    (re.compile(r"(?i)(^|[\s;|&])rm(\s|$)"), "I will not run rm from this agent."),
    (re.compile(r"(?i)(^|[\s;|&])rmdir(\s|$)"), "I will not run rmdir from this agent."),
    (re.compile(r"(?i)(^|[\s;|&])sudo(\s|$)"), "I cannot use sudo from here."),
    (re.compile(r"(?i)(^|[\s;|&])docker-compose(\s|$)"), "Run docker-compose on the host machine instead"),
    (re.compile(r"(?i)(^|[\s;|&])docker(\s|$)"), "Run Docker on the host machine instead"),
    (re.compile(r"(?i)(^|[\s;|&])podman(\s|$)"), "podman needs to stay outside this agent."),
    (re.compile(r"(?i)(^|[\s;|&])yum(\s|$)"), "I cannot run package managers from here."),
    (re.compile(r"(?i)(^|[\s;|&])dnf(\s|$)"), "I cannot run package managers from here."),
    (re.compile(r"(?i)(^|[\s;|&])apt(\s|$)"), "I cannot run package managers from here."),
    (re.compile(r"(?i)(^|[\s;|&])apt-get(\s|$)"), "I cannot run package managers from here."),
    (re.compile(r"(?i)(^|[\s;|&])apk(\s|$)"), "I cannot run package managers from here."),
    (re.compile(r"(?i)(^|[\s;|&])systemctl(\s|$)"), "service commands belong on the host."),
    (re.compile(r"(?i)(^|[\s;|&])service(\s|$)"), "service commands belong on the host."),
    (re.compile(r"(?i)(^|[\s;|&])mkfs(\s|\.|$)"), "mkfs is too dangerous to run here."),
    (re.compile(r"(?i)(^|[\s;|&])shutdown(\s|$)"), "I cannot shut the machine down."),
    (re.compile(r"(?i)(^|[\s;|&])reboot(\s|$)"), "I cannot reboot the machine."),
    (re.compile(r"(?i)(^|[\s;|&])poweroff(\s|$)"), "I cannot power the machine off."),
    (re.compile(r"(?i)(^|[\s;|&])chmod\s+-[^\n;|&]*R\b"), "recursive chmod is too broad."),
    (re.compile(r"(?i)(^|[\s;|&])chown\s+-[^\n;|&]*R\b"), "recursive chown is too broad."),
    (re.compile(r"(?i)(^|[\s;|&])find\b[^\n;|&]*\s-delete\b"), "find -delete can remove too much."),
    (re.compile(r"(?i)\|\s*xargs\b[^\n;|&]*\brm\b"), "piping paths into rm is not safe here."),
    (re.compile(r"(?i)(^|[\s;|&])bash\s+-c(\s|$)"), "I will not run bash -c wrappers"),
    (re.compile(r"(?i)(^|[\s;|&])sh\s+-c(\s|$)"), "I will not run sh -c wrappers"),
    (re.compile(r"(?i)(^|[\s;|&])cat\s+\*\s*($|[;|&])"), "cat * is too broad"),
    (re.compile(r"(?i)(^|[\s;|&])cat\s+\*\*/\*\s*($|[;|&])"), "cat **/* is too broad"),
    (re.compile(r"(?i)(^|[\s;|&])find\s+/\s*($|[;|&])"), "find / searches too much"),
    (re.compile(r"(?i)(^|[\s;|&])grep\s+-[^\n;|&]*R[^\n;|&]*/\s*($|[;|&])"), "grep -R / searches too much"),
    (re.compile(r"(?is)\bcurl\b.+\|\s*bash\b"), "I will not pipe curl into bash."),
    (re.compile(r"(?is)\bwget\b.+\|\s*bash\b"), "I will not pipe wget into bash."),
    (re.compile(r":\(\)\s*\{\s*:\|:&\s*\};:"), "That looks like a fork bomb"),
    (re.compile(r"\$\("), "I will not run command substitution."),
    (re.compile(r"`"), "I will not run backtick command substitution."),
    (re.compile(r"<\("), "I will not run input process substitution."),
    (re.compile(r">\("), "I will not run output process substitution."),
    (re.compile(r"(^|\s)\d?>>?(\s|$)"), "I will not run shell redirection."),
    (re.compile(r"(?i)(^|[\s;|&])sed\s+-[a-z]*i\b"), "I will not run sed in-place edits."),
]


# Segments separated by ; | & (the agent only inspects the first token of each).
_SEGMENT_SPLIT = re.compile(r"[;|&]+")
_FIRST_TOKEN = re.compile(r"\S+")


def command_allowlist_check(command):
    """Reject commands whose first token in any segment is not on the allowlist."""

    for segment in _SEGMENT_SPLIT.split(command):
        stripped = segment.strip()
        if not stripped:
            continue
        match = _FIRST_TOKEN.search(stripped)
        if not match:
            continue
        first = match.group(0)
        if first not in ALLOWED_COMMANDS:
            return False, f"command '{first}' is not on the allowlist"
    return True, None


def intent_refusal(user_task):
    """Return a refusal reason if the user's request is not allowed."""

    # Refuse tasks that would delete broadly or need host-only tools like Docker.
    for pattern, reason in FORBIDDEN_INTENT_PATTERNS:
        if pattern.search(user_task):
            return reason
    return None


def safety_check(command):
    """Return whether the command passes the allowlist and blocklist layers."""

    # Layer 1: default-deny. Only commands on ALLOWED_COMMANDS may run.
    allowed, reason = command_allowlist_check(command)
    if not allowed:
        return False, f"Blocked by safety check: {reason}"

    # Layer 2: scan the full command text for blocked tools, tokens, and patterns.
    for pattern, reason in DANGEROUS_PATTERNS:
        if pattern.search(command):
            return False, f"Blocked by safety check: {reason}"
    return True, None


def is_command_safe(command):
    """Return True when safety_check allows the command."""

    # Some callers only need True or False, not the refusal reason.
    allowed, _reason = safety_check(command)
    return allowed


def confirm_command(command):
    """Show the command and ask the user to approve it."""

    # Pressing Enter should mean no; only y or yes approves the command.
    print("\nProposed command:")
    print(command)
    answer = input("Run this command? [y/N] ").strip().lower()
    return answer in {"y", "yes"}
