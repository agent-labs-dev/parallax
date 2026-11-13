"""Every Pydantic contract shared across parallax slices.

Definition order differs from the spec's reading order: matcher and check types come
before the task types that reference them, and `model_rebuild()` at the bottom
resolves the remaining forward references.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, JsonValue, model_validator


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


JsonObject = dict[str, JsonValue]
Scalar = str | int | float | bool | None


# ---------------------------------------------------------------------------
# matchers (defined first: task, seed and check types reference them)
# ---------------------------------------------------------------------------


class Match(Model):
    """All set keys must hold; strings are compared after `match.normalise()`."""

    eq: Scalar | None = None
    ne: Scalar | None = None
    contains: str | tuple[str, ...] | None = None  # substring(s), or membership when the field is a list
    any_of: tuple[Scalar, ...] | None = None  # aliases
    none_of: tuple[Scalar, ...] | None = None
    regex: str | None = None
    number: float | None = None  # a number extracted from text equals this within tol
    tol: float = 0.0
    gte: float | str | None = None  # numbers or ISO datetimes
    lte: float | str | None = None
    absent: bool | None = None

    @model_validator(mode="after")
    def _has_condition(self) -> Match:
        # An empty Match would silently select every row / accept every value.
        conditions = self.model_dump(exclude={"tol"}, exclude_none=True)
        if not conditions:
            raise ValueError("Match needs at least one condition (tol alone is not a condition)")
        return self


Matcher = Scalar | Match  # bare scalar == Match(eq=...); dotted keys address nested fields: "scope.charge"


class Range(Model):
    min: int = 0
    max: int | None = None


Count = int | Range


class FailureMode(StrEnum):
    MISSING_ACTION = "missing_action"
    WRONG_TARGET = "wrong_target"
    COLLATERAL_CHANGE = "collateral_change"
    DUPLICATE_ACTION = "duplicate_action"
    PREMATURE_ACTION = "premature_action"
    IGNORED_CORRECTION = "ignored_correction"
    FALSE_CLAIM = "false_claim"
    FABRICATION = "fabrication"
    NO_RECOVERY = "no_recovery"
    UNSAFE_ACTION = "unsafe_action"
    FORGOT_CONTEXT = "forgot_context"
    WRONG_OUTPUT = "wrong_output"
    BROKE_BUILD = "broke_build"
    WRONG_SCHEDULE = "wrong_schedule"


# ---------------------------------------------------------------------------
# task and task.toml
# ---------------------------------------------------------------------------


class Capability(StrEnum):
    MCP = "mcp"  # mounts the world MCP server and calls its tools
    WORKSPACE = "workspace"  # reads/writes /workspace which the runner snapshots (attachments are delivered here)
    SHELL = "shell"  # runs commands in /workspace
    DESKTOP = "desktop"  # operates seeded GUI apps in the device box's X11/AT-SPI session
    MULTI_TURN = "multi_turn"  # continues one conversation across steps
    MEMORY = "memory"  # recalls across a fresh session using the harness's own persistent memory
    # Runs on the world's clock, so "today" and "next week" mean the scenario's dates. The runner
    # grants it to every harness whose process it owns (`trial_capabilities`); a remote product has
    # it only if it declares it.
    WORLD_CLOCK = "world_clock"
    # Starts a step as a scheduled job's run that nobody is watching (`StepSpec.origin`), and answers
    # the approval card the product raises for it with the step's scripted decision.
    UNATTENDED = "unattended"
    # Runs interactive commands in a persistent terminal: answers their non-secret prompts and hands
    # sign-ins and secrets to the user. Evidence is under /workspace/.parallax-terminal
    # (docs/authoring.md#terminal-tasks).
    TERMINAL = "terminal"
    # Operates a web browser on live sites; a task requiring it runs on no seeded world.
    BROWSER = "browser"


# Starting a fresh session is universal (every adapter can start a new conversation); MEMORY is about recall.


class Area(StrEnum):
    SAAS = "saas"
    PRECISION = "precision"
    RECOVERY = "recovery"
    SAFETY = "safety"
    CODE = "code"
    FILES = "files"
    RESEARCH = "research"
    RESTRAINT = "restraint"
    MEMORY = "memory"
    SCHEDULE = "schedule"
    # Operating the harness's own product surface (its channels, agents, jobs, apps, devices) rather
    # than a seeded third-party service. A plugin's task extension supplies the lane that grades it.
    PLATFORM = "platform"
    # A recorded call replayed through the harness's own voice agent loop, as a plugin's task extension
    # declares it: whether it speaks or stays silent per line, and what it says, looks up and hands off.
    VOICE = "voice"
    # Work in a web browser on live sites, graded from the evidence the browser harness reports.
    BROWSER = "browser"
    CONFORMANCE = "conformance"


class Split(StrEnum):
    TUNE = "tune"  # every public task; visible to the self-improvement proposer
    HOLDOUT = "holdout"  # only valid in the private parallax-holdout repo; the loader rejects it under tasks/


class Maturity(StrEnum):
    DRAFT = "draft"  # only under review/drafts/; never discovered
    CANDIDATE = "candidate"  # passes controls; not yet piloted
    REGRESSION = "regression"  # piloted, discriminates between harnesses
    ASPIRATIONAL = "aspirational"  # piloted, best harness pass@1 < 0.5
    RETIRED = "retired"  # kept for history; never scheduled


class Provenance(StrEnum):
    HAND_AUTHORED = "hand_authored"
    CONVERTED = "converted"
    DERIVED_FROM_FAILURE_CLASS = "derived_from_failure_class"


class FaultKind(StrEnum):
    RATE_LIMITED = "rate_limited"  # no mutation; 429-shaped error with retry_after_s
    SERVER_ERROR = "server_error"  # no mutation; 500-shaped error
    AUTH_EXPIRED = "auth_expired"  # no mutation; 401-shaped error
    VALIDATION = "validation"  # no mutation; vendor-style field error
    LOST_RESPONSE = "lost_response"  # mutation COMMITTED, then error {code: outcome_unknown}
    CORRUPT_SUCCESS = "corrupt_success"  # NO mutation, but a schema-valid success result is returned


class Fault(Model):
    tool: str  # canonical tool name, e.g. "chat_post_message"
    on_call: int = 1  # 1-based index among calls to that tool
    times: int = 1  # consecutive calls affected; 0 = every call from on_call on
    kind: FaultKind


class Attachment(Model):
    # Under environment/files; delivered to /workspace/attachments/<name> and announced
    # as a line "Attached: attachments/<name>" (a remote harness may pass it as context instead).
    path: str


class RunOrigin(StrEnum):
    """Who starts a step's run."""

    MEMBER = "member"  # the user typed it in the harness's own client
    JOB = "job"  # a scheduled job fired it; nobody is watching the run


