"""Terminal-Bench 2 (Harbor-format) task importer, source and grading policy.

Upstream tasks are Harbor task directories, not Parallax task directories, so they enter through
`parallax.task_sources` as world-less records like the browser suites. The pinned archive is fetched
at run time, verified against a per-file SHA-256 manifest, and kept in the user cache; no dataset
bytes are committed. Grading is structured, not answer-text matching: the grader itself copies the
pinned `tests/` from its digest-checked cache into the harness's still-live task container and runs
`tests/test.sh` there, reading the reward only from the verifier reward file. A harness that cannot
keep a task container live does not declare `live_container` and scores NA. The oracle conformance
path runs the pinned `solution/solve.sh` in a fresh task container, then the verifier; the null path
runs the verifier alone.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import re
import tarfile
import tempfile
import tomllib
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, cast

import httpx
from pydantic import BaseModel, JsonValue

from parallax.extensions import GradeRequest, GradingPolicy, TaskExtension
from parallax.judging import JudgePolicy
from parallax.models import (
    Area,
    Capability,
    Check,
    FailureMode,
    JsonObject,
    Maturity,
    Model,
    Provenance,
    RubricCheck,
    Task,
    TaskRecord,
)
from parallax.semantic import Graded

TERMINAL_BENCH = "terminal-bench-2"
TB2_POLICY = "terminal-bench-2-upstream-v1"
TERMINAL_BENCH_COMMIT = "2fd12b88aafdd04a52c298e3940bcb189f9766d6"
TERMINAL_BENCH_MANIFEST_SHA256 = "efcb17d74def636fc67695b6a2bfb4a6b65945c2e52e5e0eff05e2f28cf839b3"
TERMINAL_BENCH_EXPECTED_COUNT = 89
DEFAULT_PIDS_LIMIT = 512
REWARD_TEXT = "/logs/verifier/reward.txt"
REWARD_JSON = "/logs/verifier/reward.json"

# Registry digests for the pinned set's mutable `alexgshaw/*:20251031` tags, resolved from
# Docker Hub at the pinned commit. An image not listed here (a fixture, a future pin) carries
# no digest pin and is graded without one.
PINNED_IMAGE_DIGESTS: dict[str, str] = {
    "alexgshaw/adaptive-rejection-sampler:20251031": "sha256:985bed39e8448bc95f138b5f766b59003a95c4be171c96af8e76448338f3dd8f",
    "alexgshaw/bn-fit-modify:20251031": "sha256:5aeb11ca6e802b83816c9a31f055ac9d028309a853509400c2e3139ac2d8380d",
    "alexgshaw/break-filter-js-from-html:20251031": "sha256:d7bfb45e47cf10d2e21dc520239215498f4eba9c1ab2d0cd7d647985dddb4fa8",
    "alexgshaw/build-cython-ext:20251031": "sha256:3612a38fadb89a96f74a1a951fb0b0af734198fd160571eeaba6401593234594",
    "alexgshaw/build-pmars:20251031": "sha256:57a82706f7491e0b2d1ff7e927e97ae5339892dd09c47ac43290b4ba1f5abc6f",
    "alexgshaw/build-pov-ray:20251031": "sha256:874baf49341e2853a15a9184e308c7eb632e97f434359fcff2891c6e5ca1b39b",
    "alexgshaw/caffe-cifar-10:20251031": "sha256:5b54d6e8f1b392cdeb819e28215853f500b72b8e08b91270c7a3693bf44d1fe2",
    "alexgshaw/cancel-async-tasks:20251031": "sha256:84c7fae6b256dcc56a350790e2a9715eefc7dad662a9d8e8a472363aa71ef18d",
    "alexgshaw/chess-best-move:20251031": "sha256:bb447f94d9e2a8ed879f85c85a514b213b7418f9fe11fd7b2428a0b0e436e647",
    "alexgshaw/circuit-fibsqrt:20251031": "sha256:29783439f529eaed2145592f15af7a2281161528860392a382c825489030ae3a",
    "alexgshaw/cobol-modernization:20251031": "sha256:593ab9df3d83f771e927888b2d9436b380cdf7ec4d0af2a9f0802d541edebba0",
    "alexgshaw/code-from-image:20251031": "sha256:2be9cdb746ed18c1b2b1404032efae0b2235a1b8fc479ef17730c95409d89f76",
    "alexgshaw/compile-compcert:20251031": "sha256:f316947b7be23ad0d187320126eb1cbb5685f48342c6543fdd3ac839aa9e1a7f",
    "alexgshaw/configure-git-webserver:20251031": "sha256:9e48389b917fd4650dda1f64406bd7198bbf20dc1c6b0e5c4d81a3c16369f88c",
    "alexgshaw/constraints-scheduling:20251031": "sha256:567ce5a189f8d11ac461790876e934cc7af38391baf89a78f95f4dafc1fec3b0",
    "alexgshaw/count-dataset-tokens:20251031": "sha256:9c513c4ba342bd95be501f6f5eadd4806a2b18749c87c251615825100130232d",
    "alexgshaw/crack-7z-hash:20251031": "sha256:0f4453abd774c5a3d3d7e66ba28fae88ec2e49ada3a993b324ebc16c348666d3",
    "alexgshaw/custom-memory-heap-crash:20251031": "sha256:1895d7d6240d5fb2ec774d6bfdd1057d170f8d199c15c582e4585d3c63802557",
    "alexgshaw/db-wal-recovery:20251031": "sha256:0e33ea5ec823975d1bd6c3778395c9f94251dd88f571146057bff6adb7e4594e",
    "alexgshaw/distribution-search:20251031": "sha256:1448f7aa1c6080488251d00350e8e90477858989b1bec0accc866d7b3220738a",
    "alexgshaw/dna-assembly:20251031": "sha256:d1adf6835f1dd91205ba70e452c699d0aea601010038e5617f370716efb50569",
    "alexgshaw/dna-insert:20251031": "sha256:4dd8760694e355de85ecf03605cb487ccdb33322f6afad513d2920829baa33b2",
    "alexgshaw/extract-elf:20251031": "sha256:6932e4cb318464307eacd497ef8dc617eaf551b6a90231f815ec0b911895cfed",
    "alexgshaw/extract-moves-from-video:20251031": "sha256:14a9dcca7d08ce53c5a0ed9fbd663a996ef28f207c199ed69d9ccf6064f8b184",
    "alexgshaw/feal-differential-cryptanalysis:20251031": "sha256:bea93bafe9eab601ca58729f5735a13f8339f52055322d1cf840e3742b54a287",
    "alexgshaw/feal-linear-cryptanalysis:20251031": "sha256:ea78749a8422c6228d26c8753f45906a6a2df089906764b7e3c3e27e8f718ee7",
    "alexgshaw/filter-js-from-html:20251031": "sha256:92acda0f124b988036a6f426ce0bc47fac19f5efe9fc5e6ea3ea52ccb075d0a4",
    "alexgshaw/financial-document-processor:20251031": "sha256:ef6c9cfaaf14cdd200163008a188d5baa9f626f3bfb8d3e7d6f30c08518e6251",
    "alexgshaw/fix-code-vulnerability:20251031": "sha256:cac325252991f823713b2d0441502972901dd782bd67f66c03d9b1e410dac5c0",
    "alexgshaw/fix-git:20251031": "sha256:61e431c00c58df652287aadce5457634d9f9330cfdd153ebdf2802df0d540119",
    "alexgshaw/fix-ocaml-gc:20251031": "sha256:66a1e2293076c9ef27a118611b3089a6284fdf467fd4675592d0cce7d11e5b0b",
    "alexgshaw/gcode-to-text:20251031": "sha256:0979ef40c6a3e8c4e7ab5b6c2524c74625a84799e15cd45dd98dcf83b11efd4d",
    "alexgshaw/git-leak-recovery:20251031": "sha256:62160e522a4238b8bcd522411ea97b5b200f811e5899d34947e66809bef7e586",
    "alexgshaw/git-multibranch:20251031": "sha256:5bc449921c138295f6ddd8dca2977319fee2ce523811dbe56cec4ca663385fb5",
    "alexgshaw/gpt2-codegolf:20251031": "sha256:537e7bd26b02db5c761230515fa09dc05f89403c6737fbdae4b5793ebc2c7066",
    "alexgshaw/headless-terminal:20251031": "sha256:eb7e209672bf6cef2785fafd9e13509b10626c327bcc2b37f5bf40ca83eaf3aa",
    "alexgshaw/hf-model-inference:20251031": "sha256:f96dd7d3c85c7257b404fd63ba8d24ced2478cd90232c20e297c372f953fa607",
    "alexgshaw/install-windows-3.11:20251031": "sha256:c6695097316ef8810abce0545e347e65354aef9d51d4f7f662c3242e742f6a3e",
    "alexgshaw/kv-store-grpc:20251031": "sha256:3399400800dcb207634daa42bc1b052e831e285cc9d221eea66c47bc0fc79791",
    "alexgshaw/large-scale-text-editing:20251031": "sha256:719adca3f1388220546ce6a155eee56eff3c4fe318183100320606a210f6b59c",
    "alexgshaw/largest-eigenval:20251031": "sha256:cb9e3e3b09d0783f3bbcfeec8d77bc32d3791204fb2be6650b19be4fbc082b2e",
    "alexgshaw/llm-inference-batching-scheduler:20251031": "sha256:19e79aa49be4e55dccca1dde966515ebe6852314b6010bcb9230afb48b996774",
    "alexgshaw/log-summary-date-ranges:20251031": "sha256:cbeb6ba905c2fec294f16cd5e16e3ea7f2e04d38ac2484d51a11de262aa7dc51",
    "alexgshaw/mailman:20251031": "sha256:04fac73db7e2b4f49d43f09fbc2266a94d70131f07efa1393d370abc059f4094",
    "alexgshaw/make-doom-for-mips:20251031": "sha256:80a9459f1b94b692ea8346f68e6b864f6c4d984fbdf6ffd68ad332308300c2ec",
    "alexgshaw/make-mips-interpreter:20251031": "sha256:082fc8821b317f30fdfbf8d08d528874ce331c03e8083aef0406d48cdd7132a2",
    "alexgshaw/mcmc-sampling-stan:20251031": "sha256:073fc36a3347b4f6b43bd6e98339542c41ed531c771af8bee40ba10f00cfabaf",
    "alexgshaw/merge-diff-arc-agi-task:20251031": "sha256:bfc2a235f2ceea64ffb1ffc5529c2acebf570bff61f3668f0e5669ef570c17d4",
    "alexgshaw/model-extraction-relu-logits:20251031": "sha256:52fe1f089f38650f0dc22d7531ee6e01ebd526de1b349aaa2ecb331dca9fabce",
    "alexgshaw/modernize-scientific-stack:20251031": "sha256:64e69cee13bf6b0b9016e735b51891bce996a60f1e2d1a005bf12c949e221d71",
    "alexgshaw/mteb-leaderboard:20251031": "sha256:968de9456bfc53728f7df0cb6c9ce33e17bd0e5563ddfe031e6ed2964965aecf",
    "alexgshaw/mteb-retrieve:20251031": "sha256:99dd676a21fd3124b7b8164cd93e8f0c51f1fdab2f84a831c61049eff78d063d",
    "alexgshaw/multi-source-data-merger:20251031": "sha256:8b32782078ff7383a1b4e5d3cecca8ce287e30f50bb4c0e5db009a18064e666e",
    "alexgshaw/nginx-request-logging:20251031": "sha256:693e64431a0dbf45952e8b91a298d54fd60247e501c6623fb8fa3ebc07fc3d3d",
    "alexgshaw/openssl-selfsigned-cert:20251031": "sha256:4c948a4e630af2435ae0a19108fc0814a946ac2fa29a512469e0fc77b38c8c12",
    "alexgshaw/overfull-hbox:20251031": "sha256:67fe0ccc0ff2305d6fcb3652977d377b901bb7bd96904b6765835935574992e1",
    "alexgshaw/password-recovery:20251031": "sha256:15fcd84bf3b06270ee148211cc6da6599b13ec2f41ac1d0894cf75c2225872ee",
    "alexgshaw/path-tracing-reverse:20251031": "sha256:7911e42479d8f6d8af1248bc52442a0efbc5a33e3ecf535fdc20d252a2c64142",
    "alexgshaw/path-tracing:20251031": "sha256:4b44725102cd627dfa895191a5a30d16dae3c5623bb43da6459f79135db6b482",
    "alexgshaw/polyglot-c-py:20251031": "sha256:0f1c3b7816d70cf5551573fd6aeef76893f2ae3000be2419997b6871b5d987ed",
    "alexgshaw/polyglot-rust-c:20251031": "sha256:3ba84f0e138a4b65e58c53f920a0fc852571e6402901c3bdbb876e9dab6e6c5e",
    "alexgshaw/portfolio-optimization:20251031": "sha256:93461db3cec697c31585941a3b9387c1398495aa1ff0aee2ac752c7230b50ef8",
    "alexgshaw/protein-assembly:20251031": "sha256:94de701b57e7ecfdbada6044f8534876e4b58874f8f313184dadc4d7533e0818",
    "alexgshaw/prove-plus-comm:20251031": "sha256:e26741d01681a9da1beeff8b7b7b65fe2b921b8b122a306a3707a81ebd051f73",
    "alexgshaw/pypi-server:20251031": "sha256:d18bb30f47c7dcaa3acdbc31ac7413c98eadca2ea16540bbd380f2f063b95276",
    "alexgshaw/pytorch-model-cli:20251031": "sha256:c996884b5fb5fb2c0c07d67da2c4f974f271f07234ed28e1a868240375c57a67",
    "alexgshaw/pytorch-model-recovery:20251031": "sha256:83efba8d1a163dfe62694789e271ed55010509b6deabbf2d7d6472b40ca8d69a",
    "alexgshaw/qemu-alpine-ssh:20251031": "sha256:541e8214a8c4faa7b556531b899d45da21048fa2ca6dd328d8cb6c4cffa8e964",
    "alexgshaw/qemu-startup:20251031": "sha256:1a666e049db553c5e713830b1f5de1953210ab7c99212d006bb4e46cbc6b6d04",
    "alexgshaw/query-optimize:20251031": "sha256:28de7da87004eab5b29bd5eaf1823579bc5b73ccea4b80349a5623185770da14",
    "alexgshaw/raman-fitting:20251031": "sha256:401fa95d5d491f44a473eda9d2c52930f9565cc889c796eabcb6e20fec26852e",
    "alexgshaw/regex-chess:20251031": "sha256:e3b6d61d2d3930fbbdc5f7bf091a7f8b7f6125e1951ef8a7dbe165b1ef62942c",
    "alexgshaw/regex-log:20251031": "sha256:90101b2e815323a8da20528a1439bebc407eb9761c9c68a3d557730856c878e9",
    "alexgshaw/reshard-c4-data:20251031": "sha256:92d209e704785e17ad6b3f740e813dd72bc7ac33982ce435d197ab9d890eac2a",
    "alexgshaw/rstan-to-pystan:20251031": "sha256:b23d48839408524326095c15ada4145c772b87646cf299022ff1f58c97baba91",
    "alexgshaw/sam-cell-seg:20251031": "sha256:d76bdeef19d8113f4094013249c982eca93e62e46130484846269b2d378879d0",
    "alexgshaw/sanitize-git-repo:20251031": "sha256:4b5234da5bb0d67f3b0bf8db40a2883c07a5219f62b64c2bf9ff1ac84cd0f672",
    "alexgshaw/schemelike-metacircular-eval:20251031": "sha256:b485be38bf80d56c1a4cd9da95bbf7ec129dc03f6786b86c0525518a96f6d1db",
    "alexgshaw/sparql-university:20251031": "sha256:92fa4304e51167f9fb4466144eba33ef9272a7df0ac1cab9fc3986bccd1ab708",
    "alexgshaw/sqlite-db-truncate:20251031": "sha256:aabac93c93bd1f310e6a6fb893911d7735026ed18491c72133c9196a09092ca4",
    "alexgshaw/sqlite-with-gcov:20251031": "sha256:f986820b00c74ce75db7288719d364df313fde7c7917bea6eb6352a08431a89f",
    "alexgshaw/torch-pipeline-parallelism:20251031": "sha256:3cb7b39d86f1704b20e668851da0b08b34b3270c01d583f136d65190984a8d1b",
    "alexgshaw/torch-tensor-parallelism:20251031": "sha256:c50ac169e11465f41b49749fe87e4487f24d31a0c39c0906cd3242cc5499c690",
    "alexgshaw/train-fasttext:20251031": "sha256:3c77da9617e7fa577de04aef73fb1ac3114be32d7ffef72609c83929d842b157",
    "alexgshaw/tune-mjcf:20251031": "sha256:56c197739db2db79562384581fcc9d3d71e6c171bf052104b1a7ee0282e7c3cf",
    "alexgshaw/video-processing:20251031": "sha256:a2c0f39e3ab04e67ac6e49cad9167bb0d987babe32511768c01aa0c6905a875f",
    "alexgshaw/vulnerable-secret:20251031": "sha256:61ebb40454dd103aa2f7e71ad6dafd91cf2b301e6bb07e69d5b472412d1ee15b",
    "alexgshaw/winning-avg-corewars:20251031": "sha256:ad7a6b1c53aed59e10916c692aa7e2ae00042a4283187430aee1e3e79f72d5c6",
    "alexgshaw/write-compressor:20251031": "sha256:3618e1f8a997b437c09cd3dbaac736705809c05f6f12f584b65722175970ebe1",
}


@dataclass(frozen=True)
class TerminalBenchSpec:
    source: str
    policy: str
    upstream_commit: str
    archive_url: str
    manifest_sha256: str
    expected_count: int


TERMINAL_BENCH_SPEC = TerminalBenchSpec(
    source=TERMINAL_BENCH,
    policy=TB2_POLICY,
    upstream_commit=TERMINAL_BENCH_COMMIT,
    archive_url=f"https://github.com/harbor-framework/terminal-bench-2/archive/{TERMINAL_BENCH_COMMIT}.tar.gz",
    manifest_sha256=TERMINAL_BENCH_MANIFEST_SHA256,
    expected_count=TERMINAL_BENCH_EXPECTED_COUNT,
)


class HarborTaskError(RuntimeError):
    """A Harbor task directory cannot be imported, or its verifier cannot run."""


class HarborTaskSpec(Model):
    """The typed subset of an upstream Harbor `task.toml` the importer and verifier need."""

    upstream_id: str
    instruction: str
    docker_image: str | None = None
    docker_image_digest: str | None = None
    dockerfile: str | None = None
    network_mode: Literal["no-network", "public"] = "public"
    cpus: float = 2
    memory_mb: int = 4096
    pids_limit: int = DEFAULT_PIDS_LIMIT
    verifier_timeout_s: int = 300
    steps: tuple[str, ...] = ()
    tests_digest: str = ""


def cache_root() -> Path:
    return Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "parallax" / "official-suites"


def _tree_digest(root: Path) -> str:
    lines = []
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        lines.append(f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.relative_to(root).as_posix()}")
    return hashlib.sha256(("\n".join(lines) + ("\n" if lines else "")).encode()).hexdigest()


def tests_digest(tests_dir: Path) -> str:
    """SHA-256 over the sorted per-file digests of a task's pinned `tests/` tree."""
    return _tree_digest(tests_dir)


