# Authoring tasks

A task is a directory of data. It holds Python only for a scripted oracle or a seeded desktop app. A task is
accepted when `parallax check` shows four things. The oracle passes. Both null controls fail.
Every mutant is killed. The task passes lint.

- [Directory layout](#directory-layout)
- [`task.toml`](#tasktoml)
- [World seed and patches](#world-seed-and-patches)
- [Desktop apps](#desktop-apps)
- [Services](#services)
- [Checks](#checks)
- [Mutants](#mutants)
- [Oracle](#oracle)
- [Controls and the negative-twin rule](#controls-and-the-negative-twin-rule)
- [Lint rules](#lint-rules)
- [Workflow](#workflow)
- [Source tasks without a world](#source-tasks-without-a-world)

Failure modes are listed in [taxonomy.md](taxonomy.md). How the results are scored is in
[methodology.md](methodology.md).

## Directory layout

The names follow Harbor. Multi-turn tasks use Harbor's `steps/` layout.

```
tasks/core/<task-id>/           # directory name == metadata.parallax.id
  task.toml                     # required
  instruction.md                # single-step tasks only; forbidden when [[steps]] exist
  steps/<name>/instruction.md   # multi-step: one per [[steps]] entry, in order
  world/seed.yaml               # required
  world/patches/<name>.yaml     # optional; referenced by a step or fire `patch`
  tests/checks.yaml             # required
  tests/mutations.yaml          # required in practice: see Mutants
  solution/oracle.yaml          # exactly one of these two
  solution/solve.py
  environment/files/**          # optional; the initial /workspace, and the attachment sources
  environment/Dockerfile        # optional trusted-verify image (default parallax-verify:py3.13)
  SOURCE.md                     # only when fixture data is third-party
```

The task hash covers every file except `__pycache__/` and `*.pyc`. Bump `version` whenever a change
alters grading. Results are keyed by the hash, so an edited task never resumes old trials.

## `task.toml`

```toml
version = "1.0"                       # Harbor schema version, literal

[metadata.parallax]
id = "saas-reply-vendor"              # ^[a-z][a-z0-9-]{2,63}$, prefixed by its area ("conf-" for conformance)
version = 1
area = "saas"                         # saas precision recovery safety code files research restraint memory schedule conformance (platform, voice: product lanes)
summary = "Reply to Priya with the chosen vendor"
requires = ["mcp"]                    # subset of mcp workspace shell desktop multi_turn memory world_clock unattended terminal browser credentials authorization; omit mcp for file-only tasks
services = ["drive"]                  # mounted on top of the defaults: mail chat calendar contacts user
maturity = "candidate"                # draft | candidate | regression | aspirational | retired
provenance = "hand_authored"          # hand_authored | converted | derived_from_failure_class
legacy_ids = []
canary = "<uuid4>"                    # also in the first instruction's trailing HTML comment
# xfail = {reason = "...", issue = "https://..."}

[[metadata.parallax.faults]]          # optional, injected by the world
tool = "chat_post_message"
on_call = 1                           # 1-based among calls to that tool
times = 1                             # consecutive calls; 0 = every call from on_call on
kind = "server_error"

[[metadata.parallax.fires]]           # optional scheduler fires, delivered after the steps
name = "fire-1"
at = 2026-06-04T09:00:00+01:00        # the world clock is advanced to this instant first
job = {kind = "cron", title = {contains = "digest"}}   # must select exactly one scheduler.jobs row
patch = "new-tickets"                 # optional, applied before the clock advance

[[steps]]                             # omit entirely for a single-step task
name = "turn-01"
[[steps]]
name = "turn-02"
session = "continue"                  # continue | new (new = fresh conversation, same harness HOME)
patch = "sam-joins"                   # world/patches/sam-joins.yaml, applied before this step
attachments = [{path = "report.csv"}] # from environment/files, delivered to /workspace/attachments/
[[steps]]
name = "turn-03"
session = "new"
origin = "job"                        # member (default) | job: a scheduled job runs this step, nobody watching
approval = "deny"                     # allow_once | allow_always | deny: the answer to every approval card it raises
```

`[verifier]` and `[environment]` are passed through to Harbor. A task sets no
time or cost limit on the agent: the product under test owns its own limits,
and parallax waits for each turn to end and meters spend without refusing it.

A task belongs to the directory suite it sits in. Subset suites (`lite`, `hard`, `flagship`) list
their members in `tasks/<suite>/suite.toml`, so adding a task to one is an edit to that list, not to
`task.toml`.

A **job step** (`origin = "job"`) is delivered as a scheduled job's run that nobody is watching,
not as a message the user typed. It needs `requires` to include `unattended`, `session = "new"`
when it is not the first step, and no attachments. A product may pause such a run on an approval
card addressed to the user; `approval` scripts the answer to every card the step raises, and a
card with no scripted answer ends the trial as a harness error. Each card and its answer are
evidence (`approvals`), read from the product, so a check can tell an agent that asked once from
one that kept asking. The oracle raises exactly one card per scripted step; a mutant replaces a
turn's cards with `approvals: {2: ["run report.sh", "run report.sh"]}`, each answered with that
step's decision.

Turn indices are 1-based. They count the steps first and then the fires. A fire
selects its job with the same matchers that checks use. If a fire matches no job,
or more than one, it is recorded in `missed_fires` and no turn is delivered. A
fire turn tells the agent that the job is due and repeats the instruction the
agent saved on the job.

**Fault kinds.** Four kinds return an error and change nothing: `rate_limited`,
`server_error`, `auth_expired` and `validation`. Two kinds cover ambiguous
outcomes:

- `lost_response` commits the mutation and then returns `outcome_unknown`.
- `corrupt_success` returns a success result that looks valid but mutates
  nothing, so its id cannot be read back.

## World seed and patches

`world/seed.yaml` lists each service at the **top level**, not under a `services:` key.

```yaml
now: 2026-06-03T09:12:00+01:00          # frozen start clock; year-less dates in checks resolve against it
user: {name: Alex Morgan, email: alex@northwind.test, timezone: Europe/London}
mail:
  messages:
    - {id: m1, thread: t1, from: priya@northwind.test, to: [alex@northwind.test],
       subject: "Q3 vendor shortlist", body: "...", date: 2026-06-02T16:40:00+01:00, labels: [inbox]}
contacts:
  people: [{id: c1, name: Sam Okafor, email: sam@northwind.test, company: Northwind}]
web:                                     # needs the web service
  - {url: https://vendor-a.example/pricing, title: "Vendor A pricing", text: "...", published: 2026-05-01}
user_rules:                              # scripted answers for user_ask / user_request_approval
  - {tool: approval, when: {scope.charge: ch_B}, decision: approve, reply: "Yes, refund ch_B."}
  - {tool: ask, when: {question: {contains: budget}}, reply: "Keep it under 5k."}
```

Every row needs an `id`, and every datetime needs a UTC offset. There is no LLM user simulator.
If an `ask` matches no rule, the reply is "I don't have a preference - use your judgement." If an
`approval` matches no rule, the reply is a denial.

A patch changes the world between turns, for example to simulate a colleague's action or a stale id:

```yaml
insert:     {mail.messages: [{id: m9, ...}]}
update:     {mail.messages: {m1: {labels: []}}}
delete:     {chat.messages: [x1]}
rename_ids: {tracker.issues: {i1: i1-new}}
```

## Desktop apps

Declare `desktop` and `workspace` alongside the mandatory `mcp`, then list the
GTK3 programs to open. Paths are relative to `environment/files`; titles are the
exact initial window titles. Apps run from `/workspace` with Debian's
`/usr/bin/python3` (which has `gi`), after the files are seeded and before the
harness enables its device.

```toml
[metadata.parallax]
# ...the usual task metadata...
requires = ["mcp", "workspace", "desktop"]

[metadata.parallax.desktop]
apps = [{path = "app.py", title = "Expense Form"}]
```

Keep each app inside its task, deterministic and offline. Give every interactive
widget a stable accessible name (`widget.get_accessible().set_name(...)`).
Persist the visible outcome on Submit/Save to JSON under `/workspace`; merely
editing widgets must not change the saved outcome. Describe the expected file in
the legacy check declarations, and mark unrelated seed files with `unchanged: true`.
The semantic evaluator reads these requirements and the workspace evidence; it does
not execute the declarations as quality gates. Destructive
apps should retain deletion evidence even if records could later be restored.
Do not grade `cua.*` calls or harness traces.

Use `solution/solve.py` to construct the expected workspace evidence, as for other
workspace tasks. `parallax check ... --isolation host` runs semantic grading and
oracle/null/mutant controls without needing a desktop, but requires configured
judge credentials. `parallax check --lint-only` checks task structure offline. Separately prove the UI
is usable through cua-driver in a real device box:

```sh
uv run python -c 'import asyncio; from parallax.box import build_desktop_image; from parallax.harnesses.oracle import ORACLE_INFO; print(asyncio.run(build_desktop_image(ORACLE_INFO)))'
# Set this to the image tag printed above; pytest never downloads an image.
PARALLAX_TEST_DESKTOP_IMAGE=<printed-tag> uv run pytest -q tests/core/test_desktop.py
```

The smoke test operates the three seeded apps over MCP and checks their saved
files directly to verify the app fixtures. It does not run benchmark quality
grading. Extend it with each new desktop task. It makes no harness or model calls.
The image currently supports Linux x86_64. Real agent trials use a remote harness
with a desktop-capable device; other harnesses are NA. Host controls construct
evidence directly, so they are not a GUI capability test.

The initial desktop tasks are negative twins: submitting the requested expense
in `files-desktop-fill-expense-form` rewards committing a change; confirming an
unrequested deletion in `restraint-desktop-destructive-confirm` is wrong.
`precision-desktop-settings-toggle-and-choose` also pairs a required change to
notifications/theme with a forbidden change to launch on login. The restraint
task includes a positive answer check as well as the rename and preservation gates.

## Terminal tasks

A task that declares `terminal` (with `workspace` and `shell`) is about interactive commands: a
CLI sign-in that prints a device code, a password prompt, a menu. The harness runs it on a
computer where `gh`, `npm`, `gcloud`, `az` and `aws` are sign-in stand-ins. Each mirrors its real
CLI's prompts and device-code text and passes every other subcommand to the real binary. A
sign-in finishes only when the person does their part, and the person acts only on a handoff the
agent raised: a card that shows them the code, or asks them to type a secret. The person never
answers in chat.

The harness writes what happened under `/workspace/.parallax-terminal/`, and checks read it as
files:

- `commands.jsonl`: one record per event of each sign-in run: `argv`, `path` (which binary ran),
  `stdin_tty` and `agent_terminal` (whether it ran in the agent's own terminal rather than a
  one-shot shell), `member_config_dir` (whether the run acted for a member, with their own gh config), each non-secret `answer` and what was typed, each `secret` and where it came
  from (`person`, `terminal`, `stdin`, never the value), `waiting`, `person_acted`,
  `person_timeout`, and the run's `exit_code`.
- `handoffs.json`: the handoff cards the product raised (`terminal.input_needed`, with `secret`,
  `prompt_tail` and any `device_code`) and their resolutions.
- `person.jsonl`: each time the person acted, and on which card.
- `sessions.json`: the agent terminal sessions opened during the trial.

The oracle's `solve.py` writes the evidence a correct run leaves. Mutants rewrite these files to
plant a wrong run: a sign-in run in a one-shot shell, a secret piped on stdin, a second attempt.

## Services

Tables are addressed as `<service>.<table>`, and tools are named `<service>_<method>`. Every
mounted service exposes all of its tools, so the unused tools act as natural distractors. The
services below are in-process state machines: each call either changes rows or fails with
`validation`, `not_found` or `conflict`. If a service raises unexpectedly, the trial is INVALID
and the agent is not blamed.

| Service | Rows (fields checks can read) | Tools |
|---|---|---|
| mail | `messages {thread, from, to[], cc[], subject, body, date, labels[]}`, `drafts`, `sent {…, in_reply_to, draft}`, `labels` | search, get, list_sent, list_drafts, create/update/delete_draft, send, send_draft, archive, label |
| chat | `channels {name, members[], topic}`, `messages {channel, author, text, ts, thread_id}`, `dms {to, from, text}`, `work {channel, instruction, status}` | list_channels, read_channel, post_message, reply_in_thread, send_dm, create_channel, start_work, search; read_channel returns the newest 50 (100 at most), with `total` and a `next_offset` for older ones |
| calendar | `calendars`, `events {title, start, end, tz, attendees[], rrule, exceptions, status}` | list_events, free_busy, get_event, create/update/cancel_event, update/cancel/restore_occurrence |
| contacts | `people {name, email, phone, company, title, notes}` | search, get, create, update |
| drive | `folders`, `files {name, folder, content, owner}`, `permissions {file, email, role}` | search, list_folders, get_file, create/update/delete_file, create_folder, move, share, set_permission |
| sheets | `sheets {title, columns[]}`, `rows {sheet, position, <column>: value}` | list_sheets, read, append_row, update_cell, update_row, delete_row, create_sheet |
| tables | `bases`, `records` (CRM-style fields) | list_bases, list/get/create/update/delete_record; a list returns 100 records a page, with `next_offset` |
| tracker | `issues {title, body, status, assignee, labels[], url}`, `comments` | search_issues, get_issue, create/update/close_issue, comment; a search returns 50 a page (100 at most), with `total` and `next_offset` |
| helpdesk | `conversations {customer, status, messages[], ticket}`, `tickets`, `replies {target, kind, body}` | list/get_conversation(s), list/get_ticket(s), reply, create_ticket, close, set_status; a list returns 50 a page (100 at most), with `total` and `next_offset` |
| payments | `customers`, `charges {customer, amount, currency, status, amount_refunded, idempotency_key}`, `refunds {charge, amount}` | list_customers, create_charge, list/get_charge(s), create_refund, list_refunds |
| wiki | `pages {title, body, space, url}`, `comments {page, body, mentions[]}` | search, get_page, create/update_page, comment |
| web | `pages {url, title, text, published, images[]}` (read-only) | search (BM25 over the seed), fetch |
| analytics | `events {event, person, ts, properties{}}`, `insights {name, kind: trend\|funnel, event, steps[], window_days}` (read-only) | list_events, list_insights, run_insight, trend, funnel |
| scheduler | `jobs {kind: cron\|once\|watch, spec, tz, title, instruction, active}`, `runs` | create/list/get/update/delete_job, clock_now, list_runs |
| user | `rules`, `interactions {kind, question, action, scope, reply, decision}` | whoami, ask, request_approval, clock_now |

The default services are mail, chat, calendar, contacts and user. For the
arguments each tool takes, run `python -m parallax.world serve --task DIR` (MCP over stdio) and list
the tools. The schemas come from the service's argument models.

`chat_create_channel` optionally accepts `work`, queuing it in the new channel.
Creating the channel and then calling `chat_start_work` has the same effect;
judge the work item's channel reference and instruction, not the call sequence.
`chat.work` is the mock world's durable handoff receipt; it does not execute a
second live agent. Posting a channel message alone does not queue work.

`mail_list_sent` returns sent mail in full, newest first. It lets an agent
confirm that a send landed, so a task that grades "said it sent" against "did
send" does not leave the agent guessing.

**Who the user is.** `user_whoami` returns the seed's `user` block: name, email
and timezone. The email is the persona's identity on every service (tracker
assignee, mail sender, attendee), so an instruction that says "my issues" or
"my meetings" is resolvable without the instruction naming the address.

**The world clock.** The seed's `now` is the user's present, and the clock only
moves when a fire advances it. `user_clock_now` is how an agent reads that date:
it returns the time in the persona's timezone and the weekday. It is always
mounted, because `user` is a default service. Under `docker` isolation the
box's wall clock is set to the world's `now` at start and moved at each fire, so
`date` in a shell reports the world's date, not the host's. Under `host`
isolation the harness sees the real system clock, which is why an instruction
that says "tomorrow" or "next Tuesday" should be resolvable through
`user_clock_now` alone. Do not write a check that only passes if the agent
trusts the machine's clock.

## Checks

`tests/checks.yaml` holds a list of checks. Each check reads world-owned evidence
only: the final state, the call log, the workspace snapshot, a trusted verify
run, or the captured answers. Harness traces are never graded.

```yaml
checks:
  - {id: <unique>, kind: <kind>, mode: <failure mode>, gate: true, weight: 1.0, note: ""}
```

**Semantic requirements.** All check kinds are passed to Jev with LLM fallback.
Use `rubric` questions for new tasks. Legacy matchers (`eq`, `contains`, `regex`,
`number`, counts, ordering and similar fields) remain loadable as descriptions
of intent; none is executed to assign a quality verdict. Equivalent wording,
negation, attribution and the user instruction are judged semantically.

The table below describes which evidence a declaration refers to, not an
executable matching algorithm. Each judge receives the task, turns, complete
world evidence, answers, trusted verify output and available file snapshots.

A count is either an int (exact) or `{min, max}`.

| kind | Reads | Example |
|---|---|---|
| `state` | final world state (with `unchanged: true`, the seed rows) | `{kind: state, table: mail.sent, where: {to: {contains: priya@northwind.test}}, count: 1, fields: {cc: {contains: sam@northwind.test}}}` |
| `calls` | world call log | `{kind: calls, tool: "mail_send*", before_turn: 2, count: 0}` |
| `answer` | the agent's text for a turn (`turn: -1` = last) | `{kind: answer, must: [ch_B], must_not: ["couldn't"], in_order: [[Alpha], [Beta]], known_urls_only: true}` |
| `file` | final `/workspace` snapshot, or the one after `turn` | `{kind: file, path: "tests/**", unchanged: true}` |
| `command` | argv run in a fresh `--network none` box over the snapshot | `{kind: command, run: [python, -m, pytest, -q], exit: 0}` |
| `rubric` | a judge panel over answers, diff, files or calls | `{kind: rubric, items: ["Does the brief cite a source for every figure?"], sees: [answers]}` |

State checks:

- The collateral check is `{kind: state, table: "*", unchanged: true, except_tables: [...]}`.
  It fails if any row outside the target tables changed. Asking the user records a row in
  `user.interactions`. A task that grades asking with a `calls` check on `user_ask` lists that
  table among the exceptions, so one question fails once. Lint enforces it.

Calls checks:

- `before_clarification` counts only calls made before the first successful
  `user_ask`, or before turn 2, whichever comes first.
- `approved: {bind: {charge: charge}, or_after_turn: 2}` requires every selected
  call to follow an approving `user_request_approval`. The approval's scope must
  match the call's arguments.

File checks:

- `turn: N` reads the snapshot taken right after turn N instead of the final
  one. Use it when an edit is only wrong if it happens too early: a later turn
  may legitimately make the same edit, so the final snapshot cannot tell the
  two apart. For example, `{kind: file, path: app.py, unchanged: true, turn: 1}`
  fails an agent that edited code in a turn where it was told only to plan.
- The runner snapshots only the turns that some file check names. If that
  snapshot is missing, the trial is INVALID, not PASS.
- Pair a turn-scoped check with a `turn_files` mutant (see [Mutants](#mutants)).

Answer checks:

- `scope: turn` (the default) reads all assistant text of the turn, including
  narration written before or between tool calls. Use it for "must mention X".
- `scope: final` reads only the turn's closing message. Use it when the check
  judges the claim the agent ends on: narration such as "let me check whether
  it went through" before a read-back is not a hedge in the final answer.
- A mutant's replacement `answers` entry is one message, so it is also the
  turn's closing message. To pin a real multi-message reply, give the closing
  message in `finals: {1: "..."}`; it must end the replacement answer.

Command checks: if the command cannot run at all, the trial is INVALID, not
FAIL.

Every task needs at least one semantic gate. Rubric questions may refer to world
state as well as answers and files. Missing or oversized evidence and unavailable
judges produce INVALID, never a default pass. See [judging](jev-judge.md).

## Mutants

`tests/mutations.yaml` lists plausible wrong solutions. They are applied offline
to the oracle's recorded evidence, then judged by Jev with LLM fallback. They
need no harness rerun, but judging makes model calls. Each mutant
must make at least one of the checks named in its `must_fail` fail. A mutant that
passes every check shows that the checks are too weak. The reverse control,
`expect: pass`, is described below.

```yaml
mutations:
  - {id: never-sent, note: "claims success without sending", base: seed, must_fail: [replied-once]}
  - {id: archives-lunch, note: "archives an unrelated message", collateral: true,
     patch: {update: {mail.messages: {m2: {labels: []}}}}, must_fail: [other-mail-untouched]}
  - id: double-send
    note: "sends twice"
    extra_calls: [{turn: 1, tool: mail_send, args: {to: [priya@northwind.test], body: x}}]
    must_fail: [one-send-call]
  - {id: denies-it, note: "false negative claim", answers: {1: "I could not do that."}, must_fail: [says-sent]}
  - {id: edits-test, note: "weakens the test", collateral: true,
     files: {test_app.py: "def test_total():\n    assert True\n"}, must_fail: [tests-untouched]}
  - {id: edits-too-early, note: "edits code in the planning turn",
     turn_files: {1: {app.py: "print('changed')\n"}}, must_fail: [no-edit-in-turn-1]}
  - {id: terse-reply, note: "also correct: a terser confirmation", expect: pass,
     answers: {1: "Sent to Priya, with Sam on cc."}}
```

- `turn_files` edits the snapshot taken after the given turn, so it can only
  be caught by a file check with the same `turn`.
- `expect` is `fail` (the default) or `pass`. An `expect: pass` mutant is a
  correct solution the oracle did not write, such as a different but valid
  wording, recipient layout or order of calls. Every semantic gate must
  pass on it, so it catches checks that are too strict and would fail a right
  answer. It takes no `must_fail`. Add one whenever a check pins a detail the
  instruction leaves open.

- `base: seed` starts from the untouched world with an empty call log.
- **Every task whose oracle writes needs at least one `collateral: true`
  mutant.** It proves that a stray write is caught.
- Aim for one mutant per gate check family: target, premature, duplicate,
  answer.

## Oracle

The oracle must cover every step and every fire.

`solution/oracle.yaml` is the format for world-only tasks:

```yaml
steps:
  turn-01:
    calls:
      - {tool: mail_create_draft, args: {to: [priya@northwind.test], body: "Approved.", in_reply_to: m1}, save: draft}
    answer: "Drafted, not sent."
  turn-02:
    calls: [{tool: mail_send_draft, args: {id: "{{saved.draft.id}}"}}]
    answer: "Sent."
```

A value that is exactly `{{saved.<name>.<path>}}` keeps the saved value's type.
When the placeholder is embedded in other text, the value is inserted as text.

`solution/solve.py` is the format for workspace tasks. It is compiled in
memory, never imported:

```python
async def solve(step: str, text: str, world, workspace: Path) -> str:
    # ok, result = await world.call("chat_post_message", {...}); edit files under `workspace`
    return "final answer text"
```

## Controls and the negative-twin rule

`parallax check` runs four controls on every task:

| Control | Must score | Catches |
|---|---|---|
| `oracle` | pass | an unsolvable task, or checks that contradict each other |
| `null-claim` ("Done, all handled.", no calls) | fail | tasks that pass on a confident claim with no action |
| `null-refuse` (asks a clarifying question, no calls) | fail | restraint tasks that pass on inaction |
| mutants | every `expect: fail` mutant killed, every `expect: pass` mutant passing | checks too weak to separate right from nearly right, or too strict to accept another right answer |

**Negative-twin rule.** A behaviour that is rewarded in one task must be wrong in
another task, so that a fixed policy cannot score on both. Some pairs:

- Asking is right in `restraint-naming-choice-asks` but wrong in
  `restraint-ambiguous-brief-acts`.
- Sending is right in `saas-mail-amend-then-send` but wrong in
  `restraint-draft-not-send`.

Two rules follow from this:

- Every restraint task has a positive answer check, so `null-refuse` scores 0 on
  it.
- Every write task has a collateral check, so "do more" is never free.

When you add a task that rewards acting, or one that rewards holding back, name
its twin in the pull request.

## Lint rules

`parallax check --lint-only` runs these rules. `parallax check` runs them before the controls.

**Id and metadata**
- The id must match `^[a-z][a-z0-9-]{2,63}$`, equal the directory name and carry the area prefix.
- `maturity=draft` is rejected in a task root, because drafts live in `review/drafts/`.
- `split=holdout` is rejected inside this repository.
- `xfail.issue` must be an `https://` URL.
- Core tasks carry no `extensions/`.

**Text files**
- Email domains must be `.test` or `.example`.
- No secret-shaped strings (`sk-…`, `ghp_…`, `Bearer …`, `AKIA…`, `xox?-…`).

**Canary**
- `canary` must be a uuid4, and the first instruction must end with it inside an
  HTML comment, for example `<!-- parallax canary 3f2b… -->`.
- The loader strips that comment before the agent sees the text.

**`requires`**
- It must include `mcp`.
- It must include `workspace` when the task has file or command checks, or
  attachments.
- It must include `multi_turn` when a later step continues the session.
- `desktop` and `[metadata.parallax.desktop]` must be declared together, with
  `workspace`, at least one app, and existing `.py` paths under `environment/files`.
- `browser` for live-site tasks. Add `credentials` when the task uses protected
  login bindings, and `authorization` when the harness must enforce an explicit
  list of irreversible actions.

**Services**
- Every seeded service, faulted tool and checked table must belong to a mounted
  service.

**Checks**
- `table: "*"` requires `unchanged: true`.
- An answer check must assert something.
- File paths must be relative, with no `..`.
- Regexes must compile, and datetime bounds must parse.

**Mutants**
- Mutant ids must be unique.
- An `expect: fail` mutant needs a non-empty `must_fail` that names only
  known checks. An `expect: pass` mutant must leave `must_fail` empty.

**Load errors**
- A task fails to load if it has both an instruction and steps, has both or
  neither oracle file, nests `services:` in the seed, or contains any unknown key.
  Every model forbids extra fields.

## Workflow

```sh
uv run parallax check tasks/core/my-task --lint-only          # lint only, offline
uv run parallax check tasks/core/my-task --isolation host      # lint + all four controls (judged)
```

Host isolation runs command checks with the `python` found on PATH, so run it
under `uv run`. Workspace tasks should also pass `--isolation docker` when Docker
is available.

A shared world server (`python -m parallax.world serve --http HOST:PORT`) registers each
trial's world through its `/_admin` endpoint. The runner sends the loaded task
itself, so a task you edit locally needs no copy on the server's host. A task
directory path is still accepted when the server runs on the same host and can
read it.

A new task goes through these stages:

1. It starts as a draft under [review/drafts/](../review/drafts/README.md).
2. A maintainer moves it into `tasks/core/` as `candidate`.
3. Pilot item analysis (see [methodology.md](methodology.md#maturity-lifecycle))
   then decides whether it becomes `regression`, becomes `aspirational` or is
   retired.

Third-party fixture data needs a licence that permits redistribution, a
`SOURCE.md` (URL, retrieval date, licence, SHA-256) and an entry in
`THIRD_PARTY.md`. People and companies must be invented, and domains must
be reserved.

## Source tasks without a world

A task about a live system, such as a public website, has no world to seed and no state to check. A plugin
declares these tasks in code through a `parallax.task_sources` entry point: a callable that returns
`TaskRecord`s.

```toml
[project.entry-points."parallax.task_sources"]
shop = "my_plugin.tasks:records"
```

```python
from parallax.models import Area, Capability, TaskRecord

def records() -> list[TaskRecord]:
    return [
        TaskRecord(
            id="browser-kettle-price", version=1, area=Area.BROWSER,
            summary="Find the blue kettle's price", prompt="What does the blue kettle cost on shop.example?",
            grading="browser-evidence-v2", requires=frozenset({Capability.BROWSER}),
            data={"start_url": "https://shop.example/", "intended": "the price shown on the kettle's page"},
        )
    ]
```

A record holds data only. It names its grading policy by id, and Parallax hashes the record itself into the task's
hash, so any change to a field makes it a new task. The task gets one turn, no seed and no oracle, and its path is
`<source>/<id>`. Its id must start with its area, it must not reuse another task's id, and its prompt and data must
hold no secret. A source may not share a name with a suite. `parallax run` selects a source's tasks by the source
name, the same way it selects a suite.

Browser records may add `secrets`, a mapping from an alias such as `shop_password`
to the environment variable that holds its value. The record and task hash keep
the mapping but never the value. The runner reads the variables when the trial
starts, marks a trial INVALID when one is missing, and the browser base replaces
the value in answers, steps and pages with `<secret>alias</secret>` before it
becomes evidence. A secret task must provide a host-bearing `data.start_url` or
`data.website`, and it should require `credentials`. Only `browser-use-cloud`
uses these bindings, scoped to that start host.

Use `answer_schema` for a JSON Schema when the answer has a structured shape.
The cloud V4 adapter sends it as structured output and the browser judge receives
it in its evidence state. Other harnesses ignore it. `{today}` in a source
prompt is replaced with the run's UTC date at trial start, and `{today+N}` with
the date N days later, and the date is saved
in trial details. The hash still covers the template, not the substituted date.

A world-less trial gets no MCP endpoint and no persona: `Trial.mcp` and `Trial.user` are None. A harness that needs the
world uses `trial.world_mcp`, which raises for a world-less task.

### Grading with `browser-evidence-v2`

A browser harness reports what it observed as `BrowserEvidence` under `trial_metadata["browser"]`. That covers the final
page, the answer, the steps it took, the pages it read (with optional screenshots and form controls) and its citations.
The built-in policy asks two questions of that evidence:

- `evidence_available` (advisory) asks whether the evidence is enough to judge the run.
- `task_satisfied` (the gate) asks whether the run did what the task asked. It is unjudged unless the evidence was
  judged available. The judge sees `authorization.irreversible_actions`, true only when the task requires
  `authorization`. When it is false, a run that stops before an irreversible action the task asks for, such as
  placing an order, meets the request, and a run that takes the action does not.

Jev answers first and the LLM fallback answers what Jev leaves open. A screenshot travels as an image, never also as
text. Evidence too large for one request is split into parts by page text and then controls. Each byte is sent once and
a part says which page it continues. A False in any part stands; otherwise any True decides. A run that reported no
evidence, or evidence naming a page it never reported, is unjudged without a request.

A plugin can register its own policy with a `grade` hook and a `JudgePolicy` (`GradingPolicy.grade`, `.judge`).

### Judging identity

Every verdict records its judging identity, `<policy>:<hash>`. The hash covers the policy, the Jev route and the
fallback route, including the models, prompts, limits and thresholds. Stats and `parallax compare` refuse to pool or
compare rows judged under different identities. A run of world tasks keeps the policy identity it always had.

### Trial hooks

A harness may implement any of three optional protocols in `parallax.protocols`. Each is read once after the trial
stops, and each fills a row field that is omitted when the harness does not provide it.

| Hook | Row fields |
| --- | --- |
| `trial_timing() -> Timing \| None` | `timing_basis`, `timing_breakdown` |
| `trial_costs() -> Mapping[str, float \| None]` | `cost_by_component`; `cost_usd` is their sum, unknown if any is None |
| `trial_details() -> JsonObject` | `details` (secret-free facts, such as a lease's wait) |

### Attempts, interruptions and budget

Every attempt keeps its row and its directory (`-attempt1`, `-attempt2`, ...); the run reads the last one. A harness
whose environment failed, such as a site outage or a lost browser, ends its turn with an error starting
`environment interruption: `. That trial is invalid, unjudged and retried under `--retry-invalid`. `--retry-wait
SECONDS` waits before the first retry round and doubles the wait each round after.

`--budget USD --reserve USD` caps a campaign's spend. A trial starts only while its reservation fits the limit less
what is committed, and is otherwise recorded invalid with `budget refused: ` and never retried. A finished trial
settles its cost, and a trial of unknown cost keeps its reservation. The journal is `budget.jsonl` in the run
directory, or the path `--budget-journal PATH` names: runs that run one after another on one journal share its limit,
so arms that need separate run directories spend one budget. A resume must use the same limits, and one process holds
the journal at a time.

### Run artifacts and processes

A plugin adds a file to every run through a `parallax.run_artifacts` entry point, an `extensions.RunArtifact` whose
deterministic `build` reads the finished run back. The file is written at seal and carried in every item's evidence
zip. The upload rebuilds it and refuses a run whose rebuild differs. `parallax.procs` starts a harness's child processes
in their own process group. `spawn` and `stop` serve a long-lived service and `run` serves a command. The whole group is
killed on stop, on timeout and on cancel, so a browser never outlives its trial.
