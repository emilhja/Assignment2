import re


FORBIDDEN_INTENTS: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(r"\b(delete|remove)\s+(the\s+)?whole\s+(folder|directory)\b", re.I),
        "Deleting a whole folder is not allowed.",
    ),
    (
        re.compile(r"\b(delete|remove)\s+everything\b", re.I),
        "Deleting everything is not allowed.",
    ),
    (
        re.compile(r"\b(delete|remove)\s+all\s+(files|folders|directories)?\b", re.I),
        "Broad deletion is not allowed.",
    ),
    (re.compile(r"\bwipe\b", re.I), "Wiping files is not allowed."),
)

BLOCKED_COMMANDS: dict[str, str] = {
    "rm": "rm is not allowed",
    "rmdir": "rmdir is not allowed",
    "sudo": "sudo is not allowed",
    "docker": "Docker commands are not allowed from this agent",
    "docker-compose": "Docker commands are not allowed from this agent",
    "apt": "package managers are blocked",
    "apt-get": "package managers are blocked",
    "apk": "package managers are blocked",
    "dnf": "package managers are blocked",
    "yum": "package managers are blocked",
    "shutdown": "shutdown is not allowed",
    "reboot": "reboot is not allowed",
    "poweroff": "poweroff is not allowed",
}


def refuse_user_intent(user_task: str) -> str | None:
    for pattern, reason in FORBIDDEN_INTENTS:
        if pattern.search(user_task):
            return reason
    return None


def _command_heads(command: str) -> list[str]:
    chunks = re.split(r"[;\n|&]+", command)
    heads: list[str] = []
    for chunk in chunks:
        words = chunk.strip().split()
        if words:
            heads.append(words[0].lower())
    return heads


def check_command(command: str) -> tuple[bool, str | None]:
    for head in _command_heads(command):
        reason = BLOCKED_COMMANDS.get(head)
        if reason:
            return False, f"Blocked: {reason}."
    return True, None


def is_command_safe(command: str) -> bool:
    allowed, _reason = check_command(command)
    return allowed


def confirm_command(command: str) -> bool:
    print("\nProposed command:")
    print(command)
    answer = input("Run this command? [y/N] ").strip().lower()
    return answer in {"y", "yes"}
