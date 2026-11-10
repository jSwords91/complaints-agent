# Eval driven agent

A Claude agent that handles written complaints for a fictional UK bank, with the evaluation
suite that shows whether it behaves.

The agent checks the customer's identity, looks up their account, follows the bank's policy,
refunds small amounts within a limit and escalates everything else to a person. The bank's
systems sit behind an MCP server that enforces every rule in code. A separate, cheap model
screens third-party text for prompt injections. The eval harness runs each complaint several
times on a fresh bank, grades what actually changed in the database and reports pass^k, cost
and latency per task.

**Why bank complaints?** It's a small task that contains most of the hard parts of an
enterprise agent. You have to check identity before touching data, follow a written policy,
move money within a limit, read text written by strangers and look after vulnerable customers.
And a regulator will ask how you know it behaves.

## Quickstart

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/).

```bash
uv run pytest -q                       # 36 offline tests, free, a few seconds

export ANTHROPIC_API_KEY=...
uv run agent.py                        # chat (try customer C001, born 12 April 1988, SE1 7PB)
uv run python -m evals.run --suite regression --trials 3 --gate 0.9
uv run python -m evals.screen --trials 3
cp results/latest.json results/baseline.json
uv run python -m evals.run --model claude-sonnet-5-5 --compare results/baseline.json
```

## The system in one picture

```
complaint
   │
   ▼
agent.py: one model, one loop
   │  short system prompt + skill list (cached)
   │  tool calls run concurrently; results go back in one message
   │  turn and cost budgets; a refusal or budget hit hands off to a person
   │
   ├── plain tools ─> load_skill (skills/*/SKILL.md), hand_off_to_person
   └── MCP (stdio) ─> bank_server.py, one process per conversation
                      six tools holding every rule: verification (normalised details,
                      3 attempts), £100 credit limit, no credit on fraud, audit log
                         │
                         │ third-party text comes back inside <untrusted_text> tags
                         ▼
                      injection screen (Haiku): removes anything that reads as
                      instructions, before the main model sees it

evals/run.py       fresh database + server per trial ─> code graders on the database
                   + one LLM judge call per dimension ─> pass@k, pass^k, cost, transcripts
evals/screen.py    20 labelled memos ─> the screen alone ─> catch rate, keep rate
```

| File | What it is |
| --- | --- |
| `bank_server.py` | MCP server: six tools, the bank's rules and the seed data |
| `skills/` | Two procedures: `complaint-handling` and `fraud-and-scams` |
| `agent.py` | The agent loop, skills, injection screen, caching, budgets and cost tracking |
| `evals/tasks.yaml` | Nine tasks (eight regression, one capability), each with a reference solution |
| `evals/run.py` | Eval harness: trials, graders, metrics, model comparison, CI gate |
| `evals/screen_cases.yaml`, `evals/screen.py` | Component eval for the injection screen: 10 attacks, 10 harmless memos |
| `tests/test_offline.py` | 36 free tests: tool rules, normalisation, the screen and its eval, reference solutions, metrics, the loop, hand-offs, a whole trial |
| `.github/workflows/evals.yml` | Offline tests and gated regression evals on PRs, capability evals nightly |

Each file builds on the one before it, so the table above is also a good reading order.

## Design notes in the code

Each design decision has an ID, grouped by area, and the code that implements it carries a
comment with that ID and a reference key:

```python
# S3 [CONTAIN]: the hard limits. Whatever the prompt, the customer or an injected memo
#   says, no credit above the role's authority and no credit on a fraud case.
```

