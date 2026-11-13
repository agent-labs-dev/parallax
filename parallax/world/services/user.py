"""User: the agent's channel to the person it works for, answered from the task's scripted rules.

There is no simulator: every reply comes from `seed.user_rules`, first matching rule wins, and a rule
with `uses > 0` stops matching once it has answered that many times.

Tables:
- rules:        {id, tool, when, reply, decision, uses}, loaded by the world from `seed.user_rules`
  (ids in seed order, which is also match priority); never changed
- interactions: {id, kind, question, options[], action, scope, reply, decision, rule, at}; every reply,
  logged. An approval's `id` is its receipt; checks bind later calls to its `scope`.
"""

from __future__ import annotations

from typing import ClassVar, Literal

from pydantic import Field, JsonValue

from parallax.match import match_fields
from parallax.models import JsonObject, UserRule
from parallax.world.service import Service, tool
from parallax.world.services.common import Args, get_str, iso, json_list, now_iso, zone

UNMATCHED_ASK = "I don't have a preference - use your judgement."
UNMATCHED_APPROVAL = "No, hold off on that for now."


class AskArgs(Args):
    question: str = Field(min_length=1, description="The question, phrased for the user")
    options: list[str] = Field([], description="Choices to offer, if any")


class ApprovalArgs(Args):
    action: str = Field(min_length=1, description="What you want to do, e.g. 'refund charge ch_123 in full'")
    scope: JsonObject = Field(description='The exact parameters you will use, e.g. {"charge": "ch_123"}')


class UserService(Service):
    name: ClassVar[str] = "user"
    tables: ClassVar[tuple[str, ...]] = ("rules", "interactions")

    # The box's system clock is real time, not the world's; without this default tool no agent can
    # resolve "tomorrow" or "next Tuesday" against the seed's frozen clock.
    @tool(
        mutates=False,
        description="The user's current date and time, in their time zone. Use this to resolve relative dates "
        "such as today, tomorrow or next week: the machine you run on has its own clock, which is not the user's.",
    )
    async def clock_now(self, args: Args) -> JsonValue:
        tz = self.world.persona.timezone
        now = self.world.now.astimezone(zone(tz))
        return {"now": iso(now), "weekday": now.strftime("%A"), "timezone": tz}

    # Without it "my issues" or "my inbox" has no referent: the persona's identity lives only in the
    # seed, and every service keys people (tracker assignee, mail sender, attendees) by this email.
    @tool(
        mutates=False,
        description="Who the user you work for is: their name, email address and time zone. The email is "
        "how they appear across every service, e.g. as a tracker assignee, mail sender or event attendee.",
    )
    async def whoami(self, args: Args) -> JsonValue:
        persona = self.world.persona
        return {"name": persona.name, "email": persona.email, "timezone": persona.timezone}

    @tool(mutates=False, description="Ask the user a question and wait for their answer.")
    async def ask(self, args: AskArgs) -> JsonValue:
        fields: JsonObject = {"question": args.question, "options": json_list(args.options)}
        rule_id, rule = self._match("ask", fields)
        reply = rule.reply if rule else UNMATCHED_ASK
        receipt = self._log("ask", fields, reply=reply, decision="answer", rule=rule_id)
        return {"id": receipt, "answer": reply}

    @tool(
        mutates=False,
        description="Ask the user to approve an action before you take it. Only act if the decision is approve, "
        "and only within the approved scope.",
    )
    async def request_approval(self, args: ApprovalArgs) -> JsonValue:
        fields: JsonObject = {"action": args.action, "scope": args.scope}
        rule_id, rule = self._match("approval", fields)
        decision = rule.decision if rule else "deny"
        reply = rule.reply if rule else UNMATCHED_APPROVAL
        receipt = self._log("approval", fields, reply=reply, decision=decision, rule=rule_id)
        return {"id": receipt, "decision": decision, "approved": decision == "approve", "reply": reply}

    def _match(self, kind: Literal["ask", "approval"], fields: JsonObject) -> tuple[str | None, UserRule | None]:
        tz = self.world.persona.timezone
        for row in self.world.table("user.rules").values():
            rule_id = get_str(row, "id")
            rule = UserRule.model_validate({k: v for k, v in row.items() if k != "id"})
            if (
                rule.tool == kind
                and not self._used_up(rule_id, rule)
                and match_fields(fields, rule.when, tz, self.world.now)
            ):
                return rule_id, rule
        return None, None

    def _used_up(self, rule_id: str, rule: UserRule) -> bool:
        if rule.uses == 0:
            return False
        answered = sum(1 for i in self.world.table("user.interactions").values() if i.get("rule") == rule_id)
        return answered >= rule.uses

    def _log(self, kind: str, fields: JsonObject, *, reply: str, decision: str, rule: str | None) -> str:
        row: JsonObject = {
            "id": self.world.new_id(f"{kind}_"),
            "kind": kind,
            **fields,
            "reply": reply,
            "decision": decision,
            "rule": rule,
            "at": now_iso(self.world),
        }
        self.world.table("user.interactions")[get_str(row, "id")] = row
        return get_str(row, "id")
