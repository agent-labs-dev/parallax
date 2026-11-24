# Official browser suites

Parallax exposes two public browser benchmarks as task sources. Each source downloads its pinned dataset
on first use, verifies the SHA-256 digest, and keeps the bytes under the user cache. The source task is
world-less: any harness with `BrowserEvidence` can run it. Two further world-less sources,
`terminal-bench-2` and `tau2`, follow the same fetch-and-pin pattern with their own graders below.

## Setup

Prepare one clean upstream checkout for each judge at the revision listed below. Install that checkout's
own dependencies into its virtual environment. The judge process is started with that environment's
Python and refuses a different Git revision or tracked changes.

```sh
export PARALLAX_OFFICIAL_BU_BENCH_ROOT=/path/to/benchmark
export PARALLAX_OFFICIAL_BU_BENCH_JUDGE_PYTHON=/path/to/benchmark/.venv/bin/python
export PARALLAX_OFFICIAL_ONLINE_MIND2WEB_ROOT=/path/to/Online-Mind2Web
export PARALLAX_OFFICIAL_ONLINE_MIND2WEB_JUDGE_PYTHON=/path/to/Online-Mind2Web/.venv/bin/python
export HF_TOKEN=...
export PARALLAX_JUDGE_BASE_URL=https://openrouter.ai/api/v1
export PARALLAX_JUDGE_API_KEY=...
```

The first suite uses the judge model pinned by its upstream project. The second uses the first model in
`PARALLAX_JUDGE_MODELS`, or `PARALLAX_OFFICIAL_ONLINE_JUDGE_MODEL` when that variable is set. The API
key is read from the environment and is never written into a task or result.

## BU Bench

The source id is `bu-bench` and its policy is `bu-bench-upstream-v1`.

```sh
uv run parallax run bu-bench --harness browser-harness --model provider/model \
  --repeats 1 --retry-invalid 2 --retry-wait 30
```

The encrypted `BU_Bench_V2.enc` dataset is fetched from the pinned upstream commit
`77b4df339fe8fc293fb48896cfa1c73dd743af42`, with SHA-256
`0c014b056192e261b06f04ed6da6dcf37f2135150cf58c87ea839bd4e150e5c3`. The upstream judge model is
`gpt-5.6-luna`.

## Online-Mind2Web

The source id is `online-mind2web` and its policy is `online-mind2web-upstream-v1`.

```sh
uv run parallax run online-mind2web --harness browser-harness --model provider/model \
  --repeats 1 --retry-invalid 2 --retry-wait 30
```

The JSON dataset is pinned to revision `eacad896a84dc5b65e29b0b06e4699ab0544d701`, SHA-256
`7dedf381531d423dc0fc48d21dc1d425655dd75f38b10211a880fa09102f257f`, and Git blob
`e8a5e5a99f2be9eae14f4e4259bd5af562f80da9`. Its judge checkout is pinned to
`b422075b059f0bba670ddda168444eb1d326aa45`.

## Terminal-Bench 2

The source id is `terminal-bench-2` and its policy is `terminal-bench-2-upstream-v1`.

```sh
uv run parallax run terminal-bench-2 --harness <harness> --model provider/model --repeats 1
```

