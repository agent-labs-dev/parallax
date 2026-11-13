# Official browser suites

Parallax exposes two public browser benchmarks as task sources. Each source downloads its pinned dataset
on first use, verifies the SHA-256 digest, and keeps the bytes under the user cache. The source task is
world-less: any harness with `BrowserEvidence` can run it.

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

## Interruption handling

The browser adapter probes the task start URL before the first agent action. A site transport failure,
5xx response, or access block becomes `environment interruption: ...`. That invalid attempt is retryable
with `--retry-invalid`, and does not become a quality failure. The result identity includes the suite,
the pinned judge revision, and the judge model, so results from different judge identities do not pool.