def _manifest_digest(root: Path) -> str:
    return _tree_digest(root)


def _image_digest(image: str | None) -> str | None:
    if not image:
        return None
    if "@sha256:" in image:
        return "sha256:" + image.rsplit("@sha256:", 1)[1]
    return PINNED_IMAGE_DIGESTS.get(image)


def _image_ref(spec: HarborTaskSpec) -> str:
    image = spec.docker_image
    if not image:
        raise HarborTaskError("task has no docker_image to start a container from")
    if spec.docker_image_digest and "@" not in image:
        repo = image.rsplit(":", 1)[0] if ":" in image.rsplit("/", 1)[-1] else image
        return f"{repo}@{spec.docker_image_digest}"
    return image


def parse_task_dir(task_dir: Path) -> HarborTaskSpec:
    """Parse one Harbor task directory; multi-step tasks are rejected loudly, never truncated."""
    task_dir = Path(task_dir)
    toml_path = task_dir / "task.toml"
    instruction_path = task_dir / "instruction.md"
    if not toml_path.is_file() or not instruction_path.is_file():
        raise HarborTaskError(f"{task_dir}: a Harbor task needs task.toml and instruction.md")
    try:
        raw = tomllib.loads(toml_path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise HarborTaskError(f"{task_dir}: task.toml cannot be read: {exc}") from exc
    steps = raw.get("steps") or []
    if steps:
        raise HarborTaskError(f"{task_dir}: multi-step Harbor tasks are not supported by this importer")
    instruction = instruction_path.read_text(encoding="utf-8").strip()
    if not instruction:
        raise HarborTaskError(f"{task_dir}: instruction.md is empty")

    def table(key: str) -> dict[str, Any]:
        value = raw.get(key)
        return value if isinstance(value, dict) else {}

    task_table = table("task")
    name = str(task_table.get("name") or task_dir.name)
    upstream_id = name.rsplit("/", 1)[-1].strip() or task_dir.name
    environment = table("environment")
    verifier = table("verifier")
    network = environment.get("network_mode")
    if network is None:
        network = "public" if environment.get("allow_internet", True) else "no-network"
    if network == "allowlist":
        # No egress proxy exists for task containers, so an allowlist cannot be enforced; refuse
        # the task rather than widen it to public network.
        raise HarborTaskError(f"{task_dir}: network_mode 'allowlist' is not supported")
    if network not in {"no-network", "public"}:
        raise HarborTaskError(f"{task_dir}: unsupported network_mode {network!r}")
    dockerfile = "environment/Dockerfile" if (task_dir / "environment" / "Dockerfile").is_file() else None
    tests_dir = task_dir / "tests"
    if not (tests_dir / "test.sh").is_file():
        raise HarborTaskError(f"{task_dir}: missing tests/test.sh, the upstream verifier entry point")
    if not (task_dir / "solution" / "solve.sh").is_file():
        raise HarborTaskError(f"{task_dir}: missing solution/solve.sh, the upstream oracle")
    image = environment.get("docker_image")
    return HarborTaskSpec(
        upstream_id=upstream_id,
        instruction=instruction,
        docker_image=image,
        docker_image_digest=_image_digest(image if isinstance(image, str) else None),
        dockerfile=dockerfile,
        network_mode=cast(Literal["no-network", "public"], network),
        cpus=float(environment.get("cpus", 2)),
        memory_mb=int(environment.get("memory_mb", 4096)),
        pids_limit=int(environment.get("pids_limit", DEFAULT_PIDS_LIMIT)),
        verifier_timeout_s=int(float(verifier.get("timeout_sec", 300))),
        tests_digest=tests_digest(tests_dir),
    )


def _task_id(upstream_id: str, used: set[str]) -> str:
    stem = re.sub(r"[^a-z0-9]+", "-", upstream_id.lower()).strip("-") or "task"
    candidate = f"{TERMINAL_BENCH}-{stem}"
    if len(candidate) > 64:
        digest = hashlib.sha256(upstream_id.encode()).hexdigest()[:10]
        candidate = f"{candidate[:53].rstrip('-')}-{digest}"
    if candidate in used:
        digest = hashlib.sha256(upstream_id.encode()).hexdigest()[:10]
        candidate = f"{candidate[:53].rstrip('-')}-{digest}"
    used.add(candidate)
    return candidate


def _record(spec: HarborTaskSpec, task_dir: Path, used: set[str]) -> TaskRecord:
    data: JsonObject = {
        "upstream_id": spec.upstream_id,
        "task_dir": str(task_dir),
        "docker_image": spec.docker_image,
        "docker_image_digest": spec.docker_image_digest,
        "dockerfile": spec.dockerfile,
        "network_mode": spec.network_mode,
        "cpus": spec.cpus,
        "memory_mb": spec.memory_mb,
        "pids_limit": spec.pids_limit,
        "verifier_timeout_s": spec.verifier_timeout_s,
        "tests_digest": spec.tests_digest,
    }
    return TaskRecord(
        id=_task_id(spec.upstream_id, used),
        version=1,
        area=Area.CODE,
        summary=f"{TERMINAL_BENCH} task {spec.upstream_id}",
        prompt=spec.instruction,
        grading=TB2_POLICY,
        requires=frozenset({Capability.WORKSPACE, Capability.SHELL, Capability.LIVE_CONTAINER}),
        maturity=Maturity.REGRESSION,
        provenance=Provenance.CONVERTED,
        data=data,
    )


def records_from_root(root: Path) -> list[TaskRecord]:
    task_dirs = sorted(p for p in Path(root).iterdir() if p.is_dir() and (p / "task.toml").is_file())
    used: set[str] = set()
    return [_record(parse_task_dir(path), path, used) for path in task_dirs]


def _extract_root(archive: Path, cache: Path) -> Path:
    destination = cache / f"{TERMINAL_BENCH}-{TERMINAL_BENCH_COMMIT}-{TERMINAL_BENCH_MANIFEST_SHA256}"
    if destination.is_dir():
        if _manifest_digest(destination) == TERMINAL_BENCH_MANIFEST_SHA256:
            return destination
        raise HarborTaskError(f"{TERMINAL_BENCH}: cached task tree failed its manifest digest")
    cache.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=f".{TERMINAL_BENCH}-", dir=cache) as temporary:
        staging = Path(temporary) / "tree"
        staging.mkdir()
        with tarfile.open(archive, "r:gz") as bundle:
            for member in bundle.getmembers():
                target = (staging / member.name).resolve()
                if not target.is_relative_to(staging.resolve()):
                    raise HarborTaskError(f"{TERMINAL_BENCH}: archive member escapes its root: {member.name}")
            bundle.extractall(staging, filter="data")
        children = [p for p in staging.iterdir() if p.is_dir()]
        if len(children) != 1:
            raise HarborTaskError(f"{TERMINAL_BENCH}: archive does not hold one task tree")
        tree = children[0]
        if _manifest_digest(tree) != TERMINAL_BENCH_MANIFEST_SHA256:
            raise HarborTaskError(f"{TERMINAL_BENCH}: task tree failed its manifest digest")
        os.replace(tree, destination)
    return destination


