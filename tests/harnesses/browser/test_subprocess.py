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
