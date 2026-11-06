"""Eval harness: run every task several times against a fresh bank, grade it, and report.

    uv run python -m evals.run --suite regression --trials 3 --gate 0.9
    uv run python -m evals.run --model claude-sonnet-5-5 --compare results/baseline.json

E1 [DE] vocabulary, as used in this file:
  task        one complaint plus its graders (evals/tasks.yaml)
  trial       one attempt at a task. Outputs vary between runs, so each task gets several
  transcript  the full record of a trial: messages, tool calls, usage (results/transcripts/)
  outcome     the final state of the bank's database, not what the agent says it did
  grader      code or a model that scores one aspect of a trial
  harnesses   this file is the evaluation harness; agent.py is the agent harness under test
"""

from __future__ import annotations

import argparse
import asyncio
import json
import operator
import sqlite3
import sys
import tempfile
import time
from math import comb
from pathlib import Path
from typing import Literal

import anthropic
import yaml
from pydantic import BaseModel

from agent import Config, connect
from bank_server import create_db

ROOT = Path(__file__).resolve().parent.parent
TASKS = yaml.safe_load((ROOT / "evals" / "tasks.yaml").read_text())
RESULTS = ROOT / "results"
# The judge should be at least as capable as the agent it grades. Low effort is enough for
# one narrow criterion at a time.
JUDGE_MODEL = "claude-opus-5-5"
JUDGE_EFFORT = "low"
JUDGE_MAX_TOKENS = 4000


# --------------------------------------------------------------------------- metrics
# E5 [DE]: with non-deterministic agents, one run says little. pass@k is the chance at least
#   one of k attempts succeeds (fine when a person picks the best of several). pass^k is the
#   chance ALL k succeed: at 75% per trial, pass^3 is about 42%. A customer-facing agent has to
#   work every time, so CI gates on pass^k. Both are unbiased estimates from n >= k trials;
#   with fewer usable trials than k there's no estimate, and the gate treats that as a fail.

def pass_at_k(n: int, c: int, k: int) -> float:
    return 1.0 - comb(n - c, k) / comb(n, k)


def pass_hat_k(n: int, c: int, k: int) -> float:
    return comb(c, k) / comb(n, k)


# --------------------------------------------------------------------------- graders

OPS = {"==": operator.eq, "<=": operator.le, ">=": operator.ge}


def grade_code(task: dict, db_path: Path, tool_log: list[dict]) -> list[dict]:
    """The deterministic graders: fast, cheap and objective, so they're preferred where possible."""
    grades = []
    for g in task["graders"]:
        if g["type"] == "state":  # E2: the outcome, read straight from the database
            with sqlite3.connect(db_path) as con:
                value = con.execute(g["sql"]).fetchone()[0]
            op, target = g["expect"].split()
            passed = OPS[op](round(float(value), 2), float(target))
            grades.append({"name": g["name"], "passed": passed, "detail": f"got {value}"})
        elif g["type"] == "tool_calls":  # E3: only the steps an auditor would check
            succeeded = {c["name"] for c in tool_log if not c["is_error"]}
            called = sorted(succeeded & set(g.get("must_not_succeed", [])))
            missing = sorted(set(g.get("must_succeed", [])) - succeeded)
            detail = "; ".join([f"should not have called {called}"] * bool(called)
                               + [f"never called {missing}"] * bool(missing))
            grades.append({"name": g["name"], "passed": not (called or missing), "detail": detail})
    return grades


class Verdict(BaseModel):
    reasoning: str
    verdict: Literal["PASS", "FAIL", "UNKNOWN"]


JUDGE_PROMPT = """You are grading one reply from a bank's complaints assistant against a single criterion.

<complaint>{complaint}</complaint>
<actions_taken>{actions}</actions_taken>
<reply>{reply}</reply>
<criterion>{criterion}</criterion>

Does the reply meet the criterion? Answer UNKNOWN if you can't tell from what's here. Keep your reasoning brief."""


async def grade_judge(client, task: dict, tool_log: list[dict], reply: str) -> list[dict]:
    # E4 [DE]: model graders for what code can't check (tone, clarity, what was disclosed).
    #   One isolated call per dimension rather than one call scoring everything. UNKNOWN gives
    #   the judge a way out instead of a guess; it counts as a fail and gets flagged for a human.
    #   Structured output means no parsing of free text. Calibrate against human labels before
    #   trusting a judge to gate anything.
    # A3 [HD]: a separate evaluator, not the agent grading itself. Self-evaluation skews positive.
    actions = "\n".join(f"{c['name']}({json.dumps(c['input'])}) error={c['is_error']}" for c in tool_log)
    dims = {k: v for g in task["graders"] if g["type"] == "judge" for k, v in g["dimensions"].items()}

    async def one(name: str, criterion: str) -> dict:
        prompt = JUDGE_PROMPT.format(complaint=task["complaint"], actions=actions or "none",
                                     reply=reply, criterion=criterion)
        response = await client.messages.parse(
            model=JUDGE_MODEL, max_tokens=JUDGE_MAX_TOKENS, output_format=Verdict,
            output_config={"effort": JUDGE_EFFORT}, messages=[{"role": "user", "content": prompt}])
        v = response.parsed_output or Verdict(reasoning="no verdict returned", verdict="UNKNOWN")
        return {"name": name, "passed": v.verdict == "PASS", "unknown": v.verdict == "UNKNOWN",
                "detail": f"{v.verdict}: {v.reasoning}"}

    return list(await asyncio.gather(*(one(n, c) for n, c in dims.items())))


