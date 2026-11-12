import json
import unittest

from hermes_codex_learning.review_input import bound_review_history, causal_digest
from hermes_codex_learning.store import dumps


def tool_exchange(call_id, name, arguments, result):
    return [
        {"role": "assistant", "content": None, "observed": True, "tool_calls": [
            {"id": call_id, "type": "function", "function": {"name": name, "arguments": dumps(arguments)}}
        ]},
        {"role": "tool", "tool_call_id": call_id, "tool_name": name, "content": dumps(result), "observed": True},
    ]


class ReviewInputTests(unittest.TestCase):
    def test_budget_ranking_preserves_the_prior_approach_despite_shared_source_labels(self):
        def marked(role, text, ref, turn):
            kind = 'request' if role == 'user' else 'result'
            label = {'ref':ref, 'kind':kind, 'chat':'chat', 'turn':turn,
                     'scope':'own', 'observation_revision':1 if turn == 'first' else 2}
            if role == 'user' and turn == 'second':
                label['previous_result'] = 'T1.result'
            return {'role':role, 'content':'[Evidence '+dumps(label)+']\n'+text,
                    'evidence_ref':ref, 'evidence_kind':kind, 'evidence_scope':'own'}

        prior = 'The prior comparison skipped internal evidence.'
        messages = [
            marked('user', 'Evaluate the options.', 'T1.request', 'first'),
            marked('assistant', 'Unrelated opening details.\n'*500 + prior + '\n'
                   + 'Unrelated trailing details.\n'*500, 'T1.result', 'first'),
            marked('user', 'Check internal sources too.', 'T2.request', 'second'),
            marked('assistant', 'Internal evidence changes the preferred alternative.', 'T2.result', 'second'),
        ]

        bounded = bound_review_history(messages, 4096)

        self.assertLessEqual(len(dumps(bounded).encode()), 4096)
        self.assertIn(prior, dumps(bounded))
        self.assertIn('Check internal sources too.', dumps(bounded))

    def test_native_routed_digest_retains_the_early_causal_spine(self):
        goal={'role':'user','content':'Compare deployment alternatives.', 'evidence_ref':'T1.request','evidence_kind':'request'}
        prior={'role':'assistant','content':'Only public evidence was compared.', 'evidence_ref':'T1.result','evidence_kind':'result'}
        addition={'role':'user','content':'Also compare the internal capabilities.', 'evidence_ref':'T2.request','evidence_kind':'request'}
        result={'role':'assistant','content':'Internal reuse changes the preferred alternative.', 'evidence_ref':'T2.result','evidence_kind':'result'}
        messages=[goal,prior,addition]
        for i in range(40):messages+=tool_exchange(str(i),'search',{},'long detail '*200)
        messages.append(result)
        def native_digest(snapshot):
            return [{'role':'user','content':'Earlier source body was summarised'}]+snapshot[-24:]
        digested,report=causal_digest(bound_review_history(messages),native_digest,8192)
        for message in (goal,prior,addition,result):self.assertIn(message['content'],dumps(digested))
        self.assertTrue(report['used'])
        self.assertLessEqual(len(dumps(digested).encode()),8192)
    def test_budget_preserves_goal_followup_and_the_prior_approach_in_the_middle(self):
        goal = "Compare deployment alternatives for the service."
        prior = "used only external sources; internal capabilities were not compared"
        messages = [{"role": "user", "content": goal}]
        for i in range(40):
            messages += tool_exchange(str(i), "search", {}, "repeated detail " * 8000)
        messages += [
            {"role": "assistant", "content": "Background detail.\n" * 500 + prior + "\n" + "More detail.\n" * 500},
            {"role": "user", "content": "Please compare external sources with internal capabilities as well."},
            *tool_exchange("internal", "search", {"source": "internal"}, "large result " * 10000),
            {"role": "assistant", "content": "The internal platform changes the preferred deployment alternative."},
        ]
        bounded = bound_review_history(messages, 4096)
        text = dumps(bounded)
        self.assertLessEqual(len(text.encode()), 4096)
        for fragment in (goal, prior, "Please compare external sources", "changes the preferred"):
            self.assertIn(fragment, text)
        self.assertIn("omitted", text.lower())

    def test_unmatched_results_never_complete_a_different_tool_exchange(self):
        messages = [{"role": "user", "content": "Compare the results"},
                    {"role": "assistant", "content": None, "tool_calls": [
                        {"id": "first", "type": "function", "function": {"name": "search", "arguments": "{}"}}]},
                    {"role": "tool", "tool_call_id": "other", "content": "unmatched result"},
                    {"role": "assistant", "content": "The result is still unknown."}]
        bounded = bound_review_history(messages)
        self.assertFalse(any(m.get("tool_calls") or m["role"] == "tool" for m in bounded))
        self.assertIn("unmatched", dumps(bounded).lower())

    def test_legacy_image_tool_result_is_compacted_only_in_the_review_copy(self):
        encoded = "abcd" * 50_000
        messages = [{"role": "user", "content": "Compare the design"}]
        messages += tool_exchange("image-a", "view_image", {"path": "/project/example.png"}, [
            {"type": "image", "image_url": f"data:application/octet-stream;base64,{encoded}"},
            {"type": "text", "text": "Width 960, height 540"},
        ])
        messages += tool_exchange("image-b", "another_viewer", {}, {"content": [
            {"type": "image", "mimeType": "image/jpeg", "data": encoded},
        ]})
        messages.append({"role": "assistant", "content": "The footer is clipped at 960 px."})
        before = dumps(messages)

        bounded = bound_review_history(messages)

        self.assertEqual(dumps(messages), before)
        self.assertEqual(len(bounded), len(messages))
        self.assertEqual(bounded[1], messages[1])
        self.assertEqual(bounded[-1], messages[-1])
        first_result = json.loads(bounded[2]["content"])
        self.assertEqual(first_result[1]["text"], "Width 960, height 540")
        self.assertNotIn(";base64,", dumps(bounded))
        self.assertNotIn(encoded, dumps(bounded))

    def test_large_text_history_keeps_latest_prompt_conclusion_and_complete_tool_pairs(self):
        messages = [{"role": "user", "content": "Older task"}]
        for i in range(20):
            messages.extend(tool_exchange(f"old-{i}", "Bash", {}, {"output": "output " * 1000}))
        messages += [
            {"role": "assistant", "content": "Earlier conclusion"},
            {"role": "user", "content": "检查最新的结果"},
            *tool_exchange("new", "Bash", {"command": "x" * 8000}, {"output": "细节 " * 3000 + "\nExit code: 1"}),
            {"role": "assistant", "content": "Latest conclusion"},
        ]

        bounded = bound_review_history(messages, 4096)

        self.assertLessEqual(len(dumps(bounded).encode()), 4096)
        self.assertIn("检查最新的结果", [m.get("content") for m in bounded])
        self.assertEqual(bounded[-1], messages[-1])
        self.assertTrue(any("Exit code: 1" in m["content"] for m in bounded if m["role"] == "tool"))
        pending = []
        for message in bounded:
            if message["role"] == "tool":
                self.assertEqual(message["tool_call_id"], pending.pop(0))
            else:
                self.assertEqual(pending, [])
                for call in message.get("tool_calls", []):
                    self.assertIsInstance(json.loads(call["function"]["arguments"]), dict)
                    pending.append(call["id"])
        self.assertEqual(pending, [])