class ApprovalDecision(StrEnum):
    """How the user answers an approval card the product raises during an unattended step."""

    ALLOW_ONCE = "allow_once"
    ALLOW_ALWAYS = "allow_always"  # allow, and let later unattended runs through without asking
    DENY = "deny"
    TIMEOUT = "timeout"  # never answered: the product's own timeout refuses the card


class ApprovalAnswer(Model):
    """One approval card the product raised during a turn, read from the product, and the answer given."""

    turn: int
    request: str  # what the card asked to do, as the product worded it
    decision: ApprovalDecision


class StepSpec(Model):
    """One [[steps]] entry; text comes from steps/<name>/instruction.md."""

    name: str  # "turn-01", "turn-02", ... ; the 1-based position is the turn index
    session: Literal["continue", "new"] = "continue"
    attachments: tuple[Attachment, ...] = ()
    patch: str | None = None  # world/patches/<patch>.yaml applied BEFORE this step is delivered
    # job: the text is a job's instruction, run with nobody watching
    origin: RunOrigin = Field(default=RunOrigin.MEMBER, exclude_if=lambda v: v is RunOrigin.MEMBER)
    # Every approval card the product raises during this job step gets this answer; None leaves
    # a card unanswered, which ends the trial as a harness error.
    approval: ApprovalDecision | None = Field(default=None, exclude_if=lambda v: v is None)


class Fire(Model):
    """Scheduler tier: after all steps, the runner fires stored jobs."""

    name: str  # "fire-1", ... ; oracle keys and turn index continue after steps
    at: AwareDatetime  # world clock advanced to this instant first
    job: dict[str, Matcher]  # selects exactly one scheduler.jobs row; 0 or >1 -> fire recorded as missed
    patch: str | None = None  # applied before the clock advance (e.g. new tickets arrive)


class XFail(Model):
    reason: str
    issue: str  # public issue URL


class DesktopApp(Model):
    path: str  # Python GTK3 program under environment/files, run with /usr/bin/python3
    title: str = Field(min_length=1)  # exact initial window title; startup waits for it


class Desktop(Model):
    apps: tuple[DesktopApp, ...] = Field(min_length=1)


class TaskMeta(Model):
    """[metadata.parallax]"""

    id: str  # == dir name; ^[a-z][a-z0-9-]{2,63}$, prefixed by area
    version: int  # bump on any change that alters grading
    area: Area
    summary: str
    requires: frozenset[Capability]
    services: tuple[str, ...] = ()  # mounted in addition to MCP tasks' default synthetic services
    split: Split = Split.TUNE
    maturity: Maturity = Maturity.CANDIDATE
    provenance: Provenance
    legacy_ids: tuple[str, ...] = ()
    canary: str  # uuid4; also in the first instruction's trailing HTML comment
    xfail: XFail | None = None
    faults: tuple[Fault, ...] = ()
    fires: tuple[Fire, ...] = ()
    desktop: Desktop | None = Field(default=None, exclude_if=lambda v: v is None)


