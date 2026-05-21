import io
import threading
import time

from budget import Budget
from console_control import ConsoleControl


def _wait_for(predicate, timeout=2.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def _start_console(stdin_text, **kwargs):
    budget = Budget(tokens_per_minute=100, requests_per_minute=10, lifetime_tokens=1000)
    stop = threading.Event()
    stdin = io.StringIO(stdin_text)
    stdout = io.StringIO()
    cc = ConsoleControl(budget=budget, stop_event=stop, stdin=stdin, stdout=stdout, **kwargs)
    cc.start()
    return cc, budget, stop, stdout


def test_limit_tpm_command_mutates_budget(tmp_path):
    cc, budget, stop, stdout = _start_console(":limit tpm 5000\n")
    assert _wait_for(lambda: budget.tokens_per_minute == 5000)
    stop.set()


def test_pause_and_resume():
    cc, budget, stop, stdout = _start_console(":pause\n:resume\n")
    assert _wait_for(lambda: budget.paused is False and "[budget paused]" in stdout.getvalue())
    stop.set()


def test_budget_command_prints_snapshot():
    cc, budget, stop, stdout = _start_console(":budget\n")
    assert _wait_for(lambda: "tokens_per_minute" in stdout.getvalue())
    stop.set()


def test_stop_command_sets_stop_event():
    cc, budget, stop, stdout = _start_console(":stop\n")
    assert _wait_for(stop.is_set)


def _build_console(stdin_text):
    """Build a console without starting its thread, so the test can
    register a pending approval before the operator command is read."""

    budget = Budget(tokens_per_minute=100, requests_per_minute=10, lifetime_tokens=1000)
    stop = threading.Event()
    stdin = io.StringIO(stdin_text)
    stdout = io.StringIO()
    cc = ConsoleControl(budget=budget, stop_event=stop, stdin=stdin, stdout=stdout)
    return cc, budget, stop, stdout


def test_approve_releases_pending_bash():
    cc, budget, stop, stdout = _build_console(":approve\n")
    result_holder = {}
    def worker():
        result_holder["approved"] = cc.request_bash_approval("ls -la /workspace", timeout=2.0)
    t = threading.Thread(target=worker)
    t.start()
    # Wait until the worker has registered the pending request, then start
    # the console thread that reads the :approve line.
    assert _wait_for(lambda: cc._pending is not None)
    cc.start()
    t.join(timeout=3.0)
    assert result_holder.get("approved") is True
    stop.set()


def test_deny_releases_pending_bash():
    cc, budget, stop, stdout = _build_console(":deny\n")
    result_holder = {}
    def worker():
        result_holder["approved"] = cc.request_bash_approval("ls -la /workspace", timeout=2.0)
    t = threading.Thread(target=worker)
    t.start()
    assert _wait_for(lambda: cc._pending is not None)
    cc.start()
    t.join(timeout=3.0)
    assert result_holder.get("approved") is False
    stop.set()


def test_unknown_command_prints_help():
    cc, budget, stop, stdout = _start_console(":bogus arg\n")
    assert _wait_for(lambda: "unknown command" in stdout.getvalue())
    stop.set()


def test_say_invokes_send_fn():
    sent: list[str] = []
    cc, budget, stop, stdout = _start_console(
        ":say hello group chat\n", send_fn=sent.append
    )
    assert _wait_for(lambda: sent == ["hello group chat"])
    stop.set()


def test_say_without_text_prints_usage():
    sent: list[str] = []
    cc, budget, stop, stdout = _start_console(":say   \n", send_fn=sent.append)
    assert _wait_for(lambda: "usage: :say" in stdout.getvalue())
    assert sent == []
    stop.set()


def test_say_without_send_fn_warns():
    cc, budget, stop, stdout = _start_console(":say hello\n")
    assert _wait_for(lambda: "say not wired" in stdout.getvalue())
    stop.set()


def test_say_failure_is_caught():
    def boom(_text):
        raise RuntimeError("offline")

    cc, budget, stop, stdout = _start_console(":say try\n", send_fn=boom)
    assert _wait_for(lambda: "[say failed: offline]" in stdout.getvalue())
    stop.set()
