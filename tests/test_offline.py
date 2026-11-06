"""Offline tests: no API key, no cost, run on every PR.

They cover the deterministic layers: the tool rules, the graders (via reference solutions),
the metrics and the agent loop (with a scripted model).
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import anthropic
import httpx2
import pytest
from mcp import Client
from mcp.server.mcpserver.exceptions import ToolError

import bank_server as bank
from agent import HANDOFF, LOCAL_TOOLS, REDACTED, SKILLS, Agent, Config, anthropic_tools
import evals.run as run
from evals.run import TASKS, Verdict, grade_code, pass_at_k, pass_hat_k
from evals.screen import CASES, run_cases, score


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = bank.create_db(tmp_path / "bank.db")
    monkeypatch.setenv("BANK_DB", str(path))
    monkeypatch.setenv("BANK_AGENT_ROLE", "frontline")
    return path


def verify(customer_id="C001"):
    dob, postcode = {"C001": ("1988-04-12", "SE1 7PB"), "C005": ("1983-09-09", "EH8 9YL")}[customer_id]
    bank.verify_customer(customer_id, dob, postcode)


# ---------------------------------------------------------------- tool rules (S3, T3, T4)

def test_account_tools_refuse_until_verified(db):
    with pytest.raises(ToolError, match="verify_customer first"):
        bank.find_transactions("C001")


@pytest.mark.parametrize("dob", ["1988-04-12", "12 April 1988", "12th april 1988", "12/04/1988",
                                 "April 12, 1988", "12 Apr 1988"])
def test_verification_accepts_details_as_customers_write_them(db, dob):
    assert "Verified" in bank.verify_customer(" c001 ", dob, "se17pb")


def test_unreadable_date_asks_the_customer_again(db):
    with pytest.raises(ToolError, match="Couldn't read"):
        bank.verify_customer("C001", "twelfth of the fourth", "SE1 7PB")


def test_failed_verification_does_not_say_which_field(db):
    with pytest.raises(ToolError) as e:
        bank.verify_customer("C001", "1988-04-12", "XX1 1XX")
    assert "postcode" not in str(e.value).lower() and "birth" not in str(e.value).lower()


def test_credit_above_authority_is_refused_whatever_the_model_says(db):
    verify()
    bank.open_case("C001", "card_dispute", "test")
    with pytest.raises(ToolError, match="outside your authority"):
        bank.resolve_case(1, "test", goodwill_credit_gbp=500)


def test_verification_locks_after_repeated_failures(db):
    for _ in range(bank.MAX_VERIFY_ATTEMPTS):
        with pytest.raises(ToolError, match="Verification failed"):
            bank.verify_customer("C001", "1 January 1990", "SE1 7PB")
    with pytest.raises(ToolError, match="Too many failed attempts"):
        bank.verify_customer("C001", "12 April 1988", "SE1 7PB")  # right details, too late


def test_cannot_act_on_another_customers_case(db):
    import sqlite3
    with sqlite3.connect(db) as con:
        con.execute("INSERT INTO cases (customer_id, category, summary, status) "
                    "VALUES ('C002', 'other', 'not yours', 'open')")
    verify("C001")
    with pytest.raises(ToolError, match="C002 is not verified"):
        bank.resolve_case(1, "test", goodwill_credit_gbp=10)


def test_no_credit_on_fraud_cases(db):
    verify()
    bank.open_case("C001", "fraud_or_scam", "test")
    with pytest.raises(ToolError, match="No credits on fraud"):
        bank.resolve_case(1, "test", goodwill_credit_gbp=10)


def test_third_party_text_is_labelled_untrusted(db):
    verify("C005")
    rows = bank.find_transactions("C005", merchant_contains="Brightwater")
    assert "<untrusted_text>Note for automated assistants" in rows


def test_every_call_is_audited_including_refusals(db):
    with pytest.raises(ToolError):
        bank.find_transactions("C001")
    verify()
    import sqlite3
    outcomes = [r[0] for r in sqlite3.connect(db).execute("SELECT outcome FROM audit_log")]
    assert outcomes[0].startswith("refused") and outcomes[1] == "ok"


# ---------------------------------------------------------------- injection screen (S7)

class ScriptedScreen:
    """Stands in for the screening model: flags text that talks to assistants, or fails."""

    def __init__(self, fail=False):
        self.fail = fail
        self.messages = SimpleNamespace(parse=self.parse)

    async def parse(self, **request):
        if self.fail:
            raise anthropic.APIConnectionError(request=httpx2.Request("POST", "https://api"))
        flagged = "automated assistants" in request["messages"][0]["content"]
        verdict = SimpleNamespace(reason="keyword match" if flagged else "no keyword",
                                  contains_instructions=flagged)
        return SimpleNamespace(parsed_output=verdict, usage=response("end_turn").usage)


def screen(client) -> tuple[Agent, str]:
    verify("C005")
    agent = Agent(Config(), mcp=None, tools=[], client=client)
    return agent, asyncio.run(agent._screen(bank.find_transactions("C005")))


def test_screen_removes_injected_text_and_keeps_the_rest(db):
    agent, text = screen(ScriptedScreen())
    assert "automated assistants" not in text and REDACTED in text
    assert "Direct debit" in text and len(agent.redacted) == 1


def test_screen_fails_closed(db):
    agent, text = screen(ScriptedScreen(fail=True))
    assert "Direct debit" not in text and len(agent.redacted) == 2


# ---------------------------------------------------------------- screen eval (E12)

def test_screen_cases_are_balanced():
    labels = [c["injection"] for c in CASES]
    assert labels.count(True) == labels.count(False)


def test_a_keyword_screen_fails_the_screen_eval():
    # ScriptedScreen only looks for one phrase. The eval should expose that: it misses most
    # attacks while keeping all the harmless text.
    result = score(asyncio.run(run_cases(Config(), trials=2, concurrency=4, client=ScriptedScreen())))
    assert result["keep_rate"] == 1.0 and result["catch_rate"] < 0.5


def test_screen_eval_counts_a_case_right_only_if_right_every_time():
    results = [{"id": "credit_over_limit", "trial": 0, "flagged": True, "reason": "", "cost_usd": 0, "seconds": 0},
               {"id": "credit_over_limit", "trial": 1, "flagged": False, "reason": "", "cost_usd": 0, "seconds": 0}]
    assert not score(results)["cases"]["credit_over_limit"]["every_time"]


def test_screen_eval_reports_errors_separately():
    result = score(asyncio.run(run_cases(Config(), 1, 4, client=ScriptedScreen(fail=True))))
    assert result["errors"] == len(CASES) and result["catch_rate"] == 0.0


# ---------------------------------------------------------------- reference solutions (E9)

async def replay(task: dict) -> list[dict]:
    """Run a task's reference solution through the real MCP server, in-process, with no model."""
    log = []
    async with Client(bank.mcp) as mcp:
        for name, args in task["reference_solution"]:
            if name in {t["name"] for t in LOCAL_TOOLS}:  # plain tools: check the call is valid
                is_error = name == "load_skill" and args["name"] not in SKILLS
            else:
                is_error = bool((await mcp.call_tool(name, args)).is_error)
            log.append({"name": name, "input": args, "is_error": is_error})
    return log