Harbor task directories are imported by `parallax/harbor_tasks.py` as world-less records, because they
have no `[metadata.parallax]` table for the directory loader. The pinned archive of
[harbor-framework/terminal-bench-2](https://github.com/harbor-framework/terminal-bench-2) at commit
`2fd12b88aafdd04a52c298e3940bcb189f9766d6` (89 tasks) is fetched on first use, verified against a
per-file SHA-256 manifest (`efcb17d74def636fc67695b6a2bfb4a6b65945c2e52e5e0eff05e2f28cf839b3`), and
kept in the user cache. Multi-step tasks are rejected by the importer rather than truncated; the
pinned set is single-step.

Grading is structured, not answer-text matching. Parallax owns the task container: for each
trial of a task requiring `live_container`, the runner generates the container name and records
it on the trial before `docker run` (labelled `parallax.trial=<trial id>` and
`parallax.run=<run uuid>`), pulls the pinned image explicitly (`docker pull` of the digest ref,
bounded by `IMAGE_PULL_TIMEOUT_S` at 1800 s, then `docker run -d` on the short timeout), hands
its handle to the harness as the typed `Trial.task_container` field (a manifest harness reads
it as `{task_container}`), and removes it after grading on every path, including a failed start
and a cancelled trial (the removal is shielded, so a second cancellation cannot strand it). The
run uuid is a fresh uuid per run, recorded in the run's `manifest.json` as `run_id`, never the
output basename (which two runs can share); at run start and exit the runner sweeps containers
carrying only that run's `parallax.run` label, so cleanup cannot remove another run's live
container. A harness with `LIVE_CONTAINER` acts inside that container; one without it scores
NA before the run, never a graded 0.

For manifest (CLI agent) harnesses this follows upstream Harbor's installed agents
(`agents/installed/*` in laude-institute/harbor): a manifest may declare a TB2 install step
(`tb2_install`, a script run as root inside the owned task container once per trial, at the
CLI version the manifest already pins) and TB2 agent commands (`tb2_argv_first`/`tb2_argv_next`),
which then run inside that container via `docker exec` as root, with credentials passed per
exec as env rendered from the manifest's existing `env` allowlist, never baked into the image
or written to disk. The manifest's rendered config files are written inside the container
(`home:` under `/root`, `workspace:` under `/app`). Manifests that declare this get
`live_container`. The built-in `codex` (pinned `@openai/codex@0.169.0`) and `claude-code`
(pinned `@anthropic-ai/claude-code@2.1.325`) manifests declare it; `hermes` and `openclaw`
do not, so they (and any non-manifest harness without `live_container`, such as Nebula) score
NA on terminal-bench-2 until they declare a TB2 path.

`storage_mb` is enforced with Docker `--storage-opt size=<N>m` only when the daemon reports a
supported overlay2 backing filesystem. If the driver cannot enforce the limit, Parallax refuses
the task as infrastructure before starting an uncapped container. This host uses overlay2 over
btrfs, and Docker rejects that storage option, so a pinned TB2 task with `storage_mb` is refused
here rather than run without its limit.

Metered TB2 trials require both `--budget USD` and `--reserve USD`. The reservation becomes the
hard per-trial meter cap. The task container receives only the meter key rendered by the
manifest's declared `env` entries on each `docker exec`; provider credentials remain in the
meter service and never enter the task container. The key is revoked when the trial ends and
also expires after its short fallback TTL. The built-in Node agents pin Node `22.14.0` and verify
the SHA-512 integrity of the exact CLI tarball before install; its platform package is an exact
versioned optional dependency of that tarball.

The harness turn is bounded by the task's own agent timeout (`agent.timeout_sec`, recorded in
the task data), matching upstream Harbor: `Trial._run_agent_phase` wraps the agent in a timeout
and raises `AgentTimeoutError`, whose upstream error policy still grades with a valid reward
(`should_grade`, `has_valid_reward`). A timed-out TB2 trial therefore still runs the verifier
on whatever the agent left in the container, and the trial is recorded with its timeout status
and the verifier's reward (pass or fail), not invalidated.

The grader uses only the handle parallax recorded, never a harness-reported name, and a
harness-reported reward is never trusted. The grader, never the harness, runs the verifier: it re-checks the
record's `tests_digest` against the pinned `tests/` in its digest-checked cache, restarts the
owned container (killing every process the agent left, a self-respawning reward writer
included, while keeping its filesystem), then as root removes `/tests` and `/logs/verifier`
wholesale, copies the pinned tests in, and runs `tests/test.sh` there (directly, as upstream
Harbor does) with the container's existing network, which follows the task's `[environment]`.

Reward comes only from the verifier's reward file, `/logs/verifier/reward.json` or
`/logs/verifier/reward.txt`, as upstream Harbor defines them; reward 1 is a pass. A verifier
timeout fails the trial whatever reward was written: upstream Harbor's
`Trial._verify_with_timeout` (`harbor/trial/trial.py` in laude-institute/harbor, the version the
TB2 pin targets) wraps `Verifier.verify()` in `asyncio.wait_for` and raises
`VerifierTimeoutError` before `verify()` reaches its reward-file read, so no reward written
before the kill is ever read. `failure_kind=infrastructure`, which leaves the trial unjudged,
is recorded only when docker itself fails: the daemon is unreachable, the CLI exits 125, or the
image or container is missing per `docker inspect`. After the container has restarted, any
in-container failure (a copy that fills the disk, a broken `sh`, a missing or unparsable reward
file) is the agent's and scores as a failure, never a stdout fallback, and the copy and test
exit codes are recorded in the check detail either way. Residual risk: the agent ran in the
container and could have altered system Python or site-packages that the pinned tests import,
the same exposure as upstream Harbor; the pinned tests themselves, once copied in, cannot be
the agent's copies. Task images named by mutable tag are pinned by digest: the importer records
the registry digest resolved for the pinned set in `PINNED_IMAGE_DIGESTS`, and grading refuses
a container whose image digest differs.

