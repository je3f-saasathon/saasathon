"""The agent loop's conversation, and trimming of old turns.

Every turn resends the whole conversation, so a file read at turn 2 is paid for again on
every later turn. `compact` replaces what the agent no longer needs with a short stub:
reads a later read or write has replaced, the content of applied writes, and all but the
latest command output. The first message (the incident, playbook, preloaded files) and the
last few turns are never touched. Trimming runs every few turns rather than every turn,
so the provider's prompt cache still covers the unchanged prefix in between."""

import json
from dataclasses import dataclass, field

from .context import untrusted

TAIL_LINES = 8


@dataclass
class Turn:
    action: dict  # {} for an invalid reply
    reply_index: int
    result_index: int
    observation: str  # the raw tool result, before untrusted() wrapping
    label: str = ""  # Jev's reading of a failed command's output, e.g. "tests_failed"
    trimmed: set = field(default_factory=set)  # "reply" and/or "result"

    @property
    def kind(self) -> str:
        return str(self.action.get("action") or "invalid")

    @property
    def path(self) -> str:
        return str(self.action.get("path") or ".")

    def describe(self) -> str:
        if self.kind == "run_command":
            return f"run_command {str(self.action.get('command'))[:120]!r}"
        if self.kind in ("read_file", "write_file", "list_files"):
            return f"{self.kind} {self.path}"
        return "invalid reply"


class AgentHistory:
    def __init__(self, kickoff: str):
        self.messages: list[dict] = [{"role": "user", "content": kickoff}]
        self.turns: list[Turn] = []

    def add_turn(self, reply: str, action: dict | None, result: str, observation: str = "",
                 label: str = "") -> Turn:
        self.messages.append({"role": "assistant", "content": reply})
        self.messages.append({"role": "user", "content": result})
        turn = Turn(action or {}, len(self.messages) - 2, len(self.messages) - 1, observation,
                    label)
        self.turns.append(turn)
        return turn

    def note(self, text: str) -> None:
        """Adds our own note to the last message (keeps user/assistant alternation)."""
        self._set(len(self.messages) - 1, self.messages[-1]["content"] + "\n\n" + text)

    def chars(self) -> int:
        return sum(len(m["content"]) for m in self.messages)

    def action_log(self, limit: int = 30) -> str:
        """One line per turn, for judging whether the agent is making progress."""
        lines = []
        for number, turn in enumerate(self.turns[-limit:], start=max(0, len(self.turns) - limit)):
            result = turn.observation.splitlines()[0][:80] if turn.observation else ""
            if turn.label:
                result += f" ({turn.label})"
            lines.append(f"{number}. {turn.describe()} -> {result}")
        return "\n".join(lines)

    def _old(self, keep_recent: int) -> list[tuple[int, Turn]]:
        return list(enumerate(self.turns))[:max(0, len(self.turns) - keep_recent)]

    def _stale(self, index: int, turn: Turn) -> bool:
        """A read or listing a later turn has replaced, or command output that isn't the
        latest command's."""
        later = self.turns[index + 1:]
        if turn.kind == "read_file":
            return any(t.kind in ("read_file", "write_file") and t.path == turn.path for t in later)
        if turn.kind == "list_files":
            return any(t.kind == "list_files" and t.path == turn.path for t in later)
        if turn.kind == "run_command":
            return any(t.kind == "run_command" for t in later)
        return False

    def review_candidates(self, keep_recent: int) -> list[int]:
        """Old reads and listings the rules keep; Jev may still judge them irrelevant."""
        return [i for i, turn in self._old(keep_recent)
                if turn.kind in ("read_file", "list_files") and "result" not in turn.trimmed
                and not self._stale(i, turn)]

    def compact(self, keep_recent: int, drop: set[int] = frozenset()) -> int:
        """Trims turns older than the last `keep_recent`; `drop` are turn indexes whose
        results were judged no longer relevant. Returns the characters saved."""
        before = self.chars()
        for index, turn in self._old(keep_recent):
            if "reply" not in turn.trimmed:
                if turn.kind == "write_file":
                    lines = str(turn.action.get("content", "")).count("\n") + 1
                    stub = {**turn.action,
                            "content": f"[{lines} lines written; removed from history, "
                                       "read_file shows the current version]"}
                    self._set(turn.reply_index, json.dumps(stub))
                    turn.trimmed.add("reply")
                elif turn.kind == "invalid":
                    self._set(turn.reply_index, "[invalid reply removed from history]")
                    turn.trimmed.add("reply")
            if "result" in turn.trimmed:
                continue
            if turn.kind == "run_command" and self._stale(index, turn):
                self._set(turn.result_index, self._command_stub(turn))
            elif turn.kind in ("read_file", "list_files") and self._stale(index, turn):
                self._set(turn.result_index,
                          f"[result of {turn.describe()} removed from history: a later turn "
                          "replaced it. Read it again if you need it.]")
            elif index in drop:
                self._set(turn.result_index,
                          f"[result of {turn.describe()} removed from history as no longer "
                          "relevant. Repeat the action if you need it.]")
            else:
                continue
            turn.trimmed.add("result")
        return before - self.chars()

    def _command_stub(self, turn: Turn) -> str:
        lines = turn.observation.splitlines()
        head = lines[0] if lines else ""
        tail = "\n".join(lines[1:][-TAIL_LINES:])
        label = f", {turn.label}" if turn.label else ""
        return (f"[{turn.describe()}: {head}{label}. Output trimmed to its last lines; "
                "a later command superseded it.]\n" + untrusted("tool_result_tail", tail))

    def _set(self, index: int, content: str) -> None:
        # A new dict, so a list already handed to a client (or a trace) keeps what was sent.
        self.messages[index] = {**self.messages[index], "content": content}
