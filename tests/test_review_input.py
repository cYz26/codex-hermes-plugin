import json
import unittest

from hermes_codex_learning.review_input import bound_review_history
from hermes_codex_learning.store import dumps


def tool_exchange(call_id, name, arguments, result):
    return [
        {"role": "assistant", "content": None, "observed": True, "tool_calls": [
            {"id": call_id, "type": "function", "function": {"name": name, "arguments": dumps(arguments)}}
        ]},
        {"role": "tool", "tool_call_id": call_id, "tool_name": name, "content": dumps(result), "observed": True},
    ]


class ReviewInputTests(unittest.TestCase):
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
