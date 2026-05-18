import re


DANGEROUS_PATTERNS = [
    (re.compile(r"(?i)(^|[\s;|&])rm(\s|$)"), "rm commands are blocked."),
    (re.compile(r"(?i)(^|[\s;|&])rmdir(\s|$)"), "rmdir commands are blocked."),
    (re.compile(r"(?i)(^|[\s;|&])sudo(\s|$)"), "sudo commands are blocked."),
    (re.compile(r"(?i)(^|[\s;|&])docker(\s|$)"), "Docker commands must be run on the host."),
    (re.compile(r"(?i)(^|[\s;|&])docker-compose(\s|$)"), "Docker commands must be run on the host."),
    (re.compile(r"(?i)(^|[\s;|&])apt(-get)?(\s|$)"), "package manager commands are blocked."),
    (re.compile(r"(?i)(^|[\s;|&])apk(\s|$)"), "package manager commands are blocked."),
    (re.compile(r"(?i)(^|[\s;|&])dnf(\s|$)"), "package manager commands are blocked."),
    (re.compile(r"(?i)(^|[\s;|&])yum(\s|$)"), "package manager commands are blocked."),
    (re.compile(r"(?i)(^|[\s;|&])shutdown(\s|$)"), "shutdown commands are blocked."),
    (re.compile(r"(?i)(^|[\s;|&])reboot(\s|$)"), "reboot commands are blocked."),
    (re.compile(r"(?i)(^|[\s;|&])poweroff(\s|$)"), "poweroff commands are blocked."),
]


def safety_check(command):
    for pattern, reason in DANGEROUS_PATTERNS:
        if pattern.search(command):
            return False, f"Blocked by safety check: {reason}"
    return True, None


def is_command_safe(command: str) -> bool:
    allowed, _reason = safety_check(command)
    return allowed


def confirm_command(command: str) -> bool:
    print("\nProposed command:")
    print(command)
    answer = input("Run this command? [y/N] ").strip().lower()
    return answer in {"y", "yes"}
