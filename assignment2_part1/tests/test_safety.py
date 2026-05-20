from safety import intent_refusal, is_command_safe


def test_blocks_delete_commands():
    assert not is_command_safe("rm -rf /workspace")
    assert not is_command_safe("rmdir /workspace")


def test_refuses_broad_delete_intents():
    assert intent_refusal("delete the whole folder")
    assert intent_refusal("can you delete all files")


def test_blocks_host_level_commands():
    assert not is_command_safe("sudo apt update")
    assert not is_command_safe("shutdown now")
    assert not is_command_safe("reboot")


def test_blocks_docker_and_package_manager_commands():
    assert not is_command_safe("docker compose ps")
    assert not is_command_safe("docker-compose ps")
    assert not is_command_safe("apt update")
    assert not is_command_safe("apk add git")
    assert not is_command_safe("dnf install git")
    assert not is_command_safe("yum install git")


def test_allows_safe_read_commands():
    assert is_command_safe("pwd")
    assert is_command_safe("ls -la /workspace")
    assert is_command_safe("cat /workspace/demo.txt")
