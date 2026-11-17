from __future__ import annotations

import json
import sys
from pathlib import Path

from parallax.harnesses.browser.subprocess import JsonWorker


async def test_json_worker_exchanges_events_and_cleans_up(tmp_path: Path) -> None:
    code = (
        "import json,sys; request=json.loads(sys.stdin.readline()); "
        "print(json.dumps({'event':'ready','value':request['value']}),flush=True); "
        "print(json.dumps({'event':'result','status':'done','answer':'ok'}),flush=True); "
        "json.loads(sys.stdin.readline()); print(json.dumps({'event':'stopped','stopped':True}),flush=True)"
    )
    worker = JsonWorker((sys.executable, "-c", code), env={}, cwd=tmp_path)
    async with worker:
        await worker.send({"value": 3})
        assert (await worker.receive("ready"))["value"] == 3
        assert (await worker.receive("result"))["answer"] == "ok"
        await worker.send({})
        assert (await worker.receive("stopped"))["stopped"] is True


def test_recorded_worker_stream_has_ready_result_and_stop() -> None:
    path = Path(__file__).with_name("fixtures") / "worker-events.jsonl"
    events = [json.loads(line) for line in path.read_text().splitlines()]
    assert [event["event"] for event in events] == ["ready", "result", "stopped"]
    assert events[1]["status"] == "done"


async def test_browser_harness_worker_runs_the_agent_on_the_pinned_model(tmp_path: Path) -> None:
    agent = tmp_path / "agent.py"
    agent.write_text("import json,sys; print(json.dumps(sys.argv[1:]))")
    worker_module = ("-m", "parallax.harnesses.browser.browser_harness_worker")
    env = {"PARALLAX_BROWSER_AGENT_COMMAND": f"{sys.executable} {agent}", "PATH": ""}
    worker = JsonWorker((sys.executable, *worker_module), env=env, cwd=tmp_path)
    async with worker:
        await worker.send({"goal": "open the page", "model": "gpt-6-luna", "cdp_url": "ws://x"})
        result = await worker.receive("result")
        await worker.send({})
        await worker.receive("stopped")
    assert json.loads(result["answer"]) == ["exec", "--json", "--model", "gpt-6-luna", "--", "open the page"]