class SuiteManifest(Model):
    """A suite's `suite.toml`: who owns it and what it is for. A selection suite also names the
    suite its tasks come from (`within`) and lists their ids; a directory suite is every task under it."""

    owner: str
    purpose: str
    within: str | None = None
    tasks: tuple[str, ...] = ()
    harnesses: tuple[str, ...] = ()  # the harnesses it is meant to rank; empty means any
    judge_policy: str | None = None
    consumers: tuple[str, ...] = ()  # workflows or repositories that run it

    @model_validator(mode="after")
    def selection_names_its_tasks(self) -> SuiteManifest:
        if (self.within is None) != (not self.tasks):
            raise ValueError("a selection suite declares both within and tasks; a directory suite neither")
        if len(set(self.tasks)) != len(self.tasks):
            raise ValueError("suite tasks are listed twice")
        return self


class HarborVerifier(Model):
    timeout_sec: int = 300


class HarborEnvironment(Model):
    build_timeout_sec: int = 600
    cpus: int = 2
    memory_mb: int = 4096


class TaskToml(Model):
    """Exact task.toml shape."""

    version: Literal["1.0"] = "1.0"
    metadata: dict[Literal["parallax"], TaskMeta]
    verifier: HarborVerifier = HarborVerifier()
    environment: HarborEnvironment = HarborEnvironment()
    steps: tuple[StepSpec, ...] = ()  # empty => single step "turn-01" from instruction.md


# ---------------------------------------------------------------------------
# world seed, patches, calls
# ---------------------------------------------------------------------------


class Persona(Model):
    name: str
    email: str
    timezone: str  # IANA


class WebPage(Model):
    url: str
    title: str
    text: str
    published: date | None = None
    images: tuple[str, ...] = ()


class UserRule(Model):
    """Scripted replies of the `user` service (no simulator)."""

    tool: Literal["ask", "approval"]
    when: dict[str, Matcher] = {}  # on the call args (question text / action / scope fields)
    reply: str
    decision: Literal["approve", "deny", "answer"] = "answer"  # approval rules use approve|deny
    uses: int = 0  # 0 = unlimited


class WorldSeed(Model):
    now: AwareDatetime  # frozen start clock
    user: Persona
    services: dict[str, dict[str, list[JsonObject]]] = {}  # service -> table -> rows (each has "id")
    web: tuple[WebPage, ...] = ()  # loaded into web.pages
    user_rules: tuple[UserRule, ...] = ()
    # Unmatched ask -> "I don't have a preference - use your judgement." (answer)
    # Unmatched approval -> "No, hold off on that for now." (deny)


class WorldPatch(Model):
    """Applied by the runner between turns / before fires."""

    insert: dict[str, list[JsonObject]] = {}  # "svc.table" -> rows
    update: dict[str, dict[str, JsonObject]] = {}  # "svc.table" -> id -> fields
    delete: dict[str, tuple[str, ...]] = {}
    rename_ids: dict[str, dict[str, str]] = {}  # "svc.table" -> old_id -> new_id (stale-id tasks)


class CallError(StrEnum):
    VALIDATION = "validation"
    NOT_FOUND = "not_found"
    CONFLICT = "conflict"
    FAULT = "fault"
    INTERNAL = "internal"  # a service raised: trial INVALID


class WorldCall(Model):
    """One line of world_log.jsonl."""

    seq: int
    turn: int
    session: int
    at: AwareDatetime  # world clock
    tool: str
    service: str
    args: JsonObject
    ok: bool
    result: JsonValue  # truncated to 8 KB in the log
    error: CallError | None = None
    fault: FaultKind | None = None
    mutated: bool  # the call changed at least one row (true for lost_response)


class RowChange(Model):
    id: str
    before: JsonObject
    after: JsonObject
    fields: tuple[str, ...]


class TableDiff(Model):
    added: tuple[JsonObject, ...] = ()
    removed: tuple[JsonObject, ...] = ()
    changed: tuple[RowChange, ...] = ()


class StateDiff(Model):
    tables: dict[str, TableDiff]  # only tables with a change


class WorldToolSpec(Model):
    """The mounted world tool contract available to the semantic judge."""

    name: str
    description: str
    input_schema: JsonObject


State = dict[str, dict[str, JsonObject]]  # "svc.table" -> id -> row


class WorldEvidence(Model):
    """GET /_control/{token}/evidence"""

    seed_now: AwareDatetime  # the seed clock: year-less dates and "Tuesday 14:00" in checks resolve against it
    seed_state: State
    final: State
    diff: StateDiff
    calls: tuple[WorldCall, ...]
    internal_error: str | None
    missed_fires: tuple[str, ...] = ()
    tool_schemas: tuple[WorldToolSpec, ...] = ()


class Turn(Model):
    """Resolved by the loader."""

    index: int  # 1-based across steps then fires
    name: str
    text: str
    new_session: bool
    attachments: tuple[Attachment, ...] = ()
    patch: WorldPatch | None = None
    # Unset fields stay out of dumps, so the evidence of member-only tasks is unchanged.
    origin: RunOrigin = Field(default=RunOrigin.MEMBER, exclude_if=lambda v: v is RunOrigin.MEMBER)
    approval: ApprovalDecision | None = Field(default=None, exclude_if=lambda v: v is None)


# ---------------------------------------------------------------------------
# checks (tests/checks.yaml)
# ---------------------------------------------------------------------------

CheckKind = Literal["state", "calls", "answer", "file", "command", "rubric"]


