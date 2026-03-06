import unittest
from pathlib import Path

from agents.callchain_agent import Budget, CommandResult, LowTokenCallchainAgent


class FakeRunner:
    def __init__(self):
        self.calls = []

    def run(self, command: str, cwd: Path) -> CommandResult:
        self.calls.append(command)
        return CommandResult(command=command, output="src/main.py:10", returncode=0)


class AgentTests(unittest.TestCase):
    def test_budget_enforced(self):
        runner = FakeRunner()
        agent = LowTokenCallchainAgent(Path("."), Budget(max_files=2, max_commands=1), runner=runner)
        first = agent.run_command("rg \"x\" src")
        second = agent.run_command("rg \"y\" src")
        self.assertIsNotNone(first)
        self.assertIsNone(second)

    def test_whitelist_enforced(self):
        runner = FakeRunner()
        agent = LowTokenCallchainAgent(Path("."), Budget(max_files=8, max_commands=8), runner=runner)
        with self.assertRaises(ValueError):
            agent.run_command("python bad.py")


if __name__ == "__main__":
    unittest.main()