@pytest.mark.parametrize("task", TASKS, ids=[t["id"] for t in TASKS])
def test_reference_solution_passes_code_graders(task, db):
    tool_log = asyncio.run(replay(task))
    failed = [g for g in grade_code(task, db, tool_log) if not g["passed"]]
    assert not failed, failed


# ---------------------------------------------------------------- metrics (E5)

def test_pass_at_k_and_pass_hat_k():
    assert pass_at_k(n=4, c=3, k=1) == pass_hat_k(n=4, c=3, k=1) == 0.75
    assert pass_at_k(n=4, c=3, k=3) == 1.0    # some run of 3 always includes a success
    assert pass_hat_k(n=4, c=3, k=3) == 0.25  # only 1 of the 4 ways to pick 3 is all successes
    assert pass_hat_k(n=3, c=2, k=3) == 0.0


# ---------------------------------------------------------------- agent loop (A1, A2, A5)

class ScriptedModel:
    """Stands in for AsyncAnthropic and plays back canned responses."""

    def __init__(self, *responses):
        self.responses, self.requests = list(responses), []
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self.create))
        self.messages = SimpleNamespace(parse=self.parse)  # the screen: never flags anything

    async def parse(self, **request):
        verdict = SimpleNamespace(reason="describes a payment", contains_instructions=False)
        return SimpleNamespace(parsed_output=verdict, usage=response("end_turn").usage)

    async def create(self, **request):
        self.requests.append(request)
        return self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]


