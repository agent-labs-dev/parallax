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

Grading is structured, not answer-text matching: the task's own pinned `tests/test.sh` runs in the
task environment and writes a reward, and reward 1 is a pass. A harness that keeps the agent's
container live reports that reward in its trial metadata (shared mode). A box harness instead gets a
fresh `--network none` container built from the task image over its final workspace snapshot, with
the pinned `tests/` mounted at `/tests`. Tasks whose tests read live container state outside the
workspace are snapshot-unsafe and score NA for box harnesses rather than failing silently. The
oracle for a task is its pinned `solution/solve.sh`, run in the container before the verifier. Tasks
run with the image, network mode, CPU and memory their `task.toml` declares; no credential enters
the container.

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

Parallax has no LLM user simulator, so the harness drives the multi-turn loop: it receives the
domain policy and task goal as its first turn, runs the pinned simulator internally, and returns one
trial whose metadata carries `{transcript, tool_calls, final_db_state}`. Grading sends that evidence
through `parallax/tau2_bridge.py` to the pinned checkout, which calls upstream
`evaluate_simulation` with `EvaluationType.ALL`; no database, action or communication comparison is
reimplemented in Parallax. The bridge scores communicate info with upstream's substring scorer so
grading makes no second LLM judge call. Simulator (user-model) spend is metered by the harness and
kept in trial metadata: it never enters the trial's usage or the grade's priced calls.

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
