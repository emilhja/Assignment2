"""Real-time operator console (P3.5).

While `group_chat.run_group_chat` blocks on `transport.recv`, a daemon
thread reads operator commands from a stdin-like stream and mutates the
live Budget or signals an orderly shutdown. The bash approval queue lets
the orchestrator gate destructive shell commands behind a local human
confirmation (the assignment text explicitly permits this kind of local
safety system to keep using the console).

Commands (one per line, `:` prefix):

    :budget                     print Budget.snapshot()
    :limit tpm <N>              set tokens-per-minute
    :limit rpm <N>              set requests-per-minute
    :limit total <N>            set lifetime token cap
    :pause                      stop outbound LLM calls
    :resume                     undo :pause
    :continue                   retry the last actionable hub request
    :approve                    approve the pending bash/budget request (if any)
    :allow [command]            approve a pending bash command AND bypass the
                                safety allowlist for that one call (longer timeout)
    :deny                       deny the pending bash/budget request
    :say <text>                 post a message to the group chat as this agent
    :roster                     broadcast a [ROSTER] roll-call and collect replies
    :stop                       signal the orchestrator to exit
    :help                       print this list
"""

from __future__ import annotations

import queue
import sys
import threading
from dataclasses import dataclass
from typing import Callable, IO, Optional

import part2_bridge  # noqa: F401 — sys.path side effect for `colors`

import colors
from budget import Budget
from peer import scrub_outbound


HELP_TEXT = (
    "Console commands:\n"
    "  :budget                       show current usage and limits\n"
    "  :limit tpm|rpm|total <N>      set a runtime limit\n"
    "  :pause / :resume              stop or resume outbound LLM calls\n"
    "  :continue                     retry the last actionable hub request\n"
    "  :approve / :deny              answer the pending bash/budget approval\n"
    "  :allow [command]              approve a pending bash command and bypass the\n"
    "                                safety allowlist for that one call (longer timeout)\n"
    "  :say <text>                   post a message to the group chat as this agent\n"
    "  :roster                       broadcast a [ROSTER] roll-call, wait the window,\n"
    "                                then decompose work among the agents that answered\n"
    "  :project                      show active remote-hub project\n"
    "  :project new                  allocate a fresh projectN and switch to it\n"
    "  :project use <N>              switch active project to projectN\n"
    "  :project list                 list existing projects (active marked *)\n"
    "  :stop                         exit cleanly\n"
    "  :help                         print this list\n"
)


# Sentinel returned by request_bash_approval when the operator types `:allow`.
# It is truthy but distinct from plain True so the caller can tell an ordinary
# `:approve` apart from a deliberate one-shot safety-allowlist bypass.
BASH_ALLOW_OVERRIDE = "allow-override"


@dataclass
class BashApproval:
    command: str
    response: queue.Queue  # holds True (approve) / False (deny) / BASH_ALLOW_OVERRIDE


@dataclass
class BudgetApproval:
    reason: str
    estimated_tokens: int
    response: queue.Queue  # holds True (approve) / False (deny)