class CheckBase(Model):
    id: str
    mode: FailureMode
    gate: bool = True
    weight: float = 1.0
    note: str = ""


class StateCheck(CheckBase):
    kind: Literal["state"]
    table: str  # "mail.sent", or "*" (all tables) with unchanged=true
    where: dict[str, Matcher] = {}  # selects rows: in FINAL state, or in the SEED when unchanged=true
    count: Count | None = None
    fields: dict[str, Matcher] = {}  # every selected row must satisfy
    unchanged: bool = False  # selected seed rows (or whole tables) byte-equal in final, not deleted
    except_tables: tuple[str, ...] = ()  # with table="*"


class ApprovalBinding(Model):
    """Scope-bound consent."""

    bind: dict[str, str]  # approval scope key -> call arg path, e.g. {charge: charge, amount: amount}
    or_after_turn: int | None = None  # a call made in turn >= N counts as consented by the scripted turn


class CallsCheck(CheckBase):
    kind: Literal["calls"]
    tool: str  # canonical name; fnmatch glob allowed ("mail_send*")
    where: dict[str, Matcher] = {}  # on args
    ok: bool | None = True  # None = count failed calls too
    count: Count | None = None
    before_turn: int | None = None  # only calls in turns < N
    after_turn: int | None = None  # only calls in turns >= N
    before_clarification: bool = False  # only calls before min(first ok user_ask call, first turn >= 2)
    approved: ApprovalBinding | None = None  # every selected ok call has a prior approving receipt with matching scope


class AnswerScope(StrEnum):
    TURN = "turn"  # all assistant text of the turn, narration between tool calls included
    FINAL = "final"  # the turn's closing message: judges the claim the agent ended on


class AnswerCheck(CheckBase):
    kind: Literal["answer"]
    turn: int = -1  # 1-based turn index; -1 = last delivered turn
    scope: AnswerScope = AnswerScope.TURN
    must: tuple[Matcher, ...] = ()
    must_not: tuple[Matcher, ...] = ()
    in_order: tuple[tuple[str, ...], ...] = ()  # tie groups: [["A","B"],["C"]]
    known_urls_only: bool = False  # every URL in the answer exists in web seed or world rows


class FileCheck(CheckBase):
    kind: Literal["file"]
    path: str  # glob relative to /workspace
    exists: bool | None = None
    contains: tuple[Matcher, ...] = ()
    unchanged: bool = False  # byte-equal to environment/files
    # 1-based turn: read the snapshot taken right after that turn instead of the final one, so an
    # edit made too early is visible even when a later turn legitimately makes the same edit.
    turn: int | None = None


class CommandCheck(CheckBase):
    kind: Literal["command"]
    run: tuple[str, ...]  # argv; runs in a fresh --network none verify container over the snapshot
    exit: int = 0
    stdout: tuple[Matcher, ...] = ()
    timeout_s: int = 300


class RubricCheck(CheckBase):
    kind: Literal["rubric"]
    items: tuple[str, ...] = Field(min_length=1)  # binary questions about observable work, including world state
    sees: frozenset[Literal["answers", "diff", "files", "calls", "approvals"]] = frozenset({"answers", "diff"})


Check = Annotated[
    StateCheck | CallsCheck | AnswerCheck | FileCheck | CommandCheck | RubricCheck,
    Field(discriminator="kind"),
]


class CheckSet(Model):
    checks: tuple[Check, ...]

    @model_validator(mode="after")
    def _valid(self) -> CheckSet:
        ids = [c.id for c in self.checks]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate check id")
        if not any(c.gate for c in self.checks):
            raise ValueError("needs a semantic gate")
        return self


class ScriptedCall(Model):
    turn: int
    tool: str
    args: JsonObject
    ok: bool = True
    mutated: bool = True


class MutationExpect(StrEnum):
    FAIL = "fail"  # a plausible wrong solution: at least one check in must_fail must fail
    PASS = "pass"  # a correct solution the oracle did not write: every semantic gate must pass


class Mutation(Model):
    id: str
    note: str
    expect: MutationExpect = MutationExpect.FAIL
    base: Literal["oracle", "seed"] = "oracle"  # start from oracle evidence or from the untouched seed
    patch: WorldPatch | None = None  # applied to the final state
    answers: dict[int, str] = {}  # turn -> replacement answer
    # turn -> the closing message of that replacement answer, for a multi-message answer whose
    # narration a final-scoped check must not read; unset, the replacement is one message.
    finals: dict[int, str] = {}
    files: dict[str, str | None] = {}  # workspace path -> new content (None deletes)
    turn_files: dict[int, dict[str, str | None]] = {}  # turn -> edits to that turn's snapshot (early-edit mutants)
    # turn -> the approval requests raised in that turn, each answered with the step's decision
    approvals: dict[int, tuple[str, ...]] = {}
    extra_calls: tuple[ScriptedCall, ...] = ()  # appended to the call log (premature/duplicate mutants)
    collateral: bool = False  # marks the required collateral mutant
    must_fail: tuple[str, ...] = ()  # check ids (non-rubric); at least one must fail; empty when expect=pass

    @model_validator(mode="after")
    def _finals_end_answers(self) -> Mutation:
        for turn, final in self.finals.items():
            if not self.answers.get(turn, "").strip().endswith(final.strip()):
                raise ValueError(f"mutation {self.id}: finals[{turn}] must end answers[{turn}]")
        return self


