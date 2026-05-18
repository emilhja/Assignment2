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


def confirm_command(command):
    print("\nProposed command:")
    print(command)
    answer = input("Run this command? [y/N] ").strip().lower()
    return answer in {"y", "yes"}
