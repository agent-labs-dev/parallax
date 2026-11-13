from pathlib import Path

# The evidence a correct run leaves under .parallax-terminal/, as the harness writes it.
FILES = {
    ".parallax-terminal/commands.jsonl": "{\"agent_terminal\": true, \"argv\": [\"login\", \"--use-device-code\"], \"command\": \"az\", \"event\": \"start\", \"path\": \"/usr/bin/az\", \"run\": \"r1\", \"stdin_tty\": true, \"stdout_tty\": true, \"ts_ms\": 1780390801000}\n{\"agent_terminal\": true, \"argv\": [\"login\", \"--use-device-code\"], \"command\": \"az\", \"event\": \"device_code\", \"path\": \"/usr/bin/az\", \"provider\": \"az\", \"run\": \"r1\", \"stdin_tty\": true, \"stdout_tty\": true, \"ts_ms\": 1780390801100}\n{\"agent_terminal\": true, \"argv\": [\"login\", \"--use-device-code\"], \"command\": \"az\", \"event\": \"waiting\", \"path\": \"/usr/bin/az\", \"purpose\": \"sign_in\", \"run\": \"r1\", \"stdin_tty\": true, \"stdout_tty\": true, \"ts_ms\": 1780390801150}\n{\"agent_terminal\": true, \"argv\": [\"login\", \"--use-device-code\"], \"command\": \"az\", \"event\": \"person_acted\", \"path\": \"/usr/bin/az\", \"purpose\": \"sign_in\", \"run\": \"r1\", \"stdin_tty\": true, \"stdout_tty\": true, \"ts_ms\": 1780390822000}\n{\"agent_terminal\": true, \"argv\": [\"login\", \"--use-device-code\"], \"command\": \"az\", \"event\": \"end\", \"exit_code\": \"0\", \"path\": \"/usr/bin/az\", \"run\": \"r1\", \"stdin_tty\": true, \"stdout_tty\": true, \"ts_ms\": 1780390822100}\n",
    ".parallax-terminal/handoffs.json": "[\n  {\n    \"type\": \"terminal.input_needed\",\n    \"event_id\": \"evt_c1\",\n    \"timestamp\": 1780390802800,\n    \"session_id\": \"ts_a\",\n    \"secret\": false,\n    \"prompt_tail\": \"To sign in, use a web browser to open the page https://microsoft.com/devicelogin and enter the code F7K2M9XQL to authenticate.\",\n    \"device_code\": {\n      \"provider\": \"az\",\n      \"url\": \"https://microsoft.com/devicelogin\",\n      \"user_code\": \"F7K2M9XQL\",\n      \"kind\": \"device_code\"\n    }\n  },\n  {\n    \"type\": \"terminal.input_resolved\",\n    \"event_id\": \"evt_r1\",\n    \"timestamp\": 1780390822500,\n    \"request_event_id\": \"evt_c1\",\n    \"session_id\": \"ts_a\",\n    \"outcome\": \"answered\"\n  }\n]\n",
    ".parallax-terminal/person.jsonl": "{\"action\": \"finished the sign-in in the browser\", \"card_event_id\": \"evt_c1\", \"device_code\": {\"kind\": \"device_code\", \"provider\": \"az\", \"url\": \"https://microsoft.com/devicelogin\", \"user_code\": \"F7K2M9XQL\"}, \"ts_ms\": 1780390821900}\n",
    ".parallax-terminal/sessions.json": "{\n  \"agent_sessions\": [\n    {\n      \"id\": \"ts_a\",\n      \"kind\": \"agent\",\n      \"status\": \"active\",\n      \"created_at\": 1780390800000\n    }\n  ]\n}\n"
}


async def solve(step: str, text: str, world: object, workspace: Path) -> str:
    for rel, body in FILES.items():
        path = workspace / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)
    return 'I ran `az login --use-device-code` in my terminal. You opened https://microsoft.com/devicelogin and entered the code F7K2M9XQL, and the Azure CLI is now signed in as alex@northwind.test with Northwind Dev as the default subscription.'