class MutationSet(Model):
    mutations: tuple[Mutation, ...] = ()


class Task(Model):
    """What task.load_task returns, or a task source's record once loaded (`task.source_tasks`)."""

    meta: TaskMeta
    turns: tuple[Turn, ...]  # steps only; fire turns are produced at run time
    # None for a world-less task: no world is registered, no MCP is mounted, and its grading policy grades it
    # from what the harness reported (`GradingPolicy.grade`). Only a task source declares one.
    seed: WorldSeed | None
    checks: CheckSet
    mutations: MutationSet
    oracle: Literal["yaml", "py"] | None  # None: the task has no scripted solution (a task source's record)
    path: Path
    hash: str  # sha256 over the canonicalised task dir, or over a task source record's declared content
    # Plugin `[metadata.<name>]` blocks, validated by their `parallax.extensions.TaskExtension`.
    extensions: dict[str, JsonValue] = Field(default_factory=dict, exclude_if=lambda v: not v)
    # The `parallax.task_sources` entry point that declared the task; None for a task directory.
    source: str | None = Field(default=None, exclude_if=lambda v: v is None)
    # The grading policy a task source's record names (`extensions.grading_policies`).
    grading: str | None = Field(default=None, exclude_if=lambda v: v is None)
    # A task source record's own data (a start URL, the intended outcome), for its harness and grader.
    data: JsonObject = Field(default_factory=dict, exclude_if=lambda v: not v)

    @property
    def id(self) -> str:
        return self.meta.id

    @property
    def world(self) -> WorldSeed:
        """The task's seed; raises for a world-less task, which no world path may reach."""
        if self.seed is None:
            raise ValueError(f"task {self.meta.id} is world-less: it has no seeded world")
        return self.seed


class TaskRecord(Model):
    """One task a `parallax.task_sources` entry point declares in code instead of a task directory.

    Immutable data only: the grading policy is named, never passed as a callable, so the record hashes to the
    task's identity (`task.record_hash`) and a change to any field is a new task hash.
    """

    id: str  # ^[a-z][a-z0-9-]{2,63}$, unique across every suite and source
    version: int  # bump on any change that alters grading, as in task.toml
    area: Area
    summary: str
    prompt: str  # the one turn the agent is given
    grading: str  # a registered grading policy id, e.g. "browser-evidence-v1"
    requires: frozenset[Capability] = frozenset()
    maturity: Maturity = Maturity.CANDIDATE
    split: Split = Split.TUNE
    provenance: Provenance = Provenance.HAND_AUTHORED
    xfail: XFail | None = None
    data: JsonObject = {}  # the task's own facts for its harness and grader: start URL, intended outcome


class CommandOutcome(Model):
    exit: int | None
    stdout: str
    stderr: str
    error: str | None  # error set => MissingEvidence


class Evidence(Model):
    """Assembled by the runner only from parallax-owned sources."""

    delivered_turns: tuple[TurnInput, ...] = ()  # exact runtime prompts, including scheduled fires
    answers: tuple[str, ...]  # index i = turn i+1
    # The closing message of each turn, parallel to `answers`; empty when the producer did not
    # record them; semantic judges must account for that evidence limitation.
    finals: tuple[str, ...] = ()
    world: WorldEvidence
    workspace: Path | None  # extracted final snapshot dir
    turn_workspaces: dict[int, Path] = {}  # turn -> snapshot after it, for turn-scoped file checks only
    seed_files: Path | None  # environment/files
    verify: dict[str, CommandOutcome]  # check id -> outcome (filled by box.trusted_verify)
    persona_tz: str
    # The product's approval cards and the scripted answers; absent from dumps when there were none.
    approvals: tuple[ApprovalAnswer, ...] = Field(default=(), exclude_if=lambda v: not v)

    @model_validator(mode="after")
    def _finals_parallel(self) -> Evidence:
        if self.finals and len(self.finals) != len(self.answers):
            raise ValueError(f"{len(self.finals)} final messages for {len(self.answers)} answers")
        return self


# ---------------------------------------------------------------------------
# harness side
# ---------------------------------------------------------------------------


class HarnessKind(StrEnum):
    OPEN = "open"
    PRODUCT = "product"
    CONTROL = "control"


class CostSource(StrEnum):
    METERED = "metered"
    HARNESS = "harness"
    NONE = "none"  # renders as "-", never $0


class Isolation(StrEnum):
    DOCKER = "docker"
    HOST = "host"
    REMOTE = "remote"


class WebIsolation(StrEnum):
    ENFORCED = "enforced"  # docker egress: box reaches only the sidecar
    FLAGS = "flags"  # host isolation, native web disabled by flag/config only
    NONE = "none"  # no usage reported


