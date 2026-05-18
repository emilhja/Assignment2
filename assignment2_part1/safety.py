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

def safety_check(command):
    pieces = command.replace("\n", ";").split(";")

    for piece in pieces:
        pipe_parts = piece.split("|")
        for pipe_part in pipe_parts:
            and_parts = pipe_part.split("&")
            for and_part in and_parts:
                words = and_part.strip().split()
                if not words:
                    continue

                command_name = words[0].lower()
                if command_name not in BLOCKED_COMMANDS:
                    continue

                if command_name in {"rm", "rmdir"}:
                    reason = f"I will not run {command_name} from this agent."
                elif command_name == "sudo":
                    reason = "I cannot use sudo from here."
                elif command_name in {"docker", "docker-compose"}:
                    reason = "Docker needs to be run on the host machine."
                elif command_name in {"apt", "apt-get", "apk", "dnf", "yum"}:
                    reason = "I cannot run package managers from here."
                else:
                    reason = f"I will not run {command_name} from this agent."

                return False, f"Blocked by safety check: {reason}"
    return True, None


def is_command_safe(command):
    allowed, _reason = safety_check(command)
    return allowed


def confirm_command(command):
    print("\nProposed command:")
    print(command)
    answer = input("Run this command? [y/N] ").strip().lower()
    return answer in {"y", "yes"}
