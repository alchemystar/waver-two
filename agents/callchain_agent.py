#!/usr/bin/env python3
"""Executable low-token call-chain analysis agent.

This script turns the documentation template into a runnable shell-only workflow
with strict command/file budgets.
"""

from __future__ import annotations

import argparse
import json
import re
import shlex
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

ALLOWED_BASE_COMMANDS = {"rg", "fd", "sed", "head", "tail", "awk", "cut", "sort", "uniq"}
FILE_PATTERN = re.compile(r"([\w./-]+\.[\w]+)")


@dataclass
class Budget:
    max_files: int
    max_commands: int


@dataclass
class CommandResult:
    command: str
    output: str
    returncode: int


@dataclass
class HypothesisResult:
    name: str
    status: str
    evidence: List[str] = field(default_factory=list)


class CommandRunner:
    def run(self, command: str, cwd: Path) -> CommandResult:  # pragma: no cover - interface
        raise NotImplementedError


class ShellRunner(CommandRunner):
    def __init__(self, timeout_s: int = 8, max_output_chars: int = 1600) -> None:
        self.timeout_s = timeout_s
        self.max_output_chars = max_output_chars

    def run(self, command: str, cwd: Path) -> CommandResult:
        completed = subprocess.run(
            ["bash", "-lc", command],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=self.timeout_s,
            check=False,
        )
        merged = (completed.stdout or "") + ("\n" + completed.stderr if completed.stderr else "")
        trimmed = merged[: self.max_output_chars]
        return CommandResult(command=command, output=trimmed.strip(), returncode=completed.returncode)


class LowTokenCallchainAgent:
    def __init__(self, repo_root: Path, budget: Budget, runner: Optional[CommandRunner] = None) -> None:
        self.repo_root = repo_root
        self.budget = budget
        self.runner = runner or ShellRunner()
        self.commands_run = 0
        self.files_seen: Set[str] = set()
        self.timeline: List[CommandResult] = []

    def _command_base(self, command: str) -> str:
        tokens = shlex.split(command)
        if not tokens:
            return ""
        return tokens[0]

    def _enforce_whitelist(self, command: str) -> None:
        base = self._command_base(command)
        if base not in ALLOWED_BASE_COMMANDS:
            raise ValueError(f"Disallowed command '{base}'. Allowed: {sorted(ALLOWED_BASE_COMMANDS)}")

    def _extract_files(self, command: str, output: str) -> Iterable[str]:
        for source in (command, output):
            for match in FILE_PATTERN.findall(source):
                if match.startswith("/"):
                    continue
                if match.count("/") > 0 or "." in Path(match).name:
                    yield match

    def _within_budget(self) -> bool:
        return self.commands_run < self.budget.max_commands and len(self.files_seen) < self.budget.max_files

    def run_command(self, command: str) -> Optional[CommandResult]:
        if not self._within_budget():
            return None
        self._enforce_whitelist(command)
        result = self.runner.run(command, self.repo_root)
        self.commands_run += 1
        for fp in self._extract_files(command, result.output):
            self.files_seen.add(fp)
        self.timeline.append(result)
        return result

    def run_phase(self, commands: Sequence[str]) -> List[CommandResult]:
        results: List[CommandResult] = []
        for command in commands:
            result = self.run_command(command)
            if result is None:
                break
            results.append(result)
            if len(self.files_seen) >= self.budget.max_files:
                break
        return results

    def run_hypotheses(self, hypotheses: Sequence[Dict[str, object]]) -> List[HypothesisResult]:
        results: List[HypothesisResult] = []
        for item in hypotheses:
            name = str(item.get("name", "unnamed hypothesis"))
            commands = item.get("commands", [])
            if not isinstance(commands, list):
                raise ValueError(f"Hypothesis '{name}' commands must be a list")
            phase_results = self.run_phase([str(cmd) for cmd in commands[:3]])
            if not phase_results:
                results.append(HypothesisResult(name=name, status="partial", evidence=["budget exhausted"]))
                continue
            has_success = any(r.returncode == 0 and r.output for r in phase_results)
            status = "confirmed" if has_success else "rejected"
            evidence = [f"`{r.command}` -> exit {r.returncode}" for r in phase_results]
            results.append(HypothesisResult(name=name, status=status, evidence=evidence))
            if not self._within_budget():
                break
        return results

    def stop_reason(self) -> str:
        if self.commands_run >= self.budget.max_commands or len(self.files_seen) >= self.budget.max_files:
            return "budget-exhausted"
        return "closed-loop"


def build_report(question: str, map_results: Sequence[CommandResult], hypotheses: Sequence[HypothesisResult], agent: LowTokenCallchainAgent) -> str:
    lines: List[str] = []
    lines.append("## Confirmed Call Chain")
    lines.append(f"1) Question scope: {question}")
    for hyp in hypotheses:
        lines.append(f"2) {hyp.name}: {hyp.status}")
    lines.append("")

    lines.append("## Evidence")
    for result in map_results:
        lines.append(f"- Map: `{result.command}` (exit={result.returncode})")
    for hyp in hypotheses:
        for ev in hyp.evidence:
            lines.append(f"- {hyp.name}: {ev}")
    lines.append("")

    lines.append("## Uncertainties")
    uncertain = [h for h in hypotheses if h.status != "confirmed"]
    if uncertain:
        for u in uncertain:
            lines.append(f"- {u.name} requires deeper verification.")
    else:
        lines.append("- No major uncertainty under current budget.")
    lines.append("")

    lines.append("## Minimal Next Commands")
    lines.append("1. `rg \"target_symbol\" path/to/module`")
    lines.append("2. `sed -n \"start,endp\" path/to/file`")
    lines.append("")

    lines.append("## Budget Report")
    lines.append(f"- Files opened: {len(agent.files_seen)}/{agent.budget.max_files}")
    lines.append(f"- Commands run: {agent.commands_run}/{agent.budget.max_commands}")
    lines.append(f"- Stop reason: {agent.stop_reason()}")
    return "\n".join(lines)


def load_spec(spec_path: Path) -> Dict[str, object]:
    with spec_path.open("r", encoding="utf-8") as f:
        return json.load(f)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Low-token call-chain analysis runner")
    parser.add_argument("--spec", required=True, help="Path to JSON spec file")
    parser.add_argument("--repo", default=".", help="Repository root")
    parser.add_argument("--out", help="Optional output markdown file")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    spec = load_spec(Path(args.spec))

    question = str(spec.get("question", "unspecified question"))
    budget = Budget(
        max_files=int(spec.get("budget", {}).get("max_files", 8)),
        max_commands=int(spec.get("budget", {}).get("max_commands", 12)),
    )

    map_commands = [str(cmd) for cmd in spec.get("phase_a_commands", [])][:4]
    hypotheses = spec.get("hypotheses", [])
    if not isinstance(hypotheses, list):
        raise ValueError("hypotheses must be a list")

    agent = LowTokenCallchainAgent(repo_root=Path(args.repo), budget=budget)
    map_results = agent.run_phase(map_commands)
    hypothesis_results = agent.run_hypotheses(hypotheses)
    report = build_report(question, map_results, hypothesis_results, agent)

    if args.out:
        Path(args.out).write_text(report, encoding="utf-8")
    else:
        print(report)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
