import unittest

from wechat_desktop_agent.clipboard_records import (
    CopiedRecord, Refused, parse_copied_records, to_bubbles,
)


class CopiedRecordTests(unittest.TestCase):
    def test_four_selected_records_keep_order_and_duplicate_body(self):
        sample = ("Speaker A\n2026年10月07日 10:01\nhello\n\n"
                  "Speaker B\n2026年10月07日 10:02\nline one\nline two\n\n"
                  "Speaker A\n2026年10月07日 10:03\nsame\n\n"
                  "Speaker A\n2026年10月07日 10:03\nsame")
        records = parse_copied_records(sample, 4)
        self.assertEqual(len(records), 4)
        self.assertEqual([item.text for item in records],
                         ["hello", "line one\nline two", "same", "same"])
        self.assertNotEqual(id(records[2]), id(records[3]))
        self.assertEqual(records[2].time_label, "2026年10月07日 10:03")
        self.assertTrue(all(item.direction == "unknown" and item.provenance == "clipboard_text"
                            and item.native_message_id_available is False for item in records))

    def test_body_blank_lines_and_crlf_are_preserved(self):
        sample = ("A\r\n2026年10月07日 10:01\r\none\r\n\r\nthree\r\n\r\n"
                  "B\r\n2026年10月07日 10:02\r\nlast")
        records = parse_copied_records(sample, 2)
        self.assertEqual(records[0].text, "one\n\nthree")
        self.assertEqual(records[1].text, "last")

    def test_date_structure_and_missing_fields_refused(self):
        for sample in (
            "A\n2026年02月30日 10:01\nbody",
            "A\n2026年10月07日 25:01\nbody",
            "A\n2026-10-07 10:01\nbody",
            " \n2026年10月07日 10:01\nbody",
            "A\n2026年10月07日 10:01",
            "A\n2026年10月07日 10:01\n   ",
        ):
            with self.subTest(sample=sample), self.assertRaises(Refused):
                parse_copied_records(sample, 1)

    def test_count_and_fake_record_separator_refused(self):
        sample = "A\n2026年10月07日 10:01\nbody\n\nB\n2026年10月07日 10:02\nbody"
        with self.assertRaisesRegex(Refused, "count_or_separator_ambiguous"):
            parse_copied_records(sample, 1)
        with self.assertRaisesRegex(Refused, "count_or_separator_ambiguous"):
            parse_copied_records(sample, 3)
        fake = sample + "\n\nA\n2026年10月07日 10:03\nextra"
        with self.assertRaisesRegex(Refused, "count_or_separator_ambiguous"):
            parse_copied_records(fake, 2)

    def test_limits_types_and_control_characters_refused(self):
        sample = "A\n2026年10月07日 10:01\nbody"
        for value, count, options in ((sample, True, {}), (sample, 0, {}),
                                      (sample, 51, {}), (sample, 1, {"max_chars": 5}),
                                      (sample, 1, {"max_chars": True}),
                                      (sample + "\x00", 1, {}),
                                      (sample.replace("\n", "\r", 1), 1, {})):
            with self.subTest(value=value, count=count), self.assertRaises(Refused):
                parse_copied_records(value, count, **options)

    def test_direction_conversion_requires_external_visual_verification(self):
        records = parse_copied_records("A\n2026年10月07日 10:01\nbody", 1)
        with self.assertRaisesRegex(Refused, "direction_unverified"):
            to_bubbles(records, ["incoming"])
        with self.assertRaisesRegex(Refused, "direction_unverified"):
            to_bubbles(records, [])
        with self.assertRaisesRegex(Refused, "direction_unverified"):
            to_bubbles(records, ["unknown"], visually_verified=True)
        bubbles = to_bubbles(records, ["incoming"], visually_verified=True)
        self.assertEqual((bubbles[0].text, bubbles[0].direction, bubbles[0].sender,
                          bubbles[0].time_candidate, bubbles[0].confidence),
                         ("body", "incoming", "A", "2026年10月07日 10:01", 1.0))
        self.assertIsInstance(records[0], CopiedRecord)


if __name__ == "__main__":
    unittest.main()
