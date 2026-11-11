import unittest

from popo.core.context_tuning import compress, fold


class ContextTuningTests(unittest.TestCase):
    def test_compression_only_trims_older_tool_results(self):
        messages = [{"role": "user", "content": "keep this request"}]
        messages.extend({"role": "tool", "content": f"result-{i}: " + ("x" * 700)}
                        for i in range(5))
        result, info = compress(messages, threshold=1000, target=700, safe_trim=100)
        self.assertLess(info["after"], info["before"])
        self.assertEqual(result[0]["content"], "keep this request")
        self.assertEqual(result[-1]["content"], messages[-1]["content"])

    def test_fold_keeps_first_request_and_live_tail(self):
        messages = [{"role": "system", "content": "policy"}]
        for i in range(12):
            messages.extend([{"role": "user", "content": f"request {i}"},
                             {"role": "assistant", "content": f"answer {i}"}])
        result, info = fold(messages, at_messages=14, keep_turns=3)
        self.assertLess(len(result), len(messages))
        self.assertEqual(result[1]["content"], "request 0")
        self.assertEqual(result[-2]["content"], "request 11")
        self.assertEqual(result[2]["name"], "_popo_fold")
        self.assertEqual(info["folded"], len(messages) - len(result) + 1)

    def test_fold_is_not_applied_below_threshold(self):
        messages = [{"role": "user", "content": "hello"}]
        result, info = fold(messages, at_messages=14)
        self.assertEqual(result, messages)
        self.assertEqual(info["folded"], 0)

    def test_compression_trims_oversized_system_prompt_as_last_resort(self):
        prompt = "BEGIN-RULES " + ("x" * 5000) + " END-FORMAT"
        result, info = compress([{"role": "system", "content": prompt}],
                                threshold=1000, target=700, system_max=1000)
        self.assertEqual(info["system_trimmed"], 1)
        self.assertIn("BEGIN-RULES", result[0]["content"])
        self.assertIn("END-FORMAT", result[0]["content"])
        self.assertIn("system prompt omitted", result[0]["content"])
        self.assertLess(len(result[0]["content"]), len(prompt))


if __name__ == "__main__":
    unittest.main()
