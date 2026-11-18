from __future__ import annotations

import math
from typing import Any

from can_mcp.adapters.databases.cdd import (
    CddDataIdentifier,
    CddDataObject,
    CddLinearCompensation,
)


class CddCodec:
    _SUPPORTED_LEAF_TYPES = {"IDENT", "LINCOMP", "TEXTTBL"}

    def decode(
        self,
        data_identifier: CddDataIdentifier,
        payload: bytes,
    ) -> dict[str, Any]:
        reader = _BitReader(payload)
        fields: dict[str, Any] = {}
        for index, field in enumerate(data_identifier.fields):
            if field.definition_type == "GAPDATAOBJ":
                reader.read_bits(_gap_width(field), 21)
                continue
            key = _field_key(field)
            if key in fields:
                raise ValueError(
                    f"CDD DID 0x{data_identifier.identifier:04X} contains "
                    f"duplicate field key '{key}'."
                )
            fields[key] = self._decode_object(
                field,
                reader,
                is_last=index == len(data_identifier.fields) - 1,
            )
        if reader.remaining_bits:
            raise ValueError(
                f"CDD DID 0x{data_identifier.identifier:04X} payload has "
                f"{reader.remaining_bits} unexpected trailing bits."
            )
        return {
            "identifier": data_identifier.identifier,
            "identifier_hex": f"0x{data_identifier.identifier:04X}",
            "name": data_identifier.name,
            "qualifier": data_identifier.qualifier,
            "fields": fields,
        }

    def encode(
        self,
        data_identifier: CddDataIdentifier,
        values: dict[str, Any],
    ) -> bytes:
        if not isinstance(values, dict):
            raise ValueError("values must be a mapping of CDD field names to values.")
        if any(not isinstance(key, str) for key in values):
            raise ValueError("CDD field mapping keys must be strings.")
        writer = _BitWriter()
        field_keys = [
            _field_key(field)
            for field in data_identifier.fields
            if field.definition_type != "GAPDATAOBJ"
        ]
        if len(field_keys) != len(set(field_keys)):
            raise ValueError("CDD DID contains fields with duplicate qualifiers.")
        accepted_keys = set(field_keys)
        unknown_keys = set(values) - accepted_keys
        if unknown_keys:
            raise KeyError(
                "Unknown CDD field(s): " + ", ".join(sorted(unknown_keys))
            )
        for field in data_identifier.fields:
            if field.definition_type == "GAPDATAOBJ":
                writer.write_bits(0, _gap_width(field), 21)
                continue
            key = _field_key(field)
            if key not in values:
                raise KeyError(f"Missing value for CDD field '{key}'.")
            self._encode_object(field, values[key], writer)
        return writer.to_bytes()

    def _decode_object(
        self,
        field: CddDataObject,
        reader: _BitReader,
        is_last: bool,
    ) -> Any:
        if field.children:
            result: dict[str, Any] = {}
            for index, child in enumerate(field.children):
                if child.definition_type == "GAPDATAOBJ":
                    reader.read_bits(_gap_width(child), 21)
                    continue
                key = _field_key(child)
                if key in result:
                    raise ValueError(f"CDD structure contains duplicate field '{key}'.")
                result[key] = self._decode_object(
                    child,
                    reader,
                    is_last=is_last and index == len(field.children) - 1,
                )
            return result

        self._require_supported_leaf(field)
        count = self._element_count(field, reader.remaining_bits, is_last)
        encoding = (field.encoding or "").lower()
        byte_order = self._byte_order(field)
        if encoding in {"asc", "bcd"}:
            raw = reader.read_bytes(count * (field.bit_length or 8), byte_order)
            return self._decode_string(field, raw)
        if field.bit_length is None or field.bit_length <= 0:
            raise ValueError(
                f"CDD field '{_field_key(field)}' has no usable bit length."
            )
        raw_values = [
            reader.read_bits(field.bit_length, byte_order)
            for _ in range(count)
        ]
        decoded = [self._decode_number(field, raw) for raw in raw_values]
        if field.quantity == "field":
            return decoded
        return decoded[0]

    def _encode_object(
        self,
        field: CddDataObject,
        value: Any,
        writer: _BitWriter,
    ) -> None:
        if field.children:
            if not isinstance(value, dict):
                raise ValueError(
                    f"CDD structure '{_field_key(field)}' requires a mapping value."
                )
            if any(not isinstance(key, str) for key in value):
                raise ValueError("CDD field mapping keys must be strings.")
            field_keys = [
                _field_key(child)
                for child in field.children
                if child.definition_type != "GAPDATAOBJ"
            ]
            if len(field_keys) != len(set(field_keys)):
                raise ValueError(
                    f"CDD structure '{_field_key(field)}' contains fields with "
                    "duplicate qualifiers."
                )
            accepted_keys = set(field_keys)
            unknown_keys = set(value) - accepted_keys
            if unknown_keys:
                raise KeyError(
                    f"Unknown field(s) in '{_field_key(field)}': "
                    f"{', '.join(sorted(unknown_keys))}"
                )
            for child in field.children:
                if child.definition_type == "GAPDATAOBJ":
                    writer.write_bits(0, _gap_width(child), 21)
                    continue
                key = _field_key(child)
                if key not in value:
                    raise KeyError(f"Missing value for CDD field '{key}'.")
                self._encode_object(child, value[key], writer)
            return
        self._require_supported_leaf(field)

        encoding = (field.encoding or "").lower()
        byte_order = self._byte_order(field)
        if encoding in {"asc", "bcd"}:
            raw = self._encode_string(field, value)
            writer.write_bytes(raw, byte_order)
            return
        if field.bit_length is None or field.bit_length <= 0:
            raise ValueError(
                f"CDD field '{_field_key(field)}' has no usable bit length."
            )
        values = self._normalize_numeric_values(field, value)
        self._validate_element_count(field, len(values))
        for item in values:
            raw = self._encode_number(field, item)
            writer.write_bits(raw, field.bit_length, byte_order)

    def _decode_number(self, field: CddDataObject, raw: int) -> Any:
        encoding = (field.encoding or "").lower()
        if encoding not in {"uns", "sgn"}:
            raise ValueError(
                f"CDD field '{_field_key(field)}' uses unsupported encoding "
                f"'{field.encoding}'."
            )
        if field.signed:
            assert field.bit_length is not None
            sign_bit = 1 << (field.bit_length - 1)
            if raw & sign_bit:
                raw -= 1 << field.bit_length

        if field.text_mappings:
            for mapping in field.text_mappings:
                if mapping.start <= raw <= mapping.end:
                    return mapping.text
        if field.linear_compensations:
            compensation = self._compensation_for_raw(
                field.linear_compensations,
                raw,
            )
            if compensation is None:
                raise ValueError(
                    f"CDD raw value {raw} is outside the linear-compensation "
                    f"ranges for field '{_field_key(field)}'."
                )
            value = raw * compensation.factor + compensation.offset
            return int(value) if value.is_integer() else value
        return raw

    def _encode_number(self, field: CddDataObject, value: Any) -> int:
        if field.bit_length is None:
            raise ValueError(f"CDD field '{_field_key(field)}' has no bit length.")
        encoding = (field.encoding or "").lower()
        if field.text_mappings and isinstance(value, str):
            matching = [
                mapping.start
                for mapping in field.text_mappings
                if mapping.text == value and mapping.start == mapping.end
            ]
            if len(matching) != 1:
                raise ValueError(
                    f"CDD text value '{value}' is not a unique choice for "
                    f"field '{_field_key(field)}'."
                )
            value = matching[0]
        elif field.text_mappings and not isinstance(value, (int, float)):
            raise ValueError(
                f"CDD field '{_field_key(field)}' requires a text choice or "
                "numeric raw value."
            )
        elif field.linear_compensations:
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(
                    f"CDD field '{_field_key(field)}' requires a numeric "
                    "physical value."
                )
            compensation = self._compensation_for_physical(
                field.linear_compensations,
                float(value),
            )
            if compensation is None:
                raise ValueError(
                    f"CDD physical value {value} is outside the linear-"
                    f"compensation ranges for field '{_field_key(field)}'."
                )
            raw_value = (float(value) - compensation.offset) / compensation.factor
            if not math.isfinite(raw_value):
                raise ValueError(f"CDD field '{_field_key(field)}' has invalid scaling.")
            value = round(raw_value)
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(
                f"CDD field '{_field_key(field)}' requires an integer raw value."
            )
        if encoding not in {"uns", "sgn"}:
            raise ValueError(
                f"CDD field '{_field_key(field)}' uses unsupported encoding "
                f"'{field.encoding}'."
            )
        minimum = -(1 << (field.bit_length - 1)) if field.signed else 0
        maximum = (
            (1 << (field.bit_length - 1)) - 1
            if field.signed
            else (1 << field.bit_length) - 1
        )
        if not minimum <= value <= maximum:
            raise ValueError(
                f"CDD value for '{_field_key(field)}' must be between "
                f"{minimum} and {maximum}."
            )
        return value & ((1 << field.bit_length) - 1)

    def _require_supported_leaf(self, field: CddDataObject) -> None:
        if field.definition_type not in self._SUPPORTED_LEAF_TYPES:
            raise ValueError(
                f"CDD field '{_field_key(field)}' uses unsupported definition "
                f"'{field.definition_type}'."
            )

    def _decode_string(self, field: CddDataObject, raw: bytes) -> str:
        encoding = (field.encoding or "").lower()
        if encoding == "asc":
            try:
                return raw.decode("ascii")
            except UnicodeDecodeError as exc:
                raise ValueError(
                    f"CDD ASCII field '{_field_key(field)}' contains non-ASCII data."
                ) from exc
        if encoding == "bcd":
            digits: list[str] = []
            nibbles = [
                nibble
                for byte in raw
                for nibble in (byte >> 4, byte & 0x0F)
            ]
            if nibbles and nibbles[-1] == 0xF:
                nibbles.pop()
            for nibble in nibbles:
                if nibble > 9:
                    raise ValueError(
                        f"CDD BCD field '{_field_key(field)}' contains "
                        f"invalid digit 0x{nibble:X}."
                    )
                digits.append(str(nibble))
            return "".join(digits)
        raise ValueError(f"Unsupported CDD string encoding '{field.encoding}'.")

    def _encode_string(self, field: CddDataObject, value: Any) -> bytes:
        if not isinstance(value, str):
            raise ValueError(
                f"CDD field '{_field_key(field)}' requires a string value."
            )
        encoding = (field.encoding or "").lower()
        if encoding == "asc":
            try:
                raw = value.encode("ascii")
            except UnicodeEncodeError as exc:
                raise ValueError(
                    f"CDD ASCII field '{_field_key(field)}' accepts ASCII only."
                ) from exc
        elif encoding == "bcd":
            if not value.isascii() or not value.isdecimal():
                raise ValueError(
                    f"CDD BCD field '{_field_key(field)}' accepts decimal digits only."
                )
            digits = value + ("F" if len(value) % 2 else "")
            raw = bytes(
                int(digits[index : index + 2], 16)
                for index in range(0, len(digits), 2)
            )
        else:
            raise ValueError(f"Unsupported CDD string encoding '{field.encoding}'.")
        if field.quantity == "field":
            self._validate_element_count(field, len(raw))
        elif field.bit_length is not None and len(raw) * 8 != field.bit_length:
            raise ValueError(
                f"CDD field '{_field_key(field)}' requires exactly "
                f"{field.bit_length // 8} bytes."
            )
        if field.bit_length is not None and field.quantity != "field":
            if len(raw) * 8 != field.bit_length:
                raise ValueError(
                    f"CDD field '{_field_key(field)}' requires exactly "
                    f"{field.bit_length // 8} bytes."
                )
        return raw

    def _normalize_numeric_values(
        self,
        field: CddDataObject,
        value: Any,
    ) -> list[Any]:
        if field.quantity == "field":
            if not isinstance(value, list):
                raise ValueError(
                    f"CDD numeric field '{_field_key(field)}' requires a list of values."
                )
            return value
        return [value]

    def _element_count(
        self,
        field: CddDataObject,
        remaining_bits: int,
        is_last: bool,
    ) -> int:
        if field.quantity != "field":
            return 1
        minimum = field.minimum_size or 0
        maximum = field.maximum_size
        if maximum is not None and minimum == maximum:
            return minimum
        if not is_last:
            raise ValueError(
                f"Variable-length CDD field '{_field_key(field)}' must be last "
                "in its structure."
            )
        if field.encoding in {"asc", "bcd"}:
            if remaining_bits % 8:
                raise ValueError(
                    f"Variable-length CDD field '{_field_key(field)}' is not "
                    "byte-aligned."
                )
            count = remaining_bits // 8
        elif field.bit_length:
            if remaining_bits % field.bit_length:
                raise ValueError(
                    f"Variable-length CDD field '{_field_key(field)}' has an "
                    "incomplete final element."
                )
            count = remaining_bits // field.bit_length
        else:
            raise ValueError(
                f"Variable-length CDD field '{_field_key(field)}' has no bit length."
            )
        self._validate_element_count(field, count)
        return count

    def _validate_element_count(self, field: CddDataObject, count: int) -> None:
        if field.minimum_size is not None and count < field.minimum_size:
            raise ValueError(
                f"CDD field '{_field_key(field)}' requires at least "
                f"{field.minimum_size} elements."
            )
        if field.maximum_size is not None and count > field.maximum_size:
            raise ValueError(
                f"CDD field '{_field_key(field)}' allows at most "
                f"{field.maximum_size} elements."
            )

    @staticmethod
    def _byte_order(field: CddDataObject) -> int:
        if field.byte_order not in {12, 21}:
            raise ValueError(
                f"CDD field '{_field_key(field)}' uses unsupported byte order "
                f"'{field.byte_order}'."
            )
        return field.byte_order

    @staticmethod
    def _compensation_for_raw(
        compensations: tuple[CddLinearCompensation, ...],
        raw: int,
    ) -> CddLinearCompensation | None:
        return next(
            (
                compensation
                for compensation in compensations
                if (compensation.start is None or raw >= compensation.start)
                and (compensation.end is None or raw <= compensation.end)
            ),
            None,
        )

    @classmethod
    def _compensation_for_physical(
        cls,
        compensations: tuple[CddLinearCompensation, ...],
        physical: float,
    ) -> CddLinearCompensation | None:
        for compensation in compensations:
            if compensation.factor == 0:
                continue
            raw = (physical - compensation.offset) / compensation.factor
            if (
                (compensation.start is None or raw >= compensation.start)
                and (compensation.end is None or raw <= compensation.end)
            ):
                return compensation
        return None