class RemoteBox(StrEnum):
    """Where a remote harness's trials run on the runner's side (`HarnessInfo.remote_box`)."""

    HOST = "host"  # a temp dir on the runner host: the product works elsewhere and touches nothing local
    # A container with egress for the product's own device daemon, which is the agent's shell and
    # workspace. Opened for every trial, so no trial's shell runs on a device another trial used.
    DEVICE = "device"


class TrialFlag(StrEnum):
    """Why a row's numbers need a caveat; each one is shown beside the row in the report."""

    MODEL_UNOBSERVED = "model_unobserved"  # some model response did not name the model that served it
    MODEL_MISMATCH = "model_mismatch"  # a response named a model other than the one requested
    NATIVE_CREDENTIAL = "native_credential"  # the harness spent its own provider credential: unmetered
    # The trial ran in an account that also holds real state the agent can see (connected accounts or
    # memories it did not write): not an isolated user. A remote product's adapter reports it.
    SHARED_ACCOUNT = "shared_account"
    USAGE_PROVISIONAL = "usage_provisional"  # no server finality signal backs this snapshot


class HarnessInfo(Model):
    name: str
    kind: HarnessKind
    capabilities: frozenset[Capability]  # declared UPPER BOUND
    pins_model: bool
    cost_source: CostSource
    isolation: Isolation = Isolation.DOCKER
    install: str = ""  # RUN line for box.Dockerfile
    version_cmd: tuple[str, ...] = ()
    web_disabled_by: Literal["flag", "config", "none"] = "none"
    source_url: str = ""
    # Host env vars forwarded into the box verbatim, for a harness the meter cannot route (its
    # provider API is not one the meter speaks). Only legal when cost_source is not metered; every
    # row it produces is flagged NATIVE_CREDENTIAL, since nothing parallax-owned saw its spend.
    credential_env: tuple[str, ...] = ()
    # Remote harnesses only. Left out of the serialized info at its default, so declaring the field
    # did not change the configuration identity (`config_hash`) of any harness that does not use it.
    remote_box: RemoteBox = Field(default=RemoteBox.HOST, exclude_if=lambda box: box is RemoteBox.HOST)


class McpEndpoint(Model):
    url: str  # streamable HTTP as seen from where the harness runs
    token_env: str = "PARALLAX_WORLD_TOKEN"  # env var holding the bearer token
    stdio: tuple[str, ...]  # ("python","-m","parallax.world","bridge","--url",url,"--token-env","PARALLAX_WORLD_TOKEN")
    # The world's mounted services; `<url>/<service>` serves one alone, as one app (`parallax.world.apps`).
    services: tuple[str, ...] = Field(default=(), exclude_if=lambda v: not v)


class LlmEndpoint(Model):
    openai_base_url: str
    anthropic_base_url: str
    api_key: str  # per-trial meter key


class Trial(Model):
    """Given to Harness.start."""

    trial_id: str
    task_id: str
    model: str | None  # "provider/model"; None only for controls and products
    mcp: McpEndpoint | None  # None for a world-less task: no world is registered and nothing is mounted
    workspace: Path  # /workspace in docker
    home: Path  # per-trial HOME / state dir; persists across sessions of the trial
    llm: LlmEndpoint | None
    env: dict[str, str]  # allowlisted: world token, meter key; never real provider keys when metered
    # The person the agent works for (the seed's). The world tells every agent through `user_whoami`;
    # a remote product that also keeps a profile of its own is written this persona. None for a world-less task.
    user: Persona | None
    # The world's clock at the start. A boxed harness reads it off its wall clock (px-clock); a remote
    # product is told it, and marks a trial that `requires` it invalid when it cannot be. A world-less
    # task has no world clock: this is the wall clock when the trial started.
    now: AwareDatetime
    requires: frozenset[Capability]

    @property
    def world_mcp(self) -> McpEndpoint:
        """The world's MCP endpoint; raises for a world-less trial, which a world-reading harness cannot run."""
        if self.mcp is None:
            raise ValueError(f"trial {self.trial_id} is world-less: no world MCP is mounted")
        return self.mcp


class TurnInput(Model):
    index: int
    text: str
    new_session: bool
    attachments: tuple[str, ...] = ()  # workspace-relative paths
    origin: RunOrigin = Field(default=RunOrigin.MEMBER, exclude_if=lambda v: v is RunOrigin.MEMBER)
    approval: ApprovalDecision | None = Field(default=None, exclude_if=lambda v: v is None)


class StepKind(StrEnum):
    MESSAGE = "message"
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"
    APPROVAL = "approval"


class Step(Model):
    """Diagnostics only."""

    turn: int
    kind: StepKind
    name: str | None = None
    args: JsonValue = None
    output: str | None = None
    ok: bool | None = None
    t_ms: int


class Usage(Model):
    model: str
    input: int = 0
    output: int = 0
    cache_read: int = 0
    cache_write: int = 0
    reasoning: int = 0
    requests: int = 0
    cost_usd: float | None
    source: CostSource


class TurnStatus(StrEnum):
    COMPLETED = "completed"
    HARNESS_ERROR = "harness_error"
    TIMEOUT = "timeout"