The container is named, limited by the task's `--cpus`, `--memory`, `storage_mb` and
`--pids-limit` (512 by default), and started with Docker's default capability set, as upstream
Harbor uses, plus
`no-new-privileges`: dropping all capabilities breaks `apt-get` in most TB2 test scripts, while
`no-new-privileges` alone was verified to keep `apt-get` working on a Debian image. The oracle
and null controls run through this same path: the oracle runs the pinned `solution/solve.sh`
(as root, bounded by the task's agent timeout) in the runner-owned container and must pass, the
null control runs nothing and the verifier alone must fail. No credential enters the container.

Tests: `tests/core/test_harbor_tasks.py` runs the real verifier in Docker against tiny alpine
fixtures (pass, fail, missing reward file, forged tests and a self-respawning background reward
writer, verifier timeout as an agent failure, timeout cleanup) plus a Debian fixture whose
`test.sh` runs `apt-get` (skipped without network), pure digest-pinning tests for
`_image_ref`/`_check_container_image`, a Docker-marked fixture-manifest test (a shell-command
"agent" solving the tiny task through the TB2 install and `docker exec` path), a Docker-marked
agent-timeout test (a harness that solves then hangs past the task's agent timeout still
verifies and scores the reward), and a Docker-marked runner test (the oracle control
passes and the null control fails on the tiny fixture through the runner, a start failure from
a bad digest cleans up its box and leaves no container labelled with the run's uuid, and a cancelled trial leaves
none), and `tests/core/test_tau2_suites.py` runs the real bridge against a pinned tau2 checkout
on the `mock` domain (`agent_stop` passes, `max_steps` scores 0, a tiny bridge timeout kills
the process group, a failed judge call is captured in the side file, and the serve runtime
drives tools and the simulator against a scripted gateway) plus pure
evidence-validation and preflight tests, and a Docker/network-free runner-loop test with a
scripted harness and a scripted runtime stand-in (upstream exposes no scripted simulator, so
the real-simulator loop is covered only by the skip-gated checkout test). CI's test shards run on GitHub's ubuntu runners, where
the Docker-marked tests run when a daemon answers (they skip with that reason otherwise); the
tau2 bridge tests skip in CI, which provides no pinned tau2 checkout, network judge or checkout
venv, and run wherever `PARALLAX_OFFICIAL_TAU2_ROOT` points at one.

## tau2

The source id is `tau2` and its policy is `tau2-upstream-v1`.

```sh
export PARALLAX_OFFICIAL_TAU2_ROOT=/path/to/tau2-bench
uv run parallax run tau2 --harness <harness> --model provider/model --repeats 1
```

The source covers the text `airline`, `retail` and `telecom` domains (base splits: 50, 114 and 114
tasks) from [sierra-research/tau2-bench](https://github.com/sierra-research/tau2-bench) at commit
`768f3d8fe8528b4f32a5096f9ed008ae50f48ead`. Each domain's `tasks.json`, `split_tasks.json` and policy
file are fetched at run time and verified by the SHA-256 digests recorded in `THIRD_PARTY.md`.

Parallax owns the tau2 trial the way it owns the TB2 container. Each record carries the full
pinned task JSON (`data["task"]`, digest-checked against `data["task_digest"]`), from which the
runner builds the simulation; the task's scenario never enters the agent prompt, which carries
only the domain policy, as upstream. Per trial, parallax runs (in the pinned tau2 environment,
as a serve-mode subprocess like the grading bridge) the domain tool server as the trial's world
MCP server and the upstream user simulator with the task's own scenario, then drives the
multi-turn loop itself: simulator message, harness `turn()`, and so on until an upstream
termination condition (the simulator's stop token, `max_steps`, `too_many_errors`, an agent
stop, or an agent or user error). The simulator model is pinned to the upstream default at this
revision (`gpt-4.1-2025-04-14`, `tau2.config.DEFAULT_LLM_USER`) and runs through the configured
gateway; its cost is recorded as simulator cost (`simulator_cost_usd` in the trial's metrics),
out of the harness's usage. The runner records the transcript itself, from its tool server's
tool log and the turns it drove. The transcript is the full upstream message list: every
assistant tool call is followed, in order, by its `ToolMessage`, and tool-call arguments are
objects, as upstream's `ToolCall` model requires (a string form is rejected).
`termination_reason` is required with no default and takes upstream's `TerminationReason`
values. There is no bare tool-call list and no final database state: the evaluator replays the
transcript itself. Invalid evidence is unjudged with the reason. The grader evaluates only this
parallax-recorded transcript and termination reason (`GradeRequest.runner_recorded`);
harness-reported tau2 evidence is never graded, and that reporting path is deleted.

Grading sends that record through `parallax/tau2_bridge.py` to the pinned checkout, which calls
upstream `evaluate_simulation` with `EvaluationType.ALL`, upstream's default SEMANTIC communicate
scoring and its NL-assertion evaluator; no database, action or communication comparison is
reimplemented in Parallax. Both judges run through the run's configured Parallax judge gateway
under the ids `gpt-5.5` (communicate info) and `gpt-5.4-mini-2026-03-17` (NL assertions), the
upstream pins at this revision. A preflight queries the configured gateway's model
list and refuses to start grading if either judge id is missing; a successful probe is cached
per process, keyed by gateway, the key's SHA-256 fingerprint prefix (never the key itself) and
both judge ids, and failures are not cached, so a bad judge configuration fails before any bridge process or judge spend starts. Only
the judge's endpoint and key reach the bridge, through an explicit env allowlist
(`OPENAI_BASE_URL`, `OPENAI_API_KEY`, from the run's judge config). The bridge subprocess is
bounded by a timeout and its process group is killed on timeout or cancellation. Every judge
call the evaluator makes is recorded as it happens in one channel, the side file, and recorded
on the grade as a priced call, or unpriced when upstream reported no cost, never dropped: a
failed judge call records only its exception class and HTTP status, never the exception
message, and the calls made before a bridge failure still price the grade, while a bridge
failure with no recorded calls records zero calls, not one unpriced call. Bridge stderr reaches
a grade only as the exception class plus a fixed reason, never raw text. Simulator (user-model)
spend is recorded by parallax as simulator cost in the trial's metrics: it never enters the
trial's usage or the grade's calls.

## live-web

`live-web` is Parallax's own browser suite: 53 tasks on public websites (package registries, Wikipedia, arXiv,
GitHub, Hacker News and practice shops), graded by `browser-evidence-v2` like any world-less browser task. No browser
agent under comparison owns it. Rows pool by task id and version, and `tests/core/test_live_web.py` fails if a task
changes without a new version.

Each task's `suite` field places it: `core` (20, the scored set), `dev` (22) and `stretch-dev` (11) for development.
Select a narrower set by task id. Login tasks use the demo logins the practice sites publish on their own login
pages, read from `PARALLAX_SITE_SAUCEDEMO_*`, `PARALLAX_SITE_EXPANDTESTING_*` and `PARALLAX_SITE_PRACTICE_*`
(`USERNAME`, `PASSWORD`); a harness that cannot hold credentials safely is NA on them. `saucedemo-pause` asks for an
order the task does not authorize, so the correct outcome is to stop before placing it; it requires `authorization`,
so a harness that cannot stop is NA on it.

```sh
uv run parallax run source=live-web --harness browser-harness --model openai/gpt-6-luna
```

## Prompts and scores

An Online-Mind2Web task's prompt begins `Start at <website>.`, as the upstream evaluation asks an agent; the
judge receives the bare instruction. The upstream judge's own score becomes the trial `score`, so a BU Bench
rubric met in part keeps its fractional score, while the verdict is the judge's pass or fail.

## Interruption handling

The browser adapter probes the task start URL before the first agent action. A site transport failure,
5xx response, or access block becomes `environment interruption: ...`. That invalid attempt is retryable
with `--retry-invalid`, and does not become a quality failure. The result identity includes the suite,
the pinned judge revision, and the judge model, so results from different judge identities do not pool.
