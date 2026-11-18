import math
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from can_mcp.core.errors import DatabaseFormatError
from can_mcp.core.interfaces import DatabaseParser


@dataclass(frozen=True)
class CddDataObject:
    name: str
    qualifier: str
    reference: str | None
    definition_type: str | None
    encoding: str | None
    bit_length: int | None
    byte_order: int | None
    minimum_size: int | None
    maximum_size: int | None
    quantity: str | None = None
    data_format: str | None = None
    signed: bool = False
    linear_compensations: tuple["CddLinearCompensation", ...] = ()
    text_mappings: tuple["CddTextMapping", ...] = ()
    children: tuple["CddDataObject", ...] = ()
    gap_bit_length: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "qualifier": self.qualifier,
            "reference": self.reference,
            "definition_type": self.definition_type,
            "encoding": self.encoding,
            "bit_length": self.bit_length,
            "byte_order": self.byte_order,
            "minimum_size": self.minimum_size,
            "maximum_size": self.maximum_size,
            "quantity": self.quantity,
            "data_format": self.data_format,
            "signed": self.signed,
            "linear_compensations": [
                compensation.to_dict()
                for compensation in self.linear_compensations
            ],
            "text_mappings": [mapping.to_dict() for mapping in self.text_mappings],
            "gap_bit_length": self.gap_bit_length,
            "children": [child.to_dict() for child in self.children],
        }


@dataclass(frozen=True)
class CddLinearCompensation:
    start: int | None
    end: int | None
    factor: float
    offset: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "start": self.start,
            "end": self.end,
            "factor": self.factor,
            "offset": self.offset,
        }


@dataclass(frozen=True)
class CddTextMapping:
    start: int
    end: int
    text: str

    def to_dict(self) -> dict[str, Any]:
        return {"start": self.start, "end": self.end, "text": self.text}


@dataclass(frozen=True)
class CddDataIdentifier:
    identifier: int
    name: str
    qualifier: str
    fields: tuple[CddDataObject, ...]

    def to_dict(self, include_fields: bool = True) -> dict[str, Any]:
        result: dict[str, Any] = {
            "identifier": self.identifier,
            "identifier_hex": f"0x{self.identifier:04X}",
            "name": self.name,
            "qualifier": self.qualifier,
        }
        if include_fields:
            result["fields"] = [field.to_dict() for field in self.fields]
        return result


@dataclass(frozen=True)
class CddService:
    name: str
    qualifier: str
    shortcut: str | None
    functional: bool | None
    physical: bool | None
    positive_response_physical: bool | None
    positive_response_functional: bool | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "qualifier": self.qualifier,
            "shortcut": self.shortcut,
            "functional": self.functional,
            "physical": self.physical,
            "positive_response_physical": self.positive_response_physical,
            "positive_response_functional": self.positive_response_functional,
        }


@dataclass(frozen=True)
class CddDatabase:
    path: str
    dtd_version: str | None
    manufacturer: str | None
    ecu_name: str | None
    data_identifiers: tuple[CddDataIdentifier, ...]
    services: tuple[CddService, ...]

    def describe(self) -> dict[str, Any]:
        return {
            "status": "loaded",
            "format": "cdd",
            "path": self.path,
            "dtd_version": self.dtd_version,
            "manufacturer": self.manufacturer,
            "ecu_name": self.ecu_name,
            "data_identifier_count": len(self.data_identifiers),
            "service_count": len(self.services),
            "data_identifiers": [
                did.to_dict() for did in self.data_identifiers
            ],
            "services": [service.to_dict() for service in self.services],
            "warnings": [
                "CDD DID codecs support unsigned/signed integers, BCD, ASCII, "
                "text mappings, and linear compensation; unsupported data "
                "definitions are rejected."
            ],
        }

    def summary(self) -> dict[str, Any]:
        return {
            "status": "loaded",
            "format": "cdd",
            "path": self.path,
            "dtd_version": self.dtd_version,
            "manufacturer": self.manufacturer,
            "ecu_name": self.ecu_name,
            "data_identifier_count": len(self.data_identifiers),
            "service_count": len(self.services),
            "data_identifiers": [
                did.to_dict(include_fields=False) for did in self.data_identifiers
            ],
            "warnings": [
                "CDD DID codecs support unsigned/signed integers, BCD, ASCII, "
                "text mappings, and linear compensation; unsupported data "
                "definitions are rejected."
            ],
        }