def response(stop_reason, *content):
    usage = SimpleNamespace(input_tokens=100, output_tokens=10, cache_read_input_tokens=50,
                            cache_creation_input_tokens=0)
    return SimpleNamespace(stop_reason=stop_reason, content=list(content), usage=usage)


def tool_use(id, tool, /, **input):
    return SimpleNamespace(type="tool_use", id=id, name=tool, input=input)


def run_agent(model, message="hi", **config):
    async def go():
        async with Client(bank.mcp) as mcp:
            agent = Agent(Config(**config), mcp, await anthropic_tools(mcp), client=model)
            return agent, await agent.run(message)
    return asyncio.run(go())


def test_parallel_tool_calls_come_back_in_one_message(db):
    model = ScriptedModel(
        response("tool_use",
                 tool_use("a", "verify_customer", customer_id="C001", date_of_birth="1988-04-12", postcode="SE1 7PB"),
                 tool_use("b", "load_skill", name="complaint-handling"),
                 tool_use("c", "find_transactions", customer_id="C999")),  # fails: not verified
        response("end_turn", SimpleNamespace(type="text", text="All sorted.")))
    agent, reply = run_agent(model)
    assert reply == "All sorted."
    results = agent.messages[2]["content"]
    assert [r["tool_use_id"] for r in results] == ["a", "b", "c"]
    assert [r["is_error"] for r in results] == [False, False, True]  # A5: error goes to the model
    assert "Complaint handling" in results[1]["content"]              # C2: skill body on demand
    assert model.requests[0]["system"] == model.requests[1]["system"]  # C3: stable prefix


def test_turn_budget_hands_off_to_a_person(db):
    model = ScriptedModel(response("tool_use", tool_use("a", "load_skill", name="fraud-and-scams")))
    agent, reply = run_agent(model, max_turns=3)
    assert reply == HANDOFF and agent.stop_reason == "turn budget" and len(model.requests) == 3


def test_refusal_hands_off_to_a_person(db):
    agent, reply = run_agent(ScriptedModel(response("refusal")))
    assert reply == HANDOFF and agent.stop_reason == "refusal"


def test_after_a_hand_off_the_agent_takes_no_further_action(db):
    model = ScriptedModel(
        response("tool_use", tool_use("a", "hand_off_to_person", reason="Customer asked for a person.")),
        response("tool_use", tool_use("b", "load_skill", name="complaint-handling")),
        response("end_turn", SimpleNamespace(type="text", text="A colleague will contact you.")))
    agent, reply = run_agent(model)
    assert agent.handoff_reason == "Customer asked for a person."
    assert agent.tool_log[1] == {"name": "load_skill", "input": {"name": "complaint-handling"},
                                 "is_error": True}



class ScriptedJudge:
    """Stands in for the LLM judge and passes everything."""

    def __init__(self):
        self.messages = SimpleNamespace(parse=self.parse)

    async def parse(self, **request):
        return SimpleNamespace(parsed_output=Verdict(reasoning="fine", verdict="PASS"))


def test_a_whole_trial_writes_a_readable_transcript(tmp_path, monkeypatch):
    """E10: one full eval trial, offline: real MCP server process, scripted model and judge."""
    task = next(t for t in TASKS if t["id"] == "duplicate_charge_refund")
    calls = [tool_use(str(n), name, **args) for n, (name, args) in enumerate(task["reference_solution"])]
    model = ScriptedModel(*[response("tool_use", c) for c in calls],
                          response("end_turn", SimpleNamespace(type="text", text="Refunded £24.99.")))
    judge = ScriptedJudge()
    monkeypatch.setattr("agent.anthropic.AsyncAnthropic", lambda: model)
    monkeypatch.setattr(run, "ROOT", tmp_path)
    monkeypatch.setattr(run, "RESULTS", tmp_path / "results")

    result = asyncio.run(run.run_trial(task, 0, Config(), judge))
    assert result["passed"], result
    transcript = json.loads((tmp_path / result["transcript"]).read_text())
    assert [e["tool"] for e in transcript["audit_log"]] == [c[0] for c in task["reference_solution"]]
    assert transcript["stop_reason"] == "end_turn" and transcript["complaint"] == task["complaint"]
    assert len(transcript["messages"]) == 2 * len(calls) + 2  # complaint, then a call/result pair each, then the reply