def fetch_terminal_bench(*, cache: Path | None = None, transport: httpx.BaseTransport | None = None) -> Path:
    """Download the pinned TB2 archive on first use and return its verified task tree."""
    root = cache or cache_root()
    destination = root / f"{TERMINAL_BENCH}-{TERMINAL_BENCH_COMMIT}-{TERMINAL_BENCH_MANIFEST_SHA256}"
    if destination.is_dir() and _manifest_digest(destination) == TERMINAL_BENCH_MANIFEST_SHA256:
        return destination
    root.mkdir(parents=True, exist_ok=True)
    archive = root / f"{TERMINAL_BENCH}-{TERMINAL_BENCH_COMMIT}.tar.gz"
    if not archive.is_file():
        try:
            with httpx.Client(transport=transport, follow_redirects=True, timeout=120.0) as client:
                response = client.get(TERMINAL_BENCH_SPEC.archive_url)
                response.raise_for_status()
                data = response.content
        except httpx.HTTPError as exc:
            raise HarborTaskError(f"{TERMINAL_BENCH}: dataset download failed: {exc}") from exc
        fd, temporary = tempfile.mkstemp(prefix=f".{archive.name}.", dir=root)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(data)
            os.replace(temporary, archive)
        except BaseException:
            Path(temporary).unlink(missing_ok=True)
            raise
    return _extract_root(archive, root)


