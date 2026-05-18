import re


# Check the user's plain-English request before any shell command exists.
# Example: "delete everything" is blocked before the model can turn it into rm -rf.
FORBIDDEN_INTENT_PATTERNS = [
    (
        re.compile(r"(?i)\b(delete|remove)\s+everything\b"),
        "destructive bulk deletion is not allowed.",
    ),
    (
        re.compile(r"(?i)\b(delete|remove)\s+all\b"),
        "destructive bulk deletion is not allowed.",
    ),
    (
        re.compile(r"(?i)\bwipe\b"),
        "destructive bulk deletion is not allowed.",
    ),
    (
        re.compile(r"(?i)\bclear\s+the\s+directory\b"),
        "destructive bulk deletion is not allowed.",
    ),
    (
        re.compile(r"(?i)\brm\s+-[^\n]*r[^\n]*f\b|\brm\s+-[^\n]*f[^\n]*r\b"),
        "destructive bulk deletion is not allowed.",
    ),
    (
        re.compile(r"(?i)(^\s*|\b(run|execute|use)\b[^\n]*)docker\s+compose\b"),
        "Docker commands must be run on the host, not inside the agent.",
    ),
    (
        re.compile(r"(?i)(^\s*|\b(run|execute|use)\b[^\n]*)docker-compose\b"),
        "Docker commands must be run on the host, not inside the agent.",
    ),
    (
        re.compile(r"(?i)\binstall\s+docker\b"),
        "Docker installation is not allowed inside the agent container.",
    ),
]

# Check the actual shell command after the model writes it.
# Example: "rm -rf ." is blocked even if the original request sounded harmless.
DANGEROUS_PATTERNS = [
    (re.compile(r"(?i)\b(Action|Command|Final Answer|Observation)\s*:"), "protocol tokens are blocked inside commands."),
    (re.compile(r"(?i)(^|[\s;|&])rm(\s|$)"), "rm commands are blocked."),
    (re.compile(r"(?i)(^|[\s;|&])rmdir(\s|$)"), "rmdir commands are blocked."),
    (re.compile(r"(?i)(^|[\s;|&])sudo(\s|$)"), "sudo commands are blocked."),
    (re.compile(r"(?i)(^|[\s;|&])docker-compose(\s|$)"), "docker-compose commands must be run on the host, not inside the agent."),
    (re.compile(r"(?i)(^|[\s;|&])docker(\s|$)"), "docker commands must be run on the host, not inside the agent."),
    (re.compile(r"(?i)(^|[\s;|&])podman(\s|$)"), "podman commands are blocked."),
    (re.compile(r"(?i)(^|[\s;|&])yum(\s|$)"), "package manager commands are blocked."),
    (re.compile(r"(?i)(^|[\s;|&])dnf(\s|$)"), "package manager commands are blocked."),
    (re.compile(r"(?i)(^|[\s;|&])apt(\s|$)"), "package manager commands are blocked."),
    (re.compile(r"(?i)(^|[\s;|&])apt-get(\s|$)"), "package manager commands are blocked."),
    (re.compile(r"(?i)(^|[\s;|&])apk(\s|$)"), "package manager commands are blocked."),
    (re.compile(r"(?i)(^|[\s;|&])systemctl(\s|$)"), "service manager commands are blocked."),
    (re.compile(r"(?i)(^|[\s;|&])service(\s|$)"), "service manager commands are blocked."),
    (re.compile(r"(?i)(^|[\s;|&])mkfs(\s|\.|$)"), "mkfs commands are blocked."),
    (re.compile(r"(?i)(^|[\s;|&])shutdown(\s|$)"), "shutdown commands are blocked."),
    (re.compile(r"(?i)(^|[\s;|&])reboot(\s|$)"), "reboot commands are blocked."),
    (re.compile(r"(?i)(^|[\s;|&])poweroff(\s|$)"), "poweroff commands are blocked."),
    (re.compile(r"(?i)(^|[\s;|&])chmod\s+-[^\n;|&]*R\b"), "recursive chmod is blocked."),
    (re.compile(r"(?i)(^|[\s;|&])chown\s+-[^\n;|&]*R\b"), "recursive chown is blocked."),
    (re.compile(r"(?i)(^|[\s;|&])find\b[^\n;|&]*\s-delete\b"), "find -delete commands are blocked."),
    (re.compile(r"(?i)\|\s*xargs\b[^\n;|&]*\brm\b"), "xargs rm commands are blocked."),
    (re.compile(r"(?i)(^|[\s;|&])bash\s+-c(\s|$)"), "bash -c wrapper commands are blocked."),
    (re.compile(r"(?i)(^|[\s;|&])sh\s+-c(\s|$)"), "sh -c wrapper commands are blocked."),
    (re.compile(r"(?i)(^|[\s;|&])cat\s+\*\s*($|[;|&])"), "broad cat * reads are blocked."),
    (re.compile(r"(?i)(^|[\s;|&])cat\s+\*\*/\*\s*($|[;|&])"), "broad cat **/* reads are blocked."),
    (re.compile(r"(?i)(^|[\s;|&])find\s+/\s*($|[;|&])"), "broad find / searches are blocked."),
    (re.compile(r"(?i)(^|[\s;|&])grep\s+-[^\n;|&]*R[^\n;|&]*/\s*($|[;|&])"), "broad grep -R / searches are blocked."),
    (re.compile(r"(?is)\bcurl\b.+\|\s*bash\b"), "curl piped to bash is blocked."),
    (re.compile(r"(?is)\bwget\b.+\|\s*bash\b"), "wget piped to bash is blocked."),
    (re.compile(r":\(\)\s*\{\s*:\|:&\s*\};:"), "fork bomb pattern is blocked."),
]


def intent_refusal(user_task):
    """Return a refusal reason if the user's request is not allowed."""

    # Refuse tasks that would delete broadly or need host-only tools like Docker.
    for pattern, reason in FORBIDDEN_INTENT_PATTERNS:
        if pattern.search(user_task):
            return reason
    return None


def safety_check(command):
    """Return whether the command matches any blocked command pattern."""

    # Scan the full command text for blocked tools, tokens, and shell patterns.
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
