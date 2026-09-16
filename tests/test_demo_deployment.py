"""The integration facts a unit test can still hold, and a deploy would otherwise find.

Four things in this file are only true because several places agree with each other, and
each of them has already been a real failure or was one waiting: a service that binds its
own port on a platform that assigns one, a request body the provider would reject, a page
calling a host nothing answers on, and a demo prompt that quietly grew a second copy of the
product's coaching.

None of it reaches a network or needs a credential. What it cannot prove is the other half
-- that the provider accepts this body and that the model answers well from this prompt --
which is what `python3 -m entrypoints.demo.acceptance` is for.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from entrypoints.demo import acceptance, boundary, model as model_module
from entrypoints.demo.config import (
    DEFAULT_PORT,
    PUBLIC_ENDPOINT,
    PUBLIC_HOST,
    RESPOND_PATH,
    from_environment,
)
from garmin_coach_loop import orchestration


ROOT = Path(__file__).resolve().parents[1]
DEMO = ROOT / "entrypoints" / "demo"
GATEWAY_HOST = "mcp.paceandstaystrong.com"


class PlatformPortTest(unittest.TestCase):
    """Railway assigns the port and routes to it. Binding another number is invisible.

    The failure it prevents is the confusing kind: the process starts, logs that it is
    listening, serves nothing, and the platform health check fails against a service that
    is working perfectly.
    """

    def test_the_platform_port_is_read_first(self):
        self.assertEqual(4711, from_environment({"PORT": "4711"}).port)

    def test_the_platform_port_wins_over_this_service_s_own(self):
        config = from_environment({"PORT": "4711", "COACH_DEMO_PORT": "8433"})
        self.assertEqual(4711, config.port)

    def test_the_service_s_own_port_still_works_where_nothing_is_injected(self):
        self.assertEqual(9001, from_environment({"COACH_DEMO_PORT": "9001"}).port)

    def test_an_empty_platform_port_does_not_shadow_the_rest(self):
        self.assertEqual(9001, from_environment({"PORT": "  ", "COACH_DEMO_PORT": "9001"}).port)
        self.assertEqual(DEFAULT_PORT, from_environment({"PORT": ""}).port)

    def test_a_platform_port_that_is_not_a_number_stops_the_deployment(self):
        from entrypoints.demo.config import ConfigError

        with self.assertRaises(ConfigError):
            from_environment({"PORT": "http"})

    def test_the_default_is_only_the_local_fallback(self):
        self.assertEqual(DEFAULT_PORT, from_environment({}).port)

    def test_the_railway_config_health_check_is_the_route_this_service_serves(self):
        from entrypoints.demo.config import HEALTH_PATH

        toml = (ROOT / "railway.demo.toml").read_text(encoding="utf-8")
        self.assertIn(f'healthcheckPath = "{HEALTH_PATH}"', toml)
        self.assertIn('dockerfilePath = "Dockerfile.demo"', toml)
        # Directives only -- the comments in that file explain at length why there is no
        # volume, and a substring check would read that explanation as one.
        directives = "\n".join(
            line for line in toml.splitlines() if not line.lstrip().startswith("#")
        ).lower()
        self.assertNotIn("volume", directives)
        self.assertNotIn("mount", directives)

    def test_the_demo_image_does_not_hardcode_a_bound_port(self):
        dockerfile = (ROOT / "Dockerfile.demo").read_text(encoding="utf-8")
        self.assertNotIn("COACH_DEMO_PORT=", dockerfile)
        self.assertIn("python3", dockerfile)


class ResponsesRequestShapeTest(unittest.TestCase):
    """The request body, held to what the Responses API actually takes.

    Asserted against `build_request` rather than against a live call, so the shape is
    reviewable without a credential -- and so a change to it is a visible diff here rather
    than a 400 nobody sees until launch day.
    """

    def setUp(self):
        self.body = model_module.build_request(
            instructions="be a coach",
            input_items=[{"role": "user", "content": "hello"}],
            tools=boundary.tool_definitions(),
        )

    def test_it_names_the_pinned_model(self):
        self.assertEqual("gpt-6-astra", self.body["model"])
        self.assertEqual("gpt-6-astra", model_module.MODEL)

    def test_nothing_is_stored_at_the_provider(self):
        self.assertIs(False, self.body["store"])

    def test_it_asks_for_low_reasoning_effort(self):
        self.assertEqual({"effort": "low"}, self.body["reasoning"])
        # The values the API defines. A typo here is a 400 on the first real call.
        self.assertIn(
            model_module.REASONING_EFFORT,
            {"none", "minimal", "low", "medium", "high", "xhigh", "max"},
        )

    def test_it_asks_for_encrypted_reasoning_so_a_stateless_turn_can_continue(self):
        self.assertIn("reasoning.encrypted_content", self.body["include"])
        self.assertEqual("reasoning.encrypted_content", model_module.ENCRYPTED_REASONING)

    def test_the_output_ceiling_leaves_room_for_reasoning_as_well_as_an_answer(self):
        # It counts reasoning tokens too, so the old 2000 was a budget the reasoning
        # alone could spend -- which comes back as a truncation, not a shorter answer.
        self.assertEqual(8000, self.body["max_output_tokens"])
        self.assertGreater(model_module.MAX_OUTPUT_TOKENS, 4000)

    def test_every_tool_is_declared_the_way_a_function_tool_is_declared(self):
        for tool in self.body["tools"]:
            with self.subTest(tool=tool["name"]):
                self.assertEqual("function", tool["type"])
                # `strict` is required on a function tool, and omitting it is the kind of
                # thing that only fails against the real API.
                self.assertIn("strict", tool)
                self.assertIsInstance(tool["strict"], bool)
                self.assertIsInstance(tool["parameters"], dict)
        self.assertEqual("auto", self.body["tool_choice"])

    def test_a_turn_with_no_tools_declares_none(self):
        body = model_module.build_request(instructions="x", input_items=[], tools=None)
        self.assertNotIn("tools", body)
        self.assertNotIn("tool_choice", body)

    def test_the_body_never_carries_the_credential(self):
        self.assertNotIn("api_key", json.dumps(self.body).lower())
        self.assertNotIn("authorization", json.dumps(self.body).lower())


class StatelessContinuationTest(unittest.TestCase):
    """With `store: false` the next round's input is the whole conversation.

    The item easiest to lose is the one that matters most: a `reasoning` item carries this
    turn's chain as `encrypted_content`, and dropping it makes every round after a tool
    call start again from the words alone.
    """

    def _reasoning(self, identifier: str, encrypted: str) -> dict:
        return {
            "type": "reasoning",
            "id": identifier,
            "summary": [],
            "encrypted_content": encrypted,
        }

    def _call(self, call_id: str, arguments: dict) -> dict:
        return {
            "type": "function_call",
            "call_id": call_id,
            "name": boundary.READ_EVIDENCE,
            "arguments": json.dumps(arguments),
        }

    def test_every_output_item_is_carried_forward_verbatim_and_in_order(self):
        items = (
            self._reasoning("rs_1", "ENCRYPTED-ONE"),
            self._call("fc_1", {"read": ["strength"]}),
        )
        carried = model_module.carry_forward(items)
        self.assertEqual(list(items), carried)
        self.assertEqual("ENCRYPTED-ONE", carried[0]["encrypted_content"])

    def test_carrying_forward_copies_rather_than_aliasing(self):
        item = self._reasoning("rs_1", "ENCRYPTED-ONE")
        carried = model_module.carry_forward((item,))
        carried[0]["encrypted_content"] = "CHANGED"
        self.assertEqual("ENCRYPTED-ONE", item["encrypted_content"])

    def test_a_tool_result_is_spelled_the_way_the_next_round_takes_it(self):
        output = model_module.function_call_output("fc_1", {"status": "passed"})
        self.assertEqual("function_call_output", output["type"])
        self.assertEqual("fc_1", output["call_id"])
        self.assertEqual({"status": "passed"}, json.loads(output["output"]))

    def test_a_reasoning_item_is_read_off_a_response(self):
        turn = model_module.parse_response(
            {
                "output": [
                    self._reasoning("rs_1", "ENCRYPTED-ONE"),
                    self._call("fc_1", {"read": ["strength"]}),
                ]
            }
        )
        self.assertEqual(1, len(turn.reasoning_items))
        self.assertEqual("ENCRYPTED-ONE", turn.reasoning_items[0]["encrypted_content"])
        self.assertEqual(1, len(turn.tool_calls))
        self.assertEqual("fc_1", turn.tool_calls[0].call_id)

    def test_a_response_that_spent_its_ceiling_on_reasoning_says_so_by_name(self):
        with self.assertRaises(model_module.ModelError) as raised:
            model_module.parse_response(
                {
                    "status": "incomplete",
                    "incomplete_details": {"reason": "max_output_tokens"},
                    "output": [self._reasoning("rs_1", "ENCRYPTED-ONE")],
                }
            )
        # A distinct code because the fix is to raise the ceiling, not to try again.
        self.assertEqual("model_output_truncated", raised.exception.code)


class ProviderShapeStaysInOneFileTest(unittest.TestCase):
    """Only ``model.py`` knows what the Responses API looks like.

    The value of that is not tidiness: it is that swapping or upgrading the provider means
    reading one file, and that nothing about a coaching conversation is expressed in a
    vendor's vocabulary. `service.py` drives rounds and tool results through helpers; it
    does not spell a provider field, and `garmin_coach_loop` does not know the API exists.
    """

    PROVIDER_FIELDS = (
        '"max_output_tokens"',
        '"tool_choice"',
        '"encrypted_content"',
        '"function_call_output"',
        '"incomplete_details"',
        '"output_text"',
        '"include"',
        '"store"',
    )

    def test_no_other_demo_module_spells_a_provider_field(self):
        for path in sorted(DEMO.glob("*.py")):
            if path.name == "model.py":
                continue
            text = path.read_text(encoding="utf-8")
            for field in self.PROVIDER_FIELDS:
                with self.subTest(module=path.name, field=field):
                    self.assertNotIn(field, text)

    def test_the_provider_host_is_named_only_where_the_call_is_made(self):
        for path in sorted(DEMO.glob("*.py")):
            if path.name in {"model.py", "config.py"}:
                continue
            with self.subTest(module=path.name):
                self.assertNotIn("openai.com", path.read_text(encoding="utf-8"))


class DemoPromptOwnsOnlyItsOwnLayerTest(unittest.TestCase):
    """The demo prompt says what is true here. Coaching stays with the canonical prompt.

    Each row names a policy, a phrase proving the canonical training judgment still owns
    it, and the phrasings the demo prompt would restate it with. Both halves are asserted:
    a canonical phrase that disappears fails here too, so this cannot go quiet by the
    owner dropping the policy rather than by the demo growing a copy of it.
    """

    OWNED_BY_CANONICAL = (
        (
            "missing evidence is not zero",
            "holds no matched activity",
            ("not a zero", "never a zero", "unread, not normal", "stays missing",
             "gap is never"),
        ),
        (
            "execution trend versus the declared outcome",
            "Has the declared outcome moved",
            ("declared outcome measurement", "execution trend", "keep two claims apart"),
        ),
        (
            "no invented precision in a prescription",
            "Prescribe without invented precision",
            ("invent no numbers", "manufacturing precision", "inherits the quality"),
        ),
        (
            "one session is not a direction",
            "A single session rarely establishes a direction",
            ("single session", "one session is not"),
        ),
        (
            "how a week is arranged",
            "Arrange the week",
            ("heavy lower-body", "stacking them", "baseline week shape"),
        ),
    )

    def setUp(self):
        # Whitespace-normalised: these are sentences in a wrapped Markdown file, and a
        # phrase that happens to straddle a line break is still the same policy.
        self.demo = " ".join(
            (DEMO / "orchestration.md").read_text(encoding="utf-8").lower().split()
        )
        self.canonical = orchestration.training_judgment()
        self.canonical_flat = " ".join(self.canonical.lower().split())

    def test_the_canonical_prompt_still_owns_each_policy(self):
        for label, marker, _ in self.OWNED_BY_CANONICAL:
            with self.subTest(policy=label):
                self.assertIn(" ".join(marker.lower().split()), self.canonical_flat)

    def test_the_demo_prompt_does_not_restate_any_of_them(self):
        for label, _, restatements in self.OWNED_BY_CANONICAL:
            for phrase in restatements:
                with self.subTest(policy=label, phrase=phrase):
                    self.assertNotIn(phrase, self.demo)

    def test_the_demo_prompt_keeps_what_is_true_only_here(self):
        for phrase in (
            "playground",
            "nothing you do is saved",
            boundary.READ_EVIDENCE,
            boundary.PREVIEW_PLAN_CHANGE,
            "two or three materially different allocations",
            "**preserve**",
            "**sacrifice**",
            "**evidence**",
            "**uncertainty**",
        ):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase.lower(), self.demo)

    def test_the_safety_line_it_keeps_is_not_one_the_canonical_prompt_carries(self):  # noqa: E501
        # Kept deliberately: `training_judgment()` says nothing about not diagnosing, so
        # removing this would delete the only such instruction a public demo has rather
        # than deduplicate it. If the canonical prompt ever takes it, this fails and the
        # demo's copy comes out.
        self.assertIn("do not diagnose", self.demo)
        self.assertNotIn("do not diagnose", self.canonical_flat)

    def test_the_demo_prompt_stays_shorter_than_the_coaching_it_defers_to(self):
        # Not a budget, a shape: a demo orchestration layer approaching the size of the
        # training judgment has started being a second coach.
        self.assertLess(len(self.demo), len(self.canonical))

    def test_the_served_instructions_carry_the_canonical_judgment_unchanged(self):
        from entrypoints.demo.config import DemoConfig
        from entrypoints.demo.service import DemoService

        service = DemoService(DemoConfig(api_key="x"), client=object())
        self.assertIn(self.canonical, service.instructions())


class FrontendAndBackendAgreeTest(unittest.TestCase):
    """One host, written in several places, held equal here.

    The page cannot be read from this repository, so what is checked is the constant it is
    generated from and every document that repeats it. The website repository holds its own
    end of the same equality in `scripts/check-site.py`.
    """

    def test_the_public_endpoint_is_built_from_the_host_and_the_served_route(self):
        self.assertEqual(f"https://{PUBLIC_HOST}{RESPOND_PATH}", PUBLIC_ENDPOINT)
        self.assertEqual("demo-api.paceandstaystrong.com", PUBLIC_HOST)
        self.assertEqual("/demo/v1/respond", RESPOND_PATH)

    def test_the_demo_never_answers_on_the_gateway_s_host(self):
        self.assertNotIn(GATEWAY_HOST, PUBLIC_ENDPOINT)

    def test_every_document_that_names_the_endpoint_names_this_one(self):
        for relative in (
            "entrypoints/demo/README.md",
            "docs/ops/deploy-demo-service.md",
        ):
            text = (ROOT / relative).read_text(encoding="utf-8")
            with self.subTest(document=relative):
                self.assertIn(PUBLIC_HOST, text)
                self.assertNotIn(f"{GATEWAY_HOST}{RESPOND_PATH}", text)
                self.assertNotIn(f"https://{GATEWAY_HOST}/demo", text)

    def test_the_gateway_serves_no_demo_route(self):
        from garmin_coach_loop.gateway import ROUTES

        for path in ROUTES:
            with self.subTest(route=path):
                self.assertFalse(path.startswith("/demo"))
        self.assertNotIn(RESPOND_PATH, ROUTES)

    def test_the_acceptance_command_points_at_the_same_deployment(self):
        self.assertIn(PUBLIC_HOST, acceptance.main.__doc__ or acceptance.__doc__ or "")


if __name__ == "__main__":
    unittest.main()