def terminal_bench_tasks(root: Path | None = None) -> Iterable[TaskRecord]:
    tree = Path(root) if root is not None else fetch_terminal_bench()
    records = records_from_root(tree)
    if root is None and len(records) != TERMINAL_BENCH_EXPECTED_COUNT:
        raise HarborTaskError(f"{TERMINAL_BENCH}: expected {TERMINAL_BENCH_EXPECTED_COUNT} tasks, got {len(records)}")
    return records


# ---------------------------------------------------------------------------
# containers parallax starts, and the verifier it runs in a live container
# ---------------------------------------------------------------------------


async def _docker(*argv: str, timeout: float | None = None) -> tuple[int | None, str, str]:
    process = await asyncio.create_subprocess_exec(
        "docker", *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    try:
        out, err = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except TimeoutError:
        process.kill()
        await process.wait()
        raise
    return process.returncode, out.decode(errors="replace"), err.decode(errors="replace")


def _spec_from_data(data: JsonObject) -> HarborTaskSpec:
    task_dir = data.get("task_dir")
    if not isinstance(task_dir, str) or not Path(task_dir).is_dir():
        raise HarborTaskError("task data needs the pinned task_dir from the digest-checked cache")
    spec = parse_task_dir(Path(task_dir))
    recorded = data.get("tests_digest")
    if recorded != spec.tests_digest:
        raise HarborTaskError(
            f"tests_digest mismatch at grade time: record has {recorded!r}, pinned tests hash to {spec.tests_digest}"
        )
    recorded_digest = data.get("docker_image_digest")
    if recorded_digest != spec.docker_image_digest:
        raise HarborTaskError("docker_image_digest mismatch at grade time: the pinned image digest changed")
    return spec


async def _check_container_image(container: str, spec: HarborTaskSpec) -> None:
    if not spec.docker_image_digest:
        return
    code, image_id, err = await _docker("inspect", "--format", "{{.Image}}", container)
    if code != 0:
        raise HarborTaskError(f"cannot inspect container {container!r}: {err.strip()[-300:]}")
    code, out, err = await _docker("image", "inspect", "--format", "{{json .RepoDigests}}", image_id.strip())
    if code != 0:
        raise HarborTaskError(f"cannot inspect the container image: {err.strip()[-300:]}")
    try:
        digests = [str(item).rsplit("@", 1)[-1] for item in json.loads(out or "[]")]
    except (ValueError, TypeError) as exc:
        raise HarborTaskError("cannot read the container image digests") from exc
    if (
        spec.docker_image_digest not in digests
        and f"sha256:{image_id.strip().removeprefix('sha256:')}" != spec.docker_image_digest
    ):
        # A locally built image has no RepoDigests; its image id is its digest.
        raise HarborTaskError(
            f"container image digest mismatch: pinned {spec.docker_image_digest}, container runs {digests or image_id.strip()}"
        )


async def run_verifier(container: str, data: JsonObject) -> dict[str, JsonValue]:
    """Copy the pinned `tests/` into the live container and run `tests/test.sh` there.

    Reward comes only from `/logs/verifier/reward.json` or `reward.txt` (upstream Harbor's
    `EnvironmentPaths`). Copy and test exit codes are recorded, never swallowed; a missing or
    unparsable reward file is a failed verifier outcome, not a pass and not a stdout guess.
    """
    spec = _spec_from_data(data)
    tests = Path(str(data["task_dir"])) / "tests"
    code, out, err = await _docker("inspect", "--format", "{{.State.Running}}", container)
    if code != 0 or out.strip() != "true":
        raise HarborTaskError(f"container {container!r} is not a live task container: {err.strip()[-300:]}")
    await _check_container_image(container, spec)
    timeout = spec.verifier_timeout_s
    code, _out, err = await _docker(
        "exec", container, "sh", "-c", f"rm -f {REWARD_TEXT} {REWARD_JSON}; mkdir -p /logs/verifier /tests"
    )
    if code != 0:
        return {
            "reward": None,
            "copy_exit": code,
            "test_exit": None,
            "error": f"preparing /tests failed: {err.strip()[-300:]}",
        }
    code, _out, err = await _docker("cp", f"{tests}/.", f"{container}:/tests")
    if code != 0:
        return {
            "reward": None,
            "copy_exit": code,
            "test_exit": None,
            "error": f"copying pinned tests failed (exit {code}): {err.strip()[-300:]}",
        }
    # Upstream Harbor executes the test script directly (its shebang chooses the shell).
    command = (
        f"mkdir -p /logs/verifier; chmod +x /tests/test.sh; "
        f"timeout -s KILL {timeout} /tests/test.sh > /logs/verifier/test-stdout.txt 2>&1"
    )
    try:
        test_exit, _out, err = await _docker("exec", container, "sh", "-c", command, timeout=timeout + 30)
    except TimeoutError:
        return {"reward": None, "copy_exit": 0, "test_exit": None, "error": f"verifier timed out after {timeout}s"}
    if test_exit in (124, 137):
        return {"reward": None, "copy_exit": 0, "test_exit": test_exit, "error": f"verifier timed out after {timeout}s"}
    code, out, err = await _docker(
        "exec", container, "sh", "-c",
        f"if [ -f {REWARD_JSON} ]; then echo json; cat {REWARD_JSON}; elif [ -f {REWARD_TEXT} ]; then echo text; cat {REWARD_TEXT}; else exit 3; fi",
    )  # fmt: skip
    if code != 0:
        return {
            "reward": None,
            "copy_exit": 0,
            "test_exit": test_exit,
            "error": f"verifier wrote no reward file (test exit {test_exit})",
        }
    lines = out.splitlines()
    kind, body = (lines[0].strip(), "\n".join(lines[1:]).strip()) if lines else ("", "")
    try:
        if kind == "json":
            parsed = json.loads(body)
            value = parsed.get("reward") if isinstance(parsed, dict) else parsed
            if not isinstance(value, (int, float)) or not math.isfinite(float(value)):
                raise ValueError(f"non-numeric reward {value!r}")
            reward = float(value)
        else:
            reward = float(body)
            if not math.isfinite(reward):
                raise ValueError(f"non-finite reward {body!r}")
    except (ValueError, TypeError):
        return {
            "reward": None,
            "copy_exit": 0,
            "test_exit": test_exit,
            "error": f"verifier reward file is unparsable: {body[:200]!r} (test exit {test_exit})",
        }
    return {
        "reward": reward,
        "copy_exit": 0,
        "test_exit": test_exit,
        "reward_file": REWARD_JSON if kind == "json" else REWARD_TEXT,
    }


async def start_task_container(data: JsonObject, *, name: str | None = None) -> str:
    """Start one named, resource-limited task container; the caller removes it by name."""
    spec = _spec_from_data(data)
    container = name or f"parallax-tb2-{uuid.uuid4().hex[:12]}"
    argv = [
        "run", "-d", "--name", container,
        "--cpus", str(spec.cpus), "--memory", f"{spec.memory_mb}m",
        "--pids-limit", str(spec.pids_limit),
        "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
    ]  # fmt: skip
    if spec.network_mode == "no-network":
        argv += ["--network", "none"]
    # `public` uses docker's default bridge.
    argv += [_image_ref(spec), "sh", "-c", "while true; do sleep 3600; done"]
    code, out, err = await _docker(*argv)
    if code != 0:
        await remove_container(container)
        raise HarborTaskError(f"starting task container failed (exit {code}): {err.strip()[-300:]}")
    return container


async def remove_container(container: str) -> None:
    """Remove a container parallax started, by name, on every exit path."""
    await _docker("rm", "-f", container)


async def _exec(container: str, command: str) -> tuple[int | None, str]:
    code, out, err = await _docker("exec", container, "sh", "-c", command)
    return code, (err or out).strip()[-300:]


async def oracle_conformance(data: JsonObject) -> dict[str, JsonValue]:
    """Run `solution/solve.sh` in a fresh task container, then the verifier; removed either way."""
    container = await start_task_container(data)
    try:
        solution = Path(str(data["task_dir"])) / "solution"
        code, _out, err = await _docker("cp", f"{solution}/.", f"{container}:/solution")
        if code != 0:
            return {"passed": False, "solve_exit": code, "error": f"copying solution failed: {err.strip()[-300:]}"}
        solve_exit, detail = await _exec(container, "chmod +x /solution/solve.sh; /solution/solve.sh")
        outcome = await run_verifier(container, data)
        reward = outcome.get("reward")
        return {
            "passed": reward == 1.0, "reward": reward, "solve_exit": solve_exit,
            "test_exit": outcome.get("test_exit"), "copy_exit": outcome.get("copy_exit"),
            "error": outcome.get("error") or (detail if solve_exit not in (0, None) else None),
        }  # fmt: skip
    finally:
        await remove_container(container)


async def null_conformance(data: JsonObject) -> dict[str, JsonValue]:
    """Run only the verifier in a fresh task container (no solution); removed either way."""
    container = await start_task_container(data)
    try:
        outcome = await run_verifier(container, data)
        reward = outcome.get("reward")
        return {
            "passed": reward == 1.0, "reward": reward, "solve_exit": None,
            "test_exit": outcome.get("test_exit"), "copy_exit": outcome.get("copy_exit"),
            "error": outcome.get("error"),
        }  # fmt: skip
    finally:
        await remove_container(container)


# ---------------------------------------------------------------------------
# grading: the task's own tests/test.sh in the live container, reward 1 passes
# ---------------------------------------------------------------------------


def _checks(_task: Task) -> tuple[Check, ...]:
    return (
        RubricCheck(
            id="upstream_verifier",
            kind="rubric",
            mode=FailureMode.WRONG_OUTPUT,
            items=("The pinned task's own tests/test.sh reports reward 1 in the task environment.",),
        ),
    )


def _result(check: Check, passed: bool | None, detail: str) -> Any:
    from parallax.models import CheckResult

    return CheckResult(
        id=check.id, kind=check.kind, mode=check.mode, gate=check.gate, advisory=False,
        weight=check.weight, passed=passed, detail=detail,
    )  # fmt: skip


def _unjudged(task: Task, detail: str) -> Graded:
    return Graded(checks=tuple(_result(check, None, detail) for check in _checks(task)))


def _graded(task: Task, reward: float, outcome: dict[str, JsonValue]) -> Graded:
    detail = (
        f"upstream verifier reward={reward:.6g}; reward_file={outcome.get('reward_file')}; "
        f"test_exit={outcome.get('test_exit')}; copy_exit={outcome.get('copy_exit')}; revision={TERMINAL_BENCH_COMMIT}"
    )
    return Graded(
        checks=tuple(_result(check, reward == 1.0, detail) for check in _checks(task)),
        judge_identity=f"{TB2_POLICY}:{TERMINAL_BENCH}:verifier-{TERMINAL_BENCH_COMMIT}",
        score=reward,
    )


def _failed(task: Task, detail: str) -> Graded:
    return Graded(
        checks=tuple(_result(check, False, detail) for check in _checks(task)),
        judge_identity=f"{TB2_POLICY}:{TERMINAL_BENCH}:verifier-{TERMINAL_BENCH_COMMIT}",
        score=0.0,
    )


async def _grade(req: GradeRequest) -> Graded:
    interruption = req.execution.get("error")
    if isinstance(interruption, str) and interruption.startswith("environment interruption: "):
        return _unjudged(req.task, interruption)
    product = req.product or {}
    shared = product.get("terminal_bench")
    container: str | None = None
    if isinstance(shared, dict):
        for key in ("container", "container_id", "container_name"):
            value = shared.get(key)
            if isinstance(value, str) and value:
                container = value
                break
    if container is None:
        return _unjudged(
            req.task,
            "missing evidence: harness reported no live task container handle "
            "(terminal_bench.container); harnesses without live_container score NA before the run",
        )
    try:
        outcome = await run_verifier(container, req.task.data)
    except (HarborTaskError, OSError) as exc:
        return _unjudged(req.task, f"missing evidence: upstream verifier unavailable: {exc}")
    reward = outcome.get("reward")
    if not isinstance(reward, (int, float)):
        return _failed(
            req.task,
            f"upstream verifier failed: {outcome.get('error')}; copy_exit={outcome.get('copy_exit')}; test_exit={outcome.get('test_exit')}",
        )
    if not 0 <= float(reward) <= 1:
        return _unjudged(req.task, "missing evidence: upstream verifier returned an out-of-range reward")
    return _graded(req.task, float(reward), outcome)


def _no_world(_task: Task, _evidence: Any, _product: JsonObject | None) -> dict[str, JsonValue]:
    from parallax.judge import MissingEvidence

    raise MissingEvidence("terminal-bench-2 is graded by its upstream verifier, not world evidence")


TB2_POLICY_OBJECT = GradingPolicy(
    id=TB2_POLICY,
    checks=_checks,
    evidence=_no_world,
    grade=_grade,
    judge=JudgePolicy(
        name=f"{TB2_POLICY}:{TERMINAL_BENCH_COMMIT}",
        questions={"upstream": "The pinned upstream verifier decides the task."},
        evidence_form=f"terminal-bench-2 tests/test.sh reward, pinned revision {TERMINAL_BENCH_COMMIT}",
    ),
    identity=lambda: f"{TB2_POLICY}:{TERMINAL_BENCH}:verifier-{TERMINAL_BENCH_COMMIT}",
)


def _extension_model() -> type[BaseModel]:
    from parallax.official_suites import OfficialMetadata

    return OfficialMetadata


TB2_POLICY_EXTENSION = TaskExtension(
    name="official-terminal-bench-2",
    model=_extension_model(),
    default_services=False,
    policy=TB2_POLICY_OBJECT,
)

__all__ = [
    "DEFAULT_PIDS_LIMIT",
    "PINNED_IMAGE_DIGESTS",
    "REWARD_JSON",
    "REWARD_TEXT",
    "TB2_POLICY",
    "TB2_POLICY_EXTENSION",
    "TERMINAL_BENCH",
    "TERMINAL_BENCH_COMMIT",
    "TERMINAL_BENCH_EXPECTED_COUNT",
    "TERMINAL_BENCH_MANIFEST_SHA256",
    "TERMINAL_BENCH_SPEC",
    "HarborTaskError",
    "HarborTaskSpec",
    "fetch_terminal_bench",
    "null_conformance",
    "oracle_conformance",
    "parse_task_dir",
    "records_from_root",
    "remove_container",
    "run_verifier",
    "start_task_container",
    "terminal_bench_tasks",
    "tests_digest",
]
