from part1.safety import check_command, is_command_safe, refuse_user_intent


def test_refuses_broad_delete_intents():
    assert refuse_user_intent("delete the whole folder")
    assert refuse_user_intent("remove everything")
    assert refuse_user_intent("delete all files")
    assert refuse_user_intent("wipe the workspace")


def test_blocks_destructive_commands():
    assert not is_command_safe("rm -rf /workspace")
    assert not is_command_safe("rmdir /workspace")


def test_blocks_host_level_commands():
    assert not is_command_safe("sudo apt update")
    assert not is_command_safe("shutdown now")
    assert not is_command_safe("reboot")
    assert not is_command_safe("poweroff")


def test_blocks_docker_and_package_managers():
    assert not is_command_safe("docker compose ps")
    assert not is_command_safe("docker-compose ps")
    assert not is_command_safe("apt update")
    assert not is_command_safe("apt-get update")
    assert not is_command_safe("apk add git")
    assert not is_command_safe("dnf install git")
    assert not is_command_safe("yum install git")


def test_blocks_commands_after_shell_separators():
    allowed, reason = check_command("pwd && rm -rf /workspace")

    assert not allowed
    assert "rm" in reason


def test_allows_safe_read_commands():
    assert is_command_safe("pwd")
    assert is_command_safe("ls -la /workspace")
    assert is_command_safe("cat /workspace/demo.txt")
