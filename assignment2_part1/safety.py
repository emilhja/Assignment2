import re


# Plain-English requests the agent should refuse before asking the model.
FORBIDDEN_INTENT_PATTERNS = [
    (
        re.compile(r"(?i)\b(delete|remove)\s+(the\s+)?whole\s+(folder|directory)\b"),
        "Deleting a whole folder is not allowed.",
    ),
    (
        re.compile(r"(?i)\b(delete|remove)\s+everything\b"),
        "Deleting everything is not allowed.",
    ),
    (
        re.compile(r"(?i)\b(delete|remove)\s+all\s+(files|folders|directories)?\b"),
        "Broad deletion is not allowed.",
    ),
    (
        re.compile(r"(?i)\bwipe\b"),
        "Wiping files is not allowed.",
    ),
]

# commands the agent is not allowed to run
BLOCKED_REASONS = {
    "rm": "rm is not allowed",
    "rmdir": "rmdir is not allowed",
    "sudo": "sudo not allowed",
    "docker": "docker commands aren't allowed here",
    "docker-compose": "docker commands aren't allowed here",
    "apt": "package managers are blocked",
    "apt-get": "package managers are blocked",
    "apk": "package managers are blocked",
    "dnf": "package managers are blocked",
    "yum": "package managers are blocked",
    "shutdown": "shutdown/reboot not allowed",
    "reboot": "shutdown/reboot not allowed",
    "poweroff": "poweroff is not allowed",
}


def intent_refusal(user_task):
    for pattern, reason in FORBIDDEN_INTENT_PATTERNS:
        if pattern.search(user_task):
            return reason
    return None


def safety_check(command):
    pieces = command.replace("\n", ";").split(";")

    for part in pieces:
        pipe_parts = part.split("|")
        for pipe_part in pipe_parts:
            and_parts = pipe_part.split("&")
            for and_part in and_parts:
                words = and_part.strip().split()
                if not words:
                    continue

                cmd = words[0].lower()
                reason = BLOCKED_REASONS.get(cmd)
                if reason:
                    return False, f"Blocked: {reason}"

    return True, None


def is_command_safe(command):
    allowed, _reason = safety_check(command)
    return allowed


def confirm_command(command):
    print("\nProposed command:")
    print(command)
    answer = input("Run this command? [y/N] ").strip().lower()
    return answer in {"y", "yes"}