ENVIRONMENT_INTERRUPTION = "environment interruption: "
"""The prefix of a `TurnStatus.HARNESS_ERROR` turn's error when the environment, not the agent, ended the turn: a site
or provider outage. The trial is INVALID without being judged, and retried after `RunOptions.retry_wait_s`."""


class LatencySplit(Model):
    """Where a harness's turn time went, for a harness that can see inside it (ms, each None when
    it could not be observed). A turn's; a trial's is its turns' summed, a row's the p50 of its
    trials'. Only `exit_ms` is agent latency; `answer_ms` shows how long the client took to finish
    after the reply existed, and `settle_ms` is the adapter's bookkeeping after it."""

    server_ms: int | None = None  # the service's own clock: the turn arriving to its final reply
    answer_ms: int | None = None  # prompt submitted to the final reply's timestamp
    exit_ms: int | None = None  # prompt submitted to the client process exiting
    settle_ms: int | None = None  # after the exit: waiting for the service to report the turn's usage


class Timing(Model):
    """How a harness divides a trial's time (`protocols.TrialTiming`): what its agent time measures, and its phases."""

    basis: str  # what `agent_ms` covers for this harness, e.g. "agent-run" or "session-and-agent"
    breakdown: dict[str, float] = {}  # phase -> milliseconds, e.g. {"browser_start": 820.0, "agent": 41000.0}


class TurnOutput(Model):
    status: TurnStatus
    answer: str
    session_id: str | None
    steps: tuple[Step, ...] = ()
    usage: tuple[Usage, ...] = ()
    ttft_ms: int | None = None
    # Agent latency: prompt submitted to final reply, as the harness measured it. None means the
    # runner's own measurement around `turn()` stands; a harness overrides it only to leave out
    # bookkeeping it does after the reply (reading usage back, say), never to trim the agent's work.
    latency_ms: int | None = None
    actual_model: str | None = None
    actual_models: tuple[str, ...] = ()
    error: str | None = None
    flags: frozenset[TrialFlag] = frozenset()  # caveats the harness itself reports for this trial
    split: LatencySplit | None = None
    approvals: tuple[ApprovalAnswer, ...] = ()  # approval cards the product raised this turn, as answered


class TrajectoryStatus(StrEnum):
    OVER_BUDGET = "over_budget"  # parse-only: pre-no-cap evidence keeps its original status
    COMPLETED = "completed"
    HARNESS_ERROR = "harness_error"
    BOOT_ERROR = "boot_error"
    TIMEOUT = "timeout"


class Trajectory(Model):
    schema_version: Literal["parallax.trajectory/1"] = "parallax.trajectory/1"
    status: TrajectoryStatus
    error: str | None = None
    delivered_turns: tuple[TurnInput, ...] = ()
    execution_invalid_reasons: tuple[str, ...] = ()  # runner-recorded infrastructure failures, separate from quality
    answers: tuple[str, ...]  # one per delivered turn ("" if none)
    steps: tuple[Step, ...]
    usage: tuple[Usage, ...]  # meter usage REPLACES harness usage when metered
    actual_models: tuple[str, ...]  # distinct observed models (meter first, else harness claim)
    harness_version: str
    boot_ms: int
    ttft_ms: int | None
    wall_ms: int
    agent_ms: int | None = None  # sum of `turn_latency_ms`
    turn_latency_ms: tuple[int, ...] = ()  # per delivered turn: prompt submitted to final reply
    turn_split: tuple[LatencySplit | None, ...] = ()  # per delivered turn, from harnesses that report one
    approvals: tuple[ApprovalAnswer, ...] = Field(default=(), exclude_if=lambda v: not v)
    extra: JsonObject = {}  # diagnostics: stderr tail, harness-claimed cost, native_web


class ParserId(StrEnum):
    OPENCLAW_JSON = "openclaw_json"
    HERMES_QUIET = "hermes_quiet"
    PLAIN_TEXT = "plain_text"


class ConfigFile(Model):
    path: str  # "home:<rel>" or "workspace:<rel>"
    format: Literal["json", "toml", "yaml"]
    content: JsonObject  # string leaves may contain {placeholders}
    requires_mcp: bool = False
    mcp_content: JsonObject | None = None


class ManifestSpec(Model):
    """harnesses/manifests/<name>.toml

    Placeholders (typed argv substitution, never a shell): {model} {model_provider} {model_name}
    {prompt} {prompt_file} {session} {workspace} {home} {world_url} {world_token} {world_token_env}
    {meter_openai} {meter_anthropic} {meter_key}
    """

    info: HarnessInfo
    protocol: Literal["cli"] = "cli"
    argv_first: tuple[str, ...]  # the first turn of a session
    argv_next: tuple[str, ...] = ()  # cli: continuation; empty => MULTI_TURN may not be declared
    mcp_argv: tuple[str, ...] = ()  # exact argv elements omitted when the trial has no MCP world
    stdin: Literal["prompt", "none"] = "prompt"
    session: Literal["from_output", "preassigned"] = "from_output"  # preassigned: runner mints a uuid {session}
    parser: ParserId | None = None  # required for cli
    env: dict[str, str] = {}
    config: tuple[ConfigFile, ...] = ()
    status_note: str = ""  # e.g. "binary not installed in reference env: unverified"