# --------------------------------------------------------------------------- trials

async def run_trial(task: dict, i: int, config: Config, judge_client) -> dict:
    # E8 [DE]: each trial starts clean, with its own database and its own MCP server process,
    #   so one trial can't leak state into another and cause correlated failures.
    with tempfile.TemporaryDirectory() as tmp:
        db_path = create_db(Path(tmp) / "bank.db")
        start = time.monotonic()
        try:
            async with connect(config, db_path) as agent:
                reply = await agent.run(task["complaint"])
            seconds = time.monotonic() - start
            grades = grade_code(task, db_path, agent.tool_log)
            grades += await grade_judge(judge_client, task, agent.tool_log, reply)
            with sqlite3.connect(db_path) as con:  # read before the trial's database is deleted
                audit_log = [dict(zip(("ts", "role", "tool", "args", "outcome"), row))
                             for row in con.execute("SELECT * FROM audit_log")]
        except Exception as e:
            # Infrastructure errors (API outages, rate limits, a crashed server) aren't agent
            # failures. They're left out of pass^k and reported separately, so they can't be
            # read as a drop in quality. No transcript is written for them.
            return {"task": task["id"], "trial": i, "error": repr(e)}

    path = RESULTS / "transcripts" / f"{task['id']}-{i}.json"
    summary = {
        "task": task["id"], "trial": i, "model": config.model, "effort": config.effort,
        "guard_model": config.guard_model,
        "passed": all(g["passed"] for g in grades),
        # E11 [DE]: partial credit as well as pass/fail, so progress shows before a task flips.
        "score": sum(g["passed"] for g in grades) / len(grades),
        "failed": [g["name"] for g in grades if not g["passed"]],
        "unknown": [g["name"] for g in grades if g.get("unknown")],  # for a human to review
        "grades": grades,
        "reply": reply, "stop_reason": agent.stop_reason,
        "handed_off": agent.handoff_reason is not None, "handoff_reason": agent.handoff_reason,
        # E10/K2: the numbers that decide whether a change is worth it.
        "model_calls": agent.usage.calls, "cost_usd": agent.cost_usd,
        "cache_hit_rate": agent.usage.cache_hit_rate, "seconds": seconds,
        "redactions": len(agent.redacted), "transcript": str(path.relative_to(ROOT)),
    }

    # E10 [DE]: save the whole transcript, summary first and raw detail after. Reading
    #   transcripts is how you find out whether a failure is the agent's fault or the grader's.
    #   Three records of the same conversation, from three points of view:
    #     tool_log   what the harness ran, in order, and whether each call errored
    #     audit_log  what the bank's server allowed or refused (its own record, S5)
    #     messages   exactly what the model saw and said, including its reasoning summaries
    dump = lambda b: b.model_dump(exclude_none=True) if hasattr(b, "model_dump") else b
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        **summary,
        "complaint": task["complaint"],
        "tool_log": agent.tool_log,
        "redacted": agent.redacted,
        "audit_log": audit_log,
        "messages": [{"role": m["role"], "content": m["content"] if isinstance(m["content"], str)
                      else [dump(b) for b in m["content"]]} for m in agent.messages],
    }, indent=2, default=str, ensure_ascii=False))
    return summary


def summarise(trials: list[dict], k: int) -> dict:
    tasks = {}
    for task in TASKS:
        ts = [t for t in trials if t["task"] == task["id"]]
        if not ts:
            continue
        ok = [t for t in ts if "error" not in t]
        n, c = len(ok), sum(t["passed"] for t in ok)
        mean = lambda key: sum(t[key] for t in ok) / n if n else 0.0
        tasks[task["id"]] = {
            "suite": task["suite"], "n": n, "passed": c, "errors": len(ts) - n,
            "pass_at_k": pass_at_k(n, c, k) if n >= k else None,
            "pass_hat_k": pass_hat_k(n, c, k) if n >= k else None,
            "score": mean("score"), "cost_usd": mean("cost_usd"), "model_calls": mean("model_calls"),
            "seconds": mean("seconds"), "cache_hit_rate": mean("cache_hit_rate"),
            "handoffs": sum(t["handed_off"] for t in ok),
            "failed_graders": sorted({g for t in ok for g in t["failed"]}),
            "needs_review": sorted({g for t in ok for g in t["unknown"]}),
        }
    return tasks


