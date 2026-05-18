BLOCKED_COMMANDS = [
    "rm",
    "rmdir",
    "sudo",
    "docker",
    "docker-compose",
    "apt",
    "apt-get",
    "apk",
    "dnf",
    "yum",
    "shutdown",
    "reboot",
    "poweroff",
]


def _reason_for(command_name):
    if command_name in {"rm", "rmdir"}:
        return f"{command_name} commands are blocked."
    if command_name == "sudo":
        return "sudo commands are blocked."
    if command_name in {"docker", "docker-compose"}:
        return "Docker commands must be run on the host."
    if command_name in {"apt", "apt-get", "apk", "dnf", "yum"}:
        return "package manager commands are blocked."
    return f"{command_name} commands are blocked."


def _command_starts(command):
    pieces = command.replace("\n", ";").split(";")

    for piece in pieces:
        pipe_parts = piece.split("|")
        for pipe_part in pipe_parts:
            and_parts = pipe_part.split("&")
            for and_part in and_parts:
                words = and_part.strip().split()
                if words:
                    yield words[0].lower()


def safety_check(command):
    for command_name in _command_starts(command):
        if command_name in BLOCKED_COMMANDS:
            return False, f"Blocked by safety check: {_reason_for(command_name)}"
    return True, None


def is_command_safe(command):
    allowed, _reason = safety_check(command)
    return allowed


def confirm_command(command):
    print("\nProposed command:")
    print(command)
    answer = input("Run this command? [y/N] ").strip().lower()
    return answer in {"y", "yes"}
