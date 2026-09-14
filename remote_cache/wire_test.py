"""The wire primitives: what they read, what they refuse, and what they leave out."""

import unittest

import wire


class TestReading(unittest.TestCase):
    def test_a_varint_and_a_length_delimited_field(self) -> None:
        # field 1 varint 300, field 2 bytes "hi"
        data = bytes([0x08, 0xAC, 0x02, 0x12, 0x02]) + b"hi"
        self.assertEqual(list(wire.fields(data)), [(1, 300), (2, b"hi")])

    def test_fixed_width_fields_are_stepped_over(self) -> None:
        """Nothing here uses one, but a message carrying one still has to be readable."""
        data = bytes([0x0D, 1, 2, 3, 4]) + bytes([0x11, 1, 2, 3, 4, 5, 6, 7, 8]) + b"\x18\x07"
        self.assertEqual(list(wire.fields(data)), [(3, 7)])

    def test_a_truncated_message_is_refused(self) -> None:
        # A length-delimited field, a varint, and a fixed-width field each cut short.
        for data in (bytes([0x12, 0x05]) + b"abc", bytes([0x08, 0x80]), bytes([0x11, 1, 2, 3])):
            with self.subTest(data=data), self.assertRaisesRegex(ValueError, "past the end"):
                list(wire.fields(data))

    def test_a_varint_is_at_most_64_bits(self) -> None:
        """Ten bytes carry a full 64-bit value; an eleventh is a message we do not understand."""
        biggest = wire.varint(1, 2**64 - 1)
        self.assertEqual(wire.Message(biggest).integer(1), 2**64 - 1)
        with self.assertRaisesRegex(ValueError, "longer than 64 bits"):
            list(wire.fields(bytes([0x08]) + b"\xff" * 10 + b"\x01"))

    def test_a_group_is_refused(self) -> None:
        """Groups left proto3, so one arriving means this is not the message we think it is."""
        with self.assertRaisesRegex(ValueError, "wire type 3"):
            list(wire.fields(bytes([0x0B])))

    def test_field_number_zero_is_refused(self) -> None:
        with self.assertRaisesRegex(ValueError, "field number 0"):
            list(wire.fields(bytes([0x00, 0x01])))

    def test_the_last_value_wins_for_a_singular_field(self) -> None:
        one = wire.Message(bytes([0x08, 0x01, 0x08, 0x02]))
        self.assertEqual(one.integer(1), 2)

    def test_a_repeated_field_keeps_its_order(self) -> None:
        one = wire.Message(bytes([0x12, 0x01]) + b"a" + bytes([0x12, 0x01]) + b"b")
        self.assertEqual(one.blobs(2), [b"a", b"b"])

    def test_a_missing_field_is_the_default(self) -> None:
        one = wire.Message(b"")
        self.assertEqual(one.integer(1), 0)
        self.assertEqual(one.text(1), "")
        self.assertEqual(one.blob(1), b"")
        self.assertIsNone(one.child(1))
        self.assertEqual(one.children(1), [])

    def test_a_field_of_the_wrong_kind_is_not_mistaken_for_one_we_want(self) -> None:
        """Reading a varint as bytes, or the reverse, would be worse than reading nothing."""
        one = wire.Message(bytes([0x08, 0x07]))
        self.assertEqual(one.blob(1), b"")
        self.assertEqual(one.integer(1), 7)

    def test_a_repeated_number_reads_packed_or_one_tag_each(self) -> None:
        packed = wire.Message(bytes([0x0A, 0x03, 0x01, 0x02, 0x03]))
        spread = wire.Message(bytes([0x08, 0x01, 0x08, 0x02, 0x08, 0x03]))
        self.assertEqual(packed.numbers(1), [1, 2, 3])
        self.assertEqual(spread.numbers(1), [1, 2, 3])


class TestWriting(unittest.TestCase):
    def test_a_default_is_left_out(self) -> None:
        """proto3 writes nothing for a zero or an empty, which the reference bytes depend on."""
        self.assertEqual(wire.varint(1, 0), b"")
        self.assertEqual(wire.flag(1, False), b"")
        self.assertEqual(wire.blob(1, b""), b"")
        self.assertEqual(wire.text(1, ""), b"")
        self.assertEqual(wire.packed(1, ()), b"")

    def test_an_empty_nested_message_is_still_written(self) -> None:
        """For a message the tag is what says it was set, so an empty one is not nothing."""
        self.assertEqual(wire.submessage(2, b""), bytes([0x12, 0x00]))

    def test_a_round_trip_through_the_reader(self) -> None:
        data = (
            wire.varint(1, 300)
            + wire.text(2, "hi")
            + wire.flag(3, True)
            + wire.packed(4, (1, 2))
            + wire.submessage(5, wire.varint(1, 9))
        )
        one = wire.Message(data)
        self.assertEqual(one.integer(1), 300)
        self.assertEqual(one.text(2), "hi")
        self.assertTrue(one.flag(3))
        self.assertEqual(one.numbers(4), [1, 2])
        child = one.child(5)
        assert child is not None
        self.assertEqual(child.integer(1), 9)

    def test_a_negative_number_is_refused_rather_than_encoded_wrongly(self) -> None:
        """Negative varints need ten bytes or zigzag; nothing here has one, so it is a mistake."""
        with self.assertRaisesRegex(ValueError, "negative"):
            wire.varint(1, -1)