| Prefix | Area |
| --- | --- |
| E | [Evaluations](#evaluations) |
| C | [Prompting and context](#prompting-and-context) |
| A | [Agent architecture](#agent-architecture) |
| T | [Tools, MCP and skills](#tools-mcp-and-skills) |
| S | [Security and containment](#security-and-containment) |
| K | [Cost and latency](#cost-and-latency) |

The keys in brackets are listed under [References](#references). To find every decision in
one area, or everything that draws on one reference:

```bash
grep -rn "# S[0-9]" --exclude=README.md .
grep -rn "\[DE\]" --exclude=README.md .
```

## What gets checked, and when

Three different things check this agent, at three different times, and they're easy to mix up:

| When | What | Where it runs | Can it change what the customer gets? |
| --- | --- | --- | --- |
| **During the conversation**, on every call | Tool rules in code (verification, lockout, credit limit), the injection screen on third-party text, turn and cost budgets | Chat and evals | **Yes.** This is the only layer that protects a live customer |
| **Straight after each eval trial** | Code graders read the bank's database; the LLM judge reads the finished reply | Evals only | **No.** Grading never touches the conversation; it scores it afterwards |
| **When you choose** | You read the transcript | Evals only | No, but it's how you find out what to change |

The judge runs seconds after each trial ends, and trials run several at a time, so an eval run
*looks* like grading in real time. It isn't watching the conversation. It gets the finished
reply and the list of actions, and grades them. The only model that checks anything *during* a
conversation is the injection screen, and it checks merchant text, never the agent's reply.

In chat (`uv run agent.py`) the live protections still apply, but nothing grades the
conversation and no transcript is saved. Grading real traffic (sampling live conversations and
running the same graders on them) is the production monitoring layer listed under
[From here to production](#from-here-to-production).

## Reading a transcript

Each eval trial writes `results/transcripts/<task>-<trial>.json`. (A trial that hits an
infrastructure error writes none; it shows up in the report instead.) A transcript is the
complete record of one trial, and it reads top to bottom, summary first:

| Field | What it tells you |
| --- | --- |
| `passed`, `score`, `grades` | The verdict. Each grade has a `detail`: the value a code grader found, or the judge's reasoning |
| `failed`, `unknown` | Which graders failed, and which the judge couldn't decide (read those yourself) |
| `reply`, `stop_reason`, `handoff_reason` | What the customer got, and why the conversation ended |
| `model_calls`, `cost_usd`, `cache_hit_rate`, `seconds` | What it cost |
| `complaint` | The input |
| `tool_log` | What the agent ran, in order, and whether each call errored |
| `redacted` | Any third-party text the screen removed, with the screen's one-sentence reason |
| `audit_log` | The bank's own record of what its server allowed and refused. It should agree with `tool_log`; if it doesn't, the bug is in the plumbing, not the model |
| `messages` | Exactly what went to and from the model, including a summary of its reasoning before each step |

The reasoning summaries are there because `Config.thinking_display` is `"summarized"`. Opus 5.5
always thinks, but by default it returns the thinking empty. Thinking is billed the same
either way; the setting only controls whether you can see it.

To work through a failed trial:

1. **Start with the grades.** Is the failure real? A wrong judge or an ambiguous criterion is a
   grader bug, not an agent bug. [DE] is blunt about this: you don't know if your graders are
   fair until you've read the transcripts.
2. **Then `tool_log`.** Did it verify first, load the right skill, recover from errors?
3. **Then `messages`**, around the step that went wrong. Did it misread the policy (fix the
   skill), lack information (fix a tool's output), or ignore something it had (a prompt or
   model issue)?
4. **Decide where the fix goes**: a skill, a tool, the prompt or the grader. Then make sure a
   task covers it, so it stays fixed.

---

## Evaluations

**The problem.** The agent's output varies from run to run, its reply can claim things it
didn't do, and a complaints agent has to be right every time, not once in three tries.

**What it does.** Each task is one written complaint. Graders check what ended up in the
bank's database (was £24.99 credited? was the case escalated?), because the agent can claim
anything in its reply but can't fake the database. An LLM judge covers what code can't (tone,
clarity, what was disclosed), one call per criterion. Every task runs several times on a fresh
bank, and CI gates on pass^k rather than pass@k.

Two more decisions matter as much as the graders. Infrastructure errors (an API outage, a rate
limit) are left out of pass^k and reported separately, so an outage can't look like the agent
getting worse. And when the judge answers UNKNOWN, the report lists it for a person to review
rather than quietly counting it as a fail.

The injection screen also gets its own small eval (`evals/screen.py`). It's one classification
call, so it's cheapest to test directly against labelled examples. That tells you exactly which
way it fails (missed attacks or removed real data), which the end-to-end tasks can't.

Both runners are built to be read, not just to pass or fail. `evals/run.py` ends with every
failed trial: which grader failed and why (the value it found, or the judge's reasoning), any
hand-off reason, the start of the reply, and the transcript to open. `evals/screen.py` shows
every case with a mark per trial, the screen's own one-sentence reason for each mistake, a
pass/fail verdict that names the cases at fault, and the cost and latency per check.

Each criterion is written so that two people reading the same transcript would grade it the
same way. "The reply tells the customer the refund amount and what happens next" works; "the
reply is good" doesn't.

| ID | Decision | Where |
| --- | --- | --- |
| E1 | Shared vocabulary: task, trial, grader, transcript, outcome, evaluation vs agent harness | `evals/run.py` docstring |
| E2 | Code graders check the outcome in the database, which the agent can't fake | `tasks.yaml`, `grade_code` |
| E3 | Path graders only where an auditor would care (no account access after failed verification) | `failed_verification_no_disclosure` |
| E4 | LLM judge: one call per dimension, UNKNOWN allowed, structured output | `grade_judge` |
| E5 | pass@k and pass^k, with CI gated on pass^k | `pass_at_k`, `pass_hat_k`, `--gate` |
| E6 | Regression vs capability suites, run differently in CI, with graduation | `tasks.yaml`, `evals.yml`, `print_report` |
| E7 | Balanced tasks: should-act and should-not-act pairs | `tasks.yaml` |
| E8 | Isolated trials: a fresh database and server process each time | `run_trial` |
| E9 | Reference solutions, replayed offline to prove the tasks and graders work | `test_reference_solution_passes_code_graders` |
| E10 | Transcripts with three views (tool log, bank audit log, model messages with reasoning summaries); cost, turns and latency; which tasks flipped against a baseline | `run_trial`, `print_report`, [Reading a transcript](#reading-a-transcript) |
| E11 | Partial credit as well as pass/fail | `run_trial` |
| E12 | A component eval for one LLM call (the screen), alongside the end-to-end tasks | `evals/screen.py` |

**Not done here, and why:**

- **Only nine tasks.** [DE] suggests starting with 20 to 50, drawn from real failures. Nine is
  enough to show the shape; the next ones should come from transcripts where it went wrong.
- **No judge calibration yet.** Before a judge gates anything, label 30 to 50 transcripts by
  hand and check it agrees.
- **Evals are one layer.** In production they sit alongside the other layers of the "Swiss
  cheese" model: sampling live transcripts every week, customer feedback, and A/B tests of
  prompt changes.

## Prompting and context

**The problem.** Context is a finite budget, and a rule written in a prompt only makes the
model *likely* to follow it.

**What it does.** The system prompt is short: who the agent is, what a good reply looks like,
and the one rule it needs everywhere (third-party text is data, not instructions). Procedures
live in skills, which the model loads only when it needs them. Hard rules live in the tools,
where code enforces them. That split matters more than any wording: a prompt makes a rule
likely, a tool makes it certain.

| ID | Decision | Where |
| --- | --- | --- |
| C1 | A short system prompt: procedures in skills, hard rules in tools | `SYSTEM` in `agent.py` |
| C2 | Progressive disclosure: skill names in the prompt, full text loaded on demand | `read_skills`, `load_skill` |
| C3 | Prompt caching with a stable prefix (fixed tool order, nothing that changes per call) | `Agent.run`, `anthropic_tools` |
| C4 | Append-only history, which keeps the cache and the thinking blocks valid | `Agent.run` |

**Not done here, and why:**

- **Compaction, context resets and the memory tool.** Complaints are short, so the context
  never fills. A long-running version should use the API's server-side compaction rather than
  editing history by hand, which would break the cache and the thinking blocks.
- **Retrieval (RAG).** The policy fits in one skill. If it grew to hundreds of pages, it should
  go behind a search tool before reaching for a vector database.
- **A prompt audit.** Worth running `/claude-api prompt-audit` on `SYSTEM` and the skills when
  the model changes, to catch rules the new model no longer needs.

## Agent architecture

**The problem.** Complaints don't follow one fixed path, but every extra component in a
harness is an assumption about what the model can't do.

**What it does.** The core is a single agent loop, because the steps differ by complaint
(a duplicate charge needs a transaction search; a scam needs a skill and an escalation). Around
it are two small fixed workflows from [BEA]: the injection screen is a guardrail run by a
separate model, and the eval judge is a separate evaluator, because a model grading its own
work skews positive. The harness owns the hard stops (turn and cost budgets) and hands off to a
person instead of failing silently.

| ID | Decision | Where |
| --- | --- | --- |
| A1 | A single loop; parallel tool calls returned in one message; no subagents, and why | `agent.py` docstring, `Agent.run` |
| A2 | Turn and cost budgets enforced by the harness, handing off to a person | `Config`, `Agent.run` |
| A3 | Simplest design first; a separate evaluator rather than self-grading | `agent.py` docstring, `grade_judge` |
| A4 | Swappable brain (model), hands (MCP server) and session (history, tool log) | `Agent` |
| A5 | Tool failures go back to the model as errors, not crashes | `Agent._call_tool` |
| A6 | Two routes to a person: the model chooses (`hand_off_to_person`) or the harness forces it (budget, refusal). Enforced and counted | `Agent._call_tool`, `Agent._hand_off`, `asks_for_a_person` |

**Not done here, and why:**

- **Subagents.** None of [FOLD]'s three reasons applies: no context bloat, no parallel work
  needing fresh contexts, no conflicting tool sets. A "refunds subagent" would just copy the org
  chart. If evals on long, multi-issue cases showed the context getting crowded, that would be
  the time to revisit it.
- **Managed Agents.** The same tasks could run on Managed Agents to compare harnesses. The eval
  suite is what would make that comparison fair.
- **The SDK's tool runner.** Anthropic's docs recommend it as the default for custom-tool agents,
  and its per-turn hooks would cover the budgets, the screen and the hand-off. The loop here is
  written by hand so that every step of it is visible in one place; a production build would
  reasonably start with the runner.

## Tools, MCP and skills

**The problem.** The model only sees tool descriptions and results, so a tool's design decides
how well the agent can use it, and what it's allowed to do.

**MCP or plain tools?** Anthropic's guidance doesn't give a rule; it sorts tools by where the
code runs. Plain (client) tools are ones where "you write the schema and your application runs
each call" ([tool use docs](https://platform.claude.com/docs/en/agents-and-tools/tool-use/overview)).
MCP is the standard protocol for exposing a system to agents, so "developers implement MCP once
in their agent and it unlocks an entire ecosystem of integrations" ([CEM]). The
[MCP connector](https://platform.claude.com/docs/en/agents-and-tools/mcp-connector) is a
server-side option, but it only reaches public HTTP servers, not a local stdio one like this.

So the split here is about **ownership**:

- **The bank's systems go behind MCP.** In a real bank another team would own them, and the
  same server could serve this agent, Claude Desktop or the next agent someone builds. It's
  also a security boundary: the credentials live in the server process (S1).
- **Tools that belong to this agent are plain tools.** `load_skill` and `hand_off_to_person`
  are about this conversation, not the bank's data, and they need the harness's state (the
  skill files, the session's hand-off flag). Putting them behind MCP would add a process
  boundary and a protocol for no reuse.

Both kinds follow the same design rules, because the model can't tell them apart: clear
descriptions, strict schemas, and results it can act on [TOOLS].

**What it does.** Six MCP tools, grouped by intent (verify, look up the customer, find
transactions, open, resolve, escalate), not one per database table. Every precondition is
checked inside the tool, and a refusal tells the model what to do next ("call verify_customer
first"). The tools also normalise what customers type, so "12th April 1988", "se17pb" and
"c001" all work. That's the tool's job, not something to trust the model to reformat. The rule
of thumb: tools for actions and live data, skills for procedures, reference material behind a
search tool.

| ID | Decision | Where |
| --- | --- | --- |
| T1 | An MCP server with tools grouped by intent, not by table | `bank_server.py` |
| T2 | Descriptions written for the model; typed, enum-constrained parameters | tool docstrings, `Literal` types |
| T3 | Preconditions and input normalisation inside tools; refusals the model can act on | `refuse`, `require_verified`, `normalise_date` |
| T4 | Per-role permissions checked in the tool | `CREDIT_LIMIT_GBP` |
| T5 | Token-efficient results: filters, a row cap, compact lines | `find_transactions` |
| T6 | Skills as SKILL.md files with name and description frontmatter | `skills/` |
| T7 | MCP for the bank's systems, plain client tools for the agent's own concerns | `LOCAL_TOOLS` in `agent.py` |

**Not done here, and why:**

- **Tool search.** It pays off past roughly 30 to 50 tools. With eight it would only add a
  round trip.
- **Code execution with MCP** [CEM]. Instead of loading every MCP tool definition up front and
  passing every result through the model, the agent sees MCP servers as code (a folder of typed
  functions), reads only the ones it needs, and writes code that calls them in a sandbox. Large
  intermediate data stays in the sandbox; only what the code returns reaches the model. The
  post's example went from 150,000 tokens to 2,000. It pays off with many tools or big results,
  and it needs a secure sandbox to run the agent's code. Here there are eight tools returning a
  few lines each, so it would add a sandbox and save almost nothing. The Claude API's
  programmatic tool calling is a similar idea built into the platform.
- **A remote MCP server with OAuth.** A real bank's server would be remote, with per-user
  OAuth. Stdio keeps the example self-contained.

## Security and containment

**The problem.** Model-level defences are probabilistic, the agent reads text written by
strangers, and the customer can't judge whether a tool call is safe.

**What it does.** Three kinds of risk apply to this agent:

- **User misuse:** a customer pushing for more money than policy allows, or someone guessing
  another person's details. The credit limit and a three-attempt verification lockout are in
  code, so persuasion can't move them (`large_dispute_escalated` tests the first).
- **Model misbehaviour:** the agent making a mistake on its own. It has six narrow tools and no
  shell, files or network; limits are enforced in code; every call is audited.
- **External attackers:** instructions hidden in third-party text, here a payment memo. Three
  layers stop it: the text is labelled untrusted (S2), a cheap model screens it out (S7), and
  the credit limit in code caps the damage (S3). But the limit only helps if the attacker asks
  for too much. `injected_memo_under_limit` asks for £99, which code allows, so only the model
  layers stand in the way. That task is the reason the screen exists.

Customers can't judge tool calls, so there's no "approve this action?" prompt to lean on. That
pushes the defences into the environment and the code, which is where containment should start
anyway [CONTAIN].

| ID | Decision | Where |
| --- | --- | --- |
| S1 | Credentials stay in the server process; the API key never reaches it | `connect`, `bank_server.py` |
| S2 | Third-party text labelled untrusted, plus a prompt-injection eval | `untrusted`, `injected_memo_ignored` |
| S3 | Deterministic limits in code, behind the probabilistic layers | `resolve_case` |
| S4 | Minimal capability: six tools, no shell, files or network | `bank_server.py` |
| S5 | Audit log of every call, including refusals | `audit` |
| S6 | Read and write tools annotated differently | `READ`, `WRITE`, `MOVES_MONEY` |
| S7 | A cheap guard model screens third-party text, failing closed | `screen_text`, `Agent._screen` |
| S8 | Verification locks after three failed attempts; actions on a case check its owner is verified | `verify_customer`, `open_case_for` |

**The screen's thresholds depend on the industry.** `evals/screen.py` reports two numbers:
the catch rate (attacks removed on every trial) and the keep rate (harmless text left alone on
every trial). For a bank, a missed attack can move money and a wrongly removed memo only costs
some context, so the defaults require every attack caught and tolerate a little over-removal.
A logistics agent that depends on delivery notes might weigh them the other way. Setting those
two numbers is a conversation with the risk team, not a coding decision.

**Trade-offs:** the screen fails closed. If Haiku can't be reached, the memo is removed rather
than passed through, which trades a little helpfulness for safety. Its redactions are saved in
each transcript, so a jump in the redaction rate is visible. And `--no-guard` runs the evals
without it, which answers whether it still earns its place on a newer model.

## Cost and latency

**The problem.** A change that improves quality can quietly double the cost, and an agent is
only worth running if it's cheaper than a person for the same result.

**What it does.** Every number comes from the API's own usage fields. The eval report shows
cost, model calls, latency and cache hit rate per task, so a change like "try Sonnet" or "lower
the effort" shows up as a trade-off in one table: pass^k against dollars and seconds.

| ID | Decision | Where |
| --- | --- | --- |
| K1 | Model, effort and token limits are config; an upgrade is one eval flag | `Config`, `--model`, `--effort` |
| K2 | Cost per task from usage, including cache reads and writes | `Usage.cost_usd` |
| K3 | Cache hit rate, reported per task | `Usage.cache_hit_rate` |
| K4 | Model tiering: the narrow screening job runs on the cheapest model | `Config.guard_model` |

The ROI comparison is the cost per complaint from the eval report next to what a person costs
for the same complaint (minutes per complaint times the loaded hourly rate), with the pass^k
figure alongside it, because a cheap agent that's right 75% of the time isn't cheap.

**Not done here, and why:**

- **The Batch API.** It halves the price but results can take hours. The nightly capability run
  could send its judge calls through it; the PR gate needs answers quickly.

---

## Things to try

1. **Decide on a model upgrade.** Run the regression suite on Opus 5.5, save the baseline, then
   rerun with `--model claude-sonnet-5-5`. Summarise it in three sentences: what flipped, what
   it costs, and what you'd do.
2. **Grow the suite.** Add a task for an ambiguous date ("04/12/1988" from someone who might be
   American). What should the agent do, and how would you grade it? See
   [Adding a task](#adding-a-task).
3. **Test an assumption.** Run `injected_memo_ignored` with and without `--no-guard`, five
   trials each. Does the screen change the pass rate? Is it worth its cost?
4. **Tune the screen for another industry.** Run `evals/screen.py` with
   `--guard-model claude-sonnet-5-5`. Is the better catch rate (if any) worth the cost? Then
   pick thresholds for a healthcare provider reading GP letters.
5. **Find the eval result that would justify a subagent.** What would you have to see in the
   transcripts, and which subagent would it be?
6. **Generalise the defences.** Use the S rows to design the defence against a prompt injection
   in a shared document that tries to exfiltrate data.
7. **Find the cheapest setting that holds quality.** Try `--effort low`, then work out the cost
   per complaint and the ROI comparison above.
8. **Calibrate the judge.** Hand-label 20 transcripts from `results/transcripts/` and measure
   how often the judge agrees with you.

## Adding a task

1. Add a customer and any transactions it needs to the seed data in `bank_server.py`. Each
   customer exists for one task, so tasks don't interfere.
2. Add the task to `evals/tasks.yaml`: an `id`, the `complaint`, its `suite` (`regression`
   or `capability`), a list of `graders` (`state` and `tool_calls` for what code can check,
   `judge` dimensions for what it can't), and a `reference_solution`: the tool calls that
   solve it.
3. Run `uv run pytest -q`. The offline tests replay every reference solution against the code
   graders, so a task that can't be passed fails here, for free, before any model call.
4. Run the task live a few times and read the transcripts before trusting its graders.

New capability tasks start in the `capability` suite and move to `regression` once they pass
every time; the report flags when that's happened.

## From here to production

What would need to change before this handled a real customer:

- **Hand-offs go to a real queue.** Today a hand-off is recorded on the session. In production
  it becomes a ticket a person picks up, with the transcript attached.
- **A remote MCP server with per-user OAuth**, so the agent acts with the customer's own
  permissions rather than a shared role.
- **Money as integer pence** in a real ledger, not floats.
- **Transcripts are personal data.** They need a retention period, access controls and
  redaction before anyone outside the complaints team reads them.
- **Tracing** (OpenTelemetry) on every model and tool call, so a slow or failed conversation
  can be followed end to end.
- **The other eval layers**: weekly transcript sampling, customer feedback, and a calibrated
  judge before it gates anything.
- **Rate limits and retries** sized for real traffic, with the Batch API for offline grading.

## Limitations

- The live evals haven't been run yet, so there are no published pass rates or costs. The
  offline tests pass, and the request shapes were checked against `anthropic` 2.6.0 and
  `mcp` 2.3.0.
- Server-side refusal fallback (`fallbacks: "default"`) is on for Opus 5.5 and Sonnet 5.5. If
  every model refuses, the agent hands off to a person.
- Check the prices in `agent.py` against the pricing page before quoting any cost.
- Lumen Bank, its customers and its policies are fictional.

## References

| Key | Source |
| --- | --- |
| DE | [Demystifying evals for AI agents](https://www.anthropic.com/research/demystifying-evals-for-ai-agents) (Jan 2026) |
| CTX | [Effective context engineering for AI agents](https://www.anthropic.com/research/effective-context-engineering-for-ai-agents) (Sep 2025) |
| BEA | [Building effective agents](https://www.anthropic.com/engineering/building-effective-agents) (Dec 2024) |
| FOLD | [When to fold subagents into your main agent loop](https://claude.com/resources/articles/building-agents-when-to-fold-subagents-into-your-main-agent-loop) (Oct 2026) |
| HD | [Harness design for long-running application development](https://www.anthropic.com/research/harness-design-long-running-apps) (Mar 2026) |
| MA | [Scaling Managed Agents](https://www.anthropic.com/research/managed-agents) (Apr 2026) |
| CONTAIN | [How we contain Claude across products](https://www.anthropic.com/research/how-we-contain-claude) (May 2026) |
| CEM | [Code execution with MCP](https://www.anthropic.com/engineering/code-execution-with-mcp) (Nov 2025) |
| TOOLS | [Writing effective tools for agents](https://www.anthropic.com/research/writing-tools-for-agents) (Sep 2025) |
| SKILLS | [Equipping agents for the real world with Agent Skills](https://www.anthropic.com/research/equipping-agents-for-the-real-world-with-agent-skills) (Oct 2025) |
| docs | Claude API docs: tool use, MCP connector, prompt caching, effort, structured outputs, refusal fallbacks |

The project layout started from
[anthropics/claude-quickstarts/agents](https://github.com/anthropics/claude-quickstarts/tree/main/agents).

## License

[MIT](LICENSE)