class ConsoleControl:
    def __init__(
        self,
        budget: Budget,
        stop_event: threading.Event,
        stdin: Optional[IO[str]] = None,
        stdout: Optional[IO[str]] = None,
        send_fn: Optional[Callable[[str], None]] = None,
        project_handler: Optional[Callable[[str, list[str]], str]] = None,
        continue_handler: Optional[Callable[[], str]] = None,
        roster_handler: Optional[Callable[[str], str]] = None,
    ):
        self.budget = budget
        self.stop_event = stop_event
        self.stdin = stdin if stdin is not None else sys.stdin
        self.stdout = stdout if stdout is not None else sys.stdout
        self.send_fn = send_fn
        self.project_handler = project_handler
        self.continue_handler = continue_handler
        self.roster_handler = roster_handler
        self._approval_lock = threading.Lock()
        self._pending: Optional[BashApproval] = None
        self._pending_budget: Optional[BudgetApproval] = None
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._loop, daemon=True, name="console-control")
        self._thread.start()

    def stop(self) -> None:
        self.stop_event.set()

    def request_bash_approval(self, command: str, timeout: Optional[float] = None):
        """Block until the operator answers. Default to deny on timeout.

        Returns True for `:approve`, False for `:deny`/timeout, and
        ``BASH_ALLOW_OVERRIDE`` for `:allow` (approve + one-shot safety bypass).
        """

        response: queue.Queue = queue.Queue(maxsize=1)
        with self._approval_lock:
            self._pending = BashApproval(command=command, response=response)
        tag = colors.paint("[approval needed]", colors.BOLD, colors.YELLOW)
        prompt = colors.paint("bash>", colors.BOLD)
        hint = colors.dim("Type :approve, :deny, or :allow (one-shot safety override).")
        self._print(f"\n{tag} {prompt} {command}\n{hint}")
        try:
            approved = response.get(timeout=timeout)
        except queue.Empty:
            approved = False
            self._print(colors.paint("[approval timed out] command denied.", colors.RED))
        finally:
            with self._approval_lock:
                if self._pending is not None and self._pending.response is response:
                    self._pending = None
        # Preserve the override sentinel; coerce everything else to a plain bool.
        if approved == BASH_ALLOW_OVERRIDE:
            return BASH_ALLOW_OVERRIDE
        return bool(approved)

    def request_budget_approval(
        self,
        reason: str,
        estimated_tokens: int,
        timeout: Optional[float] = None,
    ) -> bool:
        """Block until operator approves one over-budget LLM call."""

        response: queue.Queue = queue.Queue(maxsize=1)
        pending = BudgetApproval(
            reason=reason,
            estimated_tokens=estimated_tokens,
            response=response,
        )
        with self._approval_lock:
            self._pending_budget = pending
        tag = colors.paint("[budget approval needed]", colors.BOLD, colors.YELLOW)
        prompt = colors.paint("budget>", colors.BOLD)
        hint = colors.dim("Type :approve to allow this one LLM call, or :deny to stop.")
        self._print(f"\n{tag} {prompt} {reason} estimated_tokens={estimated_tokens}\n{hint}")
        try:
            approved = response.get(timeout=timeout)
        except queue.Empty:
            approved = False
            self._print(colors.paint("[approval timed out] budget override denied.", colors.RED))
        finally:
            with self._approval_lock:
                if self._pending_budget is pending:
                    self._pending_budget = None
        return bool(approved)

    def _print(self, text: str) -> None:
        try:
            self.stdout.write(text + "\n")
            self.stdout.flush()
        except Exception:
            pass

    def _resolve_pending(self, approved: bool) -> bool:
        with self._approval_lock:
            pending = self._pending
            pending_budget = self._pending_budget
        if pending is None:
            if pending_budget is None:
                self._print(colors.dim("[no pending approval]"))
                return False
            try:
                pending_budget.response.put_nowait(approved)
            except queue.Full:
                return False
            return True
        try:
            pending.response.put_nowait(approved)
        except queue.Full:
            return False
        return True

    def _cmd_allow(self, rest: str) -> None:
        """Approve a pending bash command AND bypass the safety allowlist for it.

        Unlike :approve, this resolves the pending bash request with the
        BASH_ALLOW_OVERRIDE sentinel so the runtime runs the command without the
        default-deny safety check. It only ever applies to a pending *bash*
        request (never a budget override). An optional command argument, when
        given, must match the pending command exactly — a guard so the operator
        cannot bypass safety for a command other than the one on screen.
        """

        requested = rest.strip()
        with self._approval_lock:
            pending = self._pending
        if pending is None:
            self._print(colors.paint("[:allow needs a pending bash command]", colors.RED))
            return
        if requested and requested != pending.command.strip():
            self._print(
                colors.paint(
                    f"[:allow command mismatch] pending command is:\n{pending.command}",
                    colors.RED,
                )
            )
            return
        try:
            pending.response.put_nowait(BASH_ALLOW_OVERRIDE)
        except queue.Full:
            return
        self._print(
            colors.paint("[allowed — one-shot safety override]", colors.BOLD, colors.YELLOW)
        )

    def _handle(self, raw: str) -> None:
        line = raw.rstrip("\r\n")
        stripped = line.strip()
        if not stripped:
            return
        if not stripped.startswith(":"):
            return
        body = stripped[1:]
        cmd, _, rest = body.partition(" ")
        cmd = cmd.lower()
        args = rest.split() if rest else []
        if cmd == "help":
            self._print(HELP_TEXT)
        elif cmd == "budget":
            self._print(_format_snapshot(self.budget.snapshot()))
        elif cmd == "limit":
            self._cmd_limit(args)
        elif cmd == "pause":
            self.budget.pause()
            self.budget.save()
            self._print(colors.paint("[budget paused]", colors.YELLOW))
        elif cmd == "resume":
            self.budget.resume()
            self.budget.save()
            self._print(colors.paint("[budget resumed]", colors.GREEN))
        elif cmd == "continue":
            self._cmd_continue()
        elif cmd == "approve":
            if self._resolve_pending(True):
                self._print(colors.paint("[approved]", colors.BOLD, colors.GREEN))
        elif cmd == "allow":
            self._cmd_allow(rest)
        elif cmd == "deny":
            if self._resolve_pending(False):
                self._print(colors.paint("[denied]", colors.BOLD, colors.RED))
        elif cmd == "say":
            self._cmd_say(rest)
        elif cmd == "roster":
            self._cmd_roster(rest)
        elif cmd == "project":
            self._cmd_project(args)
        elif cmd == "stop":
            self._print(colors.paint("[stop requested]", colors.YELLOW))
            self.stop_event.set()
        else:
            self._print(f"{colors.paint('[unknown command: ' + stripped + ']', colors.RED)}\n{HELP_TEXT}")

    def _cmd_say(self, text: str) -> None:
        message = text.strip()
        if not message:
            self._print("[usage: :say <text>]")
            return
        if self.send_fn is None:
            self._print("[say not wired \u2014 transport unavailable]")
            return
        scrubbed, hits = scrub_outbound(message)
        if hits:
            self._print(f"[say scrubbed: {sorted(set(hits))}]")
        try:
            self.send_fn(scrubbed)
        except Exception as exc:
            self._print(f"[say failed: {exc}]")

    def _cmd_roster(self, rest: str) -> None:
        """Queue a roster roll-call. The main loop runs the timed window so the
        console daemon thread never blocks on the transport."""

        if self.roster_handler is None:
            self._print("[roster not enabled in this mode]")
            return
        try:
            result = self.roster_handler(rest.strip())
        except Exception as exc:
            result = f"[roster error] {exc}"
        self._print(result)

    def _cmd_project(self, args: list[str]) -> None:
        if self.project_handler is None:
            self._print("[project not enabled in this mode]")
            return
        action = args[0].lower() if args else "info"
        rest = args[1:]
        try:
            result = self.project_handler(action, rest)
        except Exception as exc:
            result = f"[project error] {exc}"
        self._print(result)

    def _cmd_continue(self) -> None:
        if self.continue_handler is None:
            self._print("[continue not enabled in this mode]")
            return
        try:
            result = self.continue_handler()
        except Exception as exc:
            result = f"[continue error] {exc}"
        self._print(result)

    def _cmd_limit(self, args: list[str]) -> None:
        if len(args) != 2:
            self._print("usage: :limit tpm|rpm|total <N>")
            return
        name, value_raw = args
        try:
            value = int(value_raw)
        except ValueError:
            self._print(f"limit value must be an integer, got {value_raw!r}")
            return
        try:
            self.budget.set_limit(name, value)
        except ValueError as exc:
            self._print(f"[limit error] {exc}")
            return
        self.budget.save()
        self._print(f"[limit set] {name}={value}")

    def _loop(self) -> None:
        while not self.stop_event.is_set():
            try:
                line = self.stdin.readline()
            except Exception:
                break
            if not line:
                break
            self._handle(line)


def _format_snapshot(snap: dict) -> str:
    lines = ["[budget]"]
    for key in (
        "paused",
        "tokens_per_minute",
        "tokens_used_last_minute",
        "requests_per_minute",
        "requests_used_last_minute",
        "lifetime_tokens",
        "lifetime_tokens_used",
        "prompt_tokens_used",
        "completion_tokens_used",
        "total_tokens_used",
        "estimated_fallback_tokens",
        "llm_calls",
    ):
        lines.append(f"  {key}: {snap.get(key)}")
    return "\n".join(lines)