class CddParser(DatabaseParser):
    @property
    def format_name(self) -> str:
        return "cdd"

    def parse(self, path: Path) -> CddDatabase:
        try:
            content = path.read_bytes()
            if re.search(
                rb"<!ENTITY\b",
                content.replace(b"\x00", b""),
                re.IGNORECASE,
            ):
                raise DatabaseFormatError(
                    "CDD documents with custom XML entity declarations are not supported."
                )
            root = ET.fromstring(content)
        except DatabaseFormatError:
            raise
        except (OSError, ET.ParseError) as exc:
            raise DatabaseFormatError(
                f"Could not parse CDD XML file '{path}': {exc}"
            ) from exc

        if root.tag != "CANDELA":
            raise DatabaseFormatError(
                f"Unsupported CDD document root '{root.tag}'; expected 'CANDELA'."
            )
        ecudoc = root.find("ECUDOC")
        if ecudoc is None:
            raise DatabaseFormatError("CDD document is missing the ECUDOC element.")

        references = {
            element.get("id"): element
            for element in root.iter()
            if element.get("id") is not None
        }
        data_identifiers = self._parse_data_identifiers(ecudoc, references, path)
        services = self._parse_services(ecudoc)
        ecu = ecudoc.find("ECU")

        return CddDatabase(
            path=str(path.resolve()),
            dtd_version=root.get("dtdvers"),
            manufacturer=ecudoc.get("manufacturer"),
            ecu_name=_element_name(ecu) if ecu is not None else None,
            data_identifiers=tuple(data_identifiers),
            services=tuple(services),
        )

    @classmethod
    def _parse_data_identifiers(
        cls,
        ecudoc: ET.Element,
        references: dict[str | None, ET.Element],
        path: Path,
    ) -> list[CddDataIdentifier]:
        parsed: list[CddDataIdentifier] = []
        seen_identifiers: set[int] = set()
        for element in ecudoc.findall(".//DID"):
            raw_identifier = element.get("n")
            try:
                identifier = int(raw_identifier, 10) if raw_identifier is not None else -1
            except ValueError as exc:
                raise DatabaseFormatError(
                    f"CDD file '{path}' contains a DID with invalid identifier "
                    f"'{raw_identifier}'."
                ) from exc
            if not 0 <= identifier <= 0xFFFF:
                raise DatabaseFormatError(
                    f"CDD file '{path}' contains DID '{raw_identifier}' outside "
                    "the 16-bit identifier range."
                )
            if identifier in seen_identifiers:
                raise DatabaseFormatError(
                    f"CDD file '{path}' contains duplicate DID 0x{identifier:04X}."
                )
            seen_identifiers.add(identifier)
            structure = element.find("STRUCTURE")
            fields = (
                tuple(
                    cls._parse_data_object(child, references, frozenset(), 0)
                    for child in structure
                    if child.tag in {"DATAOBJ", "STRUCT", "GAPDATAOBJ"}
                )
                if structure is not None
                else ()
            )
            parsed.append(
                CddDataIdentifier(
                    identifier=identifier,
                    name=_element_name(element),
                    qualifier=element.findtext("QUAL", default=""),
                    fields=fields,
                )
            )
        return parsed

    @classmethod
    def _parse_data_object(
        cls,
        element: ET.Element,
        references: dict[str | None, ET.Element],
        ancestors: frozenset[str],
        depth: int,
    ) -> CddDataObject:
        reference = element.get("dtref")
        definition = references.get(reference)
        has_inline_structure = any(
            child.tag in {"DATAOBJ", "STRUCT", "GAPDATAOBJ"}
            for child in element
        )
        definition_type = (
            element.tag
            if has_inline_structure and element.tag in {"STRUCT", "RECORD"}
            else definition.tag if definition is not None else None
        )
        c_value = definition.find("CVALUETYPE") if definition is not None else None
        if c_value is None:
            c_value = element.find("CVALUETYPE")
        encoding = c_value.get("enc") if c_value is not None else None

        children: list[CddDataObject] = []
        if definition_type in {"STRUCT", "RECORD"}:
            if depth >= 16 or (reference is not None and reference in ancestors):
                children.append(
                    CddDataObject(
                        name="",
                        qualifier="",
                        reference=reference,
                        definition_type=definition_type,
                        encoding=None,
                        bit_length=None,
                        byte_order=None,
                        minimum_size=None,
                        maximum_size=None,
                    )
                )
            else:
                next_ancestors = (
                    ancestors | {reference} if reference is not None else ancestors
                )
                structure = element if has_inline_structure else definition
                children.extend(
                    cls._parse_data_object(
                        child,
                        references,
                        next_ancestors,
                        depth + 1,
                    )
                    for child in (
                        structure if structure is not None else ()
                    )
                    if child.tag in {"DATAOBJ", "STRUCT", "GAPDATAOBJ"}
                )

        gap_bit_length = _integer_attribute(element, "bl")
        linear_compensations = tuple(
            CddLinearCompensation(
                start=_integer_attribute(compensation, "s"),
                end=_integer_attribute(compensation, "e"),
                factor=_float_attribute(compensation, "f", default=1.0),
                offset=_float_attribute(compensation, "o", default=0.0),
            )
            for compensation in definition.findall("COMP")
        ) if definition is not None else ()
        text_mappings: tuple[CddTextMapping, ...] = ()
        if definition is not None:
            mappings = []
            for mapping in definition.findall("TEXTMAP"):
                start = _integer_attribute(mapping, "s")
                end = _integer_attribute(mapping, "e")
                if start is None:
                    continue
                mappings.append(
                    CddTextMapping(
                        start=start,
                        end=end if end is not None else start,
                        text=_element_text(mapping.find("TEXT")) or "",
                    )
                )
            text_mappings = tuple(mappings)
        return CddDataObject(
            name=_element_name(element),
            qualifier=element.findtext("QUAL", default=""),
            reference=reference,
            definition_type=definition_type or (
                element.tag if element.tag != "DATAOBJ" else None
            ),
            encoding=encoding,
            bit_length=_integer_attribute(c_value, "bl"),
            byte_order=_integer_attribute(c_value, "bo"),
            minimum_size=_integer_attribute(c_value, "minsz"),
            maximum_size=_integer_attribute(c_value, "maxsz"),
            quantity=c_value.get("qty") if c_value is not None else None,
            data_format=c_value.get("df") if c_value is not None else None,
            signed=(
                encoding == "sgn"
                or (c_value is not None and c_value.get("sig") == "1")
            ),
            linear_compensations=linear_compensations,
            text_mappings=text_mappings,
            children=tuple(children),
            gap_bit_length=gap_bit_length,
        )

    @staticmethod
    def _parse_services(ecudoc: ET.Element) -> list[CddService]:
        services = []
        for diagnostic_instance in ecudoc.findall(".//DIAGINST"):
            for element in diagnostic_instance.findall("SERVICE"):
                services.append(
                    CddService(
                        name=_element_name(element),
                        qualifier=element.findtext("QUAL", default=""),
                        shortcut=element.findtext("SHORTCUTQUAL"),
                        functional=_boolean_attribute(element, "func"),
                        physical=_boolean_attribute(element, "phys"),
                        positive_response_physical=_boolean_attribute(
                            element, "respOnPhys"
                        ),
                        positive_response_functional=_boolean_attribute(
                            element, "respOnFunc"
                        ),
                    )
                )
        return services