def _field_key(field: CddDataObject) -> str:
    return field.qualifier or field.name


def _gap_width(field: CddDataObject) -> int:
    if field.gap_bit_length is None or field.gap_bit_length < 0:
        raise ValueError(
            f"CDD gap '{_field_key(field)}' has no valid bit length."
        )
    return field.gap_bit_length


class _BitReader:
    def __init__(self, data: bytes) -> None:
        self._data = data
        self._position = 0

    @property
    def remaining_bits(self) -> int:
        return len(self._data) * 8 - self._position

    def read_bits(self, width: int, byte_order: int) -> int:
        if width < 0 or width > self.remaining_bits:
            raise ValueError(
                f"CDD payload ended while reading {width} bits; "
                f"{self.remaining_bits} bits remain."
            )
        value = 0
        for index in range(width):
            absolute_bit = self._position + index
            byte_index, bit_index = divmod(absolute_bit, 8)
            if byte_order == 21:
                bit = (self._data[byte_index] >> (7 - bit_index)) & 1
                value = (value << 1) | bit
            else:
                bit = (self._data[byte_index] >> bit_index) & 1
                value |= bit << index
        self._position += width
        return value

    def read_bytes(self, width: int, byte_order: int) -> bytes:
        if width % 8:
            raise ValueError("CDD string fields must use a whole number of bytes.")
        return bytes(self.read_bits(8, byte_order) for _ in range(width // 8))


class _BitWriter:
    def __init__(self) -> None:
        self._data = bytearray()
        self._position = 0

    def write_bits(self, value: int, width: int, byte_order: int) -> None:
        if width < 0 or value < 0 or value >= 1 << width:
            raise ValueError(f"Value {value} does not fit in {width} bits.")
        for index in range(width):
            if self._position % 8 == 0:
                self._data.append(0)
            source_shift = width - index - 1 if byte_order == 21 else index
            target_shift = (
                7 - self._position % 8
                if byte_order == 21
                else self._position % 8
            )
            self._data[-1] |= ((value >> source_shift) & 1) << target_shift
            self._position += 1

    def write_bytes(self, value: bytes, byte_order: int) -> None:
        for byte in value:
            self.write_bits(byte, 8, byte_order)

    def to_bytes(self) -> bytes:
        if self._position % 8:
            raise ValueError(
                "CDD DID layout does not end on a byte boundary and cannot be "
                "encoded as a byte payload."
            )
        return bytes(self._data)