# ---------------------------------------------------------------------------
# results
# ---------------------------------------------------------------------------


class Verdict(StrEnum):
    PASS = "pass"
    FAIL = "fail"
    UNGRADED = "ungraded"
    INVALID = "invalid"
    NA = "na"


class CheckResult(Model):
    id: str
    kind: CheckKind
    mode: FailureMode
    gate: bool
    advisory: bool
    weight: float
    passed: bool | None  # None => missing evidence
    detail: str  # "mail.sent: 0 rows matched where to~priya (want 1)"


class TrialResult(Model):
    schema_version: Literal["parallax.result/2"] = "parallax.result/2"
    key: str  # fingerprint; resume key
    task_id: str
    task_version: int
    task_hash: str
    harness: str
    harness_version: str
    harness_kind: HarnessKind
    config_hash: str
    model: str | None
    actual_models: tuple[str, ...]
    repeat: int
    verdict: Verdict
    xfail: bool  # report derives XFAIL (fail&xfail) / XPASS (pass&xfail)
    score: float  # weighted fraction of non-advisory checks passed (partial credit)
    invalid_reason: str | None
    na_reason: str | None
    status: TrajectoryStatus | None  # None for NA rows
    checks: tuple[CheckResult, ...]
    usage: tuple[Usage, ...]
    cost_usd: float | None
    cost_source: CostSource
    boot_ms: int | None
    ttft_ms: int | None
    wall_ms: int | None  # the whole trial: setup, every turn, teardown and evidence collection
    agent_ms: int | None = None  # the agent's own time: its turns' latencies summed (None: no turn ran)
    latency_split: LatencySplit | None = None  # its turns' splits summed, when the harness reports them
    judge: tuple[str, ...]
    judge_same_family: bool | None
    web_isolation: WebIsolation
    maturity: Maturity
    split: Split
    area: Area
    lite: bool
    dir: str | None  # trials/<task>/<harness>/<model>/<repeat>/
    flags: frozenset[TrialFlag] = frozenset()
    # Named measurements a harness reports for the trial (e.g. a voice replay's reply latency), absent when none.
    metrics: dict[str, float] = Field(default={}, exclude_if=lambda v: not v)
    # Every field below is absent from a row at its default, so rows written before it existed are unchanged.
    # The physical attempt of this trial: 0 the first, 1 the first retry. Every attempt keeps its row and its dir.
    attempt: int = Field(default=0, exclude_if=lambda v: v == 0)
    # What `agent_ms` measures and how the trial's time divides into phases (`protocols.TrialTiming`).
    timing_basis: str | None = Field(default=None, exclude_if=lambda v: v is None)
    timing_breakdown: dict[str, float] = Field(default={}, exclude_if=lambda v: not v)
    # Spend by component (agent model, browser, proxy); sums to `cost_usd` when every component is known.
    cost_by_component: dict[str, float] = Field(default={}, exclude_if=lambda v: not v)
    # Opaque, secret-free facts the harness reports for the row (`protocols.TrialDetails`), such as a lease's wait.
    details: JsonObject = Field(default={}, exclude_if=lambda v: not v)
    # The identity of the judging policy that graded the row (`judging.JudgePolicy.identity`); rows graded under
    # different identities never pool (`stats.judge_identity`). None for a policy that declares none.
    judge_identity: str | None = Field(default=None, exclude_if=lambda v: v is None)


class RunManifest(Model):
    schema_version: Literal["parallax.run/2"] = "parallax.run/2"
    run_id: str
    created_at: AwareDatetime
    parallax_version: str
    harnesses: tuple[HarnessInfo, ...]
    models: tuple[str | None, ...]
    tasks: tuple[tuple[str, str], ...]  # (id, hash)
    repeats: int
    judge: tuple[str, ...]
    isolation: Isolation
    selection: str
    bootstrap_seed: int = 0
    grading_policy: str  # `semantic.policy_identity()`: which judge configuration graded the run


def fingerprint(
    task_hash: str,
    harness: str,
    harness_version: str,
    config_hash: str,
    model: str | None,
    repeat: int,
    judge: tuple[str, ...],
    parallax_version: str,
    *,
    isolation: Isolation,
    capabilities: frozenset[Capability],
) -> str:
    """Resume key: any input that can change a verdict is part of it. Isolation decides where the
    harness and trusted verify run; the declared capabilities decide NA."""
    payload = [
        task_hash, harness, harness_version, config_hash, model, repeat, list(judge), parallax_version,
        isolation.value, sorted(c.value for c in capabilities),
    ]  # fmt: skip
    return hashlib.sha256(json.dumps(payload, separators=(",", ":")).encode()).hexdigest()[:24]


# `from __future__ import annotations` defers every annotation, so rebuild each model
# once the whole module is defined. Iterating the module keeps a newly added model from
# being forgotten here.
for _obj in list(globals().values()):
    if isinstance(_obj, type) and issubclass(_obj, BaseModel) and _obj is not BaseModel:
        _obj.model_rebuild()