def _element_name(element: ET.Element) -> str:
    if element is None:
        return ""
    name = element.find("NAME")
    if name is not None:
        for translation in name.findall("TUV"):
            text = (translation.text or "").strip()
            if text:
                return text
    return element.findtext("QUAL", default="")


def _element_text(element: ET.Element | None) -> str | None:
    if element is None:
        return None
    for translation in element.findall(".//TUV"):
        text = (translation.text or "").strip()
        if text:
            return text
    return None


def _integer_attribute(element: ET.Element | None, name: str) -> int | None:
    if element is None:
        return None
    value = element.get(name)
    if value is None:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _float_attribute(
    element: ET.Element,
    name: str,
    default: float,
) -> float:
    value = element.get(name)
    if value is None:
        return default
    try:
        result = float(value)
    except ValueError:
        raise DatabaseFormatError(
            f"CDD attribute '{name}' has invalid numeric value '{value}'."
        ) from None
    if not math.isfinite(result):
        raise DatabaseFormatError(
            f"CDD attribute '{name}' must be a finite number."
        )
    return result


def _boolean_attribute(element: ET.Element, name: str) -> bool | None:
    value = element.get(name)
    if value is None:
        return None
    if value in {"1", "true", "yes"}:
        return True
    if value in {"0", "false", "no"}:
        return False
    return None