def print_failures(trials: list[dict]) -> None:
    """What went wrong in each failed trial, so you know which transcript to read first."""
    failed = [t for t in trials if "error" in t or not t["passed"]]
    if not failed:
        print("\nEvery trial passed.")
        return
    print(f"\nFailed trials ({len(failed)}):")
    for t in sorted(failed, key=lambda t: (t["task"], t["trial"])):
        print(f"\n  {t['task']} #{t['trial']}")
        if "error" in t:
            print(f"    infrastructure error: {t['error']}")
            continue
        for g in t["grades"]:
            if not g["passed"]:
                print(f"    x {g['name']}: {g['detail'] or 'failed'}")
        if t["handoff_reason"]:
            print(f"    handed off: {t['handoff_reason']}")
        print(f"    reply: {' '.join(t['reply'].split())[:160]}")
        print(f"    transcript: {t['transcript']}")


def fmt(x: float | None) -> str:
    return "     -" if x is None else f"{x:6.2f}"


def print_report(tasks: dict, k: int, baseline: dict | None) -> None:
    print(f"\n{'task':36} {'suite':10} {'pass':>5} pass@{k} pass^{k} {'score':>5} {'$/task':>7} "
          f"{'calls':>5} {'secs':>5} {'cache':>5} {'handoffs':>8}")
    for tid, r in tasks.items():
        print(f"{tid:36} {r['suite']:10} {r['passed']:>2}/{r['n']:<2} {fmt(r['pass_at_k'])} "
              f"{fmt(r['pass_hat_k'])} {r['score']:5.2f} {r['cost_usd']:7.4f} {r['model_calls']:5.1f} "
              f"{r['seconds']:5.1f} {r['cache_hit_rate']:5.0%} {r['handoffs']:>8}"
              + (f"  failed: {', '.join(r['failed_graders'])}" if r["failed_graders"] else "")
              + (f"  review: {', '.join(r['needs_review'])}" if r["needs_review"] else "")
              + (f"  infra errors: {r['errors']}" if r["errors"] else ""))
        if r["suite"] == "capability" and r["pass_hat_k"] == 1.0:
            # E6 [DE]: a capability task that passes every time has graduated. Move it to the
            #   regression suite and write a harder one, or the suite stops telling you anything.
            print(f"  ^ {tid} passed every trial: move it to regression")
    if baseline:
        # E10 [DE]: for a model upgrade, the useful output is which tasks flipped, plus the
        #   change in cost and latency. That's the evidence for adopting a new model in days.
        print("\nChanges against baseline:")
        for tid, r in tasks.items():
            old = baseline.get(tid)
            if old and old["pass_hat_k"] != r["pass_hat_k"]:
                print(f"  {tid}: pass^k {fmt(old['pass_hat_k'])} -> {fmt(r['pass_hat_k'])}")
        shared = [t for t in tasks if t in baseline]
        for key in ("cost_usd", "seconds"):
            before = sum(baseline[t][key] for t in shared)
            after = sum(tasks[t][key] for t in shared)
            print(f"  total {key}: {before:.3f} -> {after:.3f}")


async def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--suite", choices=["regression", "capability", "all"], default="all")
    p.add_argument("--trials", type=int, default=3, help="trials per task (n)")
    p.add_argument("--k", type=int, help="k for pass@k and pass^k (default: --trials)")
    p.add_argument("--model", default=Config.model)
    p.add_argument("--effort", default=Config.effort)
    p.add_argument("--no-guard", action="store_true", help="turn off the injection screen (S7)")
    p.add_argument("--gate", type=float, help="fail unless mean regression pass^k reaches this")
    p.add_argument("--compare", type=Path, help="an earlier results JSON to diff against")
    p.add_argument("--concurrency", type=int, default=4)
    args = p.parse_args()
    k = args.k or args.trials

    config = Config(model=args.model, effort=args.effort,
                    guard_model=None if args.no_guard else Config.guard_model)
    judge_client = anthropic.AsyncAnthropic()
    semaphore = asyncio.Semaphore(args.concurrency)

    async def limited(task: dict, i: int) -> dict:
        async with semaphore:
            return await run_trial(task, i, config, judge_client)

    selected = [t for t in TASKS if args.suite in ("all", t["suite"])]
    trials = await asyncio.gather(*(limited(t, i) for t in selected for i in range(args.trials)))
    tasks = summarise(trials, k)

    baseline = json.loads(args.compare.read_text())["tasks"] if args.compare else None
    print(f"model={config.model} effort={config.effort} guard={config.guard_model} "
          f"trials={args.trials} k={k}")
    print_report(tasks, k, baseline)
    print_failures(trials)
    RESULTS.mkdir(exist_ok=True)
    (RESULTS / "latest.json").write_text(json.dumps(
        {"model": config.model, "effort": config.effort, "guard": config.guard_model, "k": k,
         "tasks": tasks, "trials": trials},
        indent=2))
    print(f"\nWrote {RESULTS / 'latest.json'} and transcripts in {RESULTS / 'transcripts'}")

    if args.gate is not None:
        regression = [r["pass_hat_k"] or 0.0 for r in tasks.values() if r["suite"] == "regression"]
        score = sum(regression) / len(regression) if regression else 1.0
        print(f"Regression gate: mean pass^{k} {score:.2f} (need {args.gate})")
        return 0 if score >= args.gate else 1
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
