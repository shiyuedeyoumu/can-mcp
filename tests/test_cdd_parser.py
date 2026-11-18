from pathlib import Path

import pytest

from can_mcp.adapters.databases.cdd import (
    CddDataIdentifier,
    CddDataObject,
    CddDatabase,
    CddParser,
)
from can_mcp.adapters.databases.cdd_codec import CddCodec
from can_mcp.core.errors import DatabaseFormatError
from can_mcp.services.database_service import DatabaseService
from can_mcp.services.virtual_bus_service import VirtualBusService
from can_mcp.tools.database_tools import DatabaseTools


CDD_FIXTURES = sorted(Path(__file__).parent.glob("*.cdd"))
CDD_EXPECTED_COUNTS = {
    "SAIC_S11L_SW040_IPDV0.37.cdd": (148, 222),
    "SAIC_S11L_SW040_IPDV0.40.cdd": (148, 222),
    "SAIC_S11L_SW050_IPDV0.3.cdd": (209, 296),
    "SAIC_S11L_SW070_IPDV0.1.cdd": (302, 393),
    "SAIC_S11L_SW070_IPDV0.2.cdd": (303, 400),
}


@pytest.mark.parametrize("path", CDD_FIXTURES, ids=lambda path: path.name)
def test_parse_repository_cdd_fixtures(path: Path) -> None:
    database = CddParser().parse(path)

    assert database.dtd_version in {"11.0.100", "12.0.104", "13.0.104", "16.0.3"}
    assert database.ecu_name
    assert (
        len(database.data_identifiers),
        len(database.services),
    ) == CDD_EXPECTED_COUNTS[path.name]
    assert all(0 <= did.identifier <= 0xFFFF for did in database.data_identifiers)
    assert all(
        did.to_dict()["identifier_hex"].startswith("0x")
        for did in database.data_identifiers
    )


@pytest.mark.skipif(
    not CDD_FIXTURES,
    reason="Optional vehicle-specific CDD fixtures are not included.",
)
def test_parse_cdd_resolves_nested_data_objects() -> None:
    database = CddParser().parse(
        Path(__file__).with_name("SAIC_S11L_SW040_IPDV0.40.cdd")
    )
    nested_field = next(
        field
        for did in database.data_identifiers
        for field in did.fields
        if field.children
    )

    assert any(child.encoding for child in nested_field.children)


def test_cdd_rejects_invalid_xml_documents(tmp_path: Path) -> None:
    cases = {
        "wrong-root.cdd": "<ROOT><ECUDOC /></ROOT>",
        "missing-ecudoc.cdd": "<CANDELA />",
        "malformed.cdd": "<CANDELA><ECUDOC></CANDELA>",
        "entity.cdd": (
            '<!DOCTYPE CANDELA [<!ENTITY custom "value">]>'
            "<CANDELA><ECUDOC /></CANDELA>"
        ),
    }
    parser = CddParser()

    for filename, content in cases.items():
        path = tmp_path / filename
        path.write_text(content, encoding="utf-8")
        with pytest.raises(DatabaseFormatError):
            parser.parse(path)


def test_cdd_rejects_custom_entities_in_utf16_xml(tmp_path: Path) -> None:
    path = tmp_path / "entity-utf16.cdd"
    path.write_bytes(
        (
            '<!DOCTYPE CANDELA [<!ENTITY custom "value">]>'
            "<CANDELA><ECUDOC /></CANDELA>"
        ).encode("utf-16")
    )

    with pytest.raises(DatabaseFormatError, match="custom XML entity"):
        CddParser().parse(path)


@pytest.mark.parametrize(
    ("identifier", "message"),
    [
        ("not-a-number", "invalid identifier"),
        ("65536", "16-bit identifier range"),
    ],
)
def test_cdd_rejects_invalid_did_identifier(
    tmp_path: Path,
    identifier: str,
    message: str,
) -> None:
    path = tmp_path / "invalid-did.cdd"
    path.write_text(
        f'<CANDELA><ECUDOC><DID n="{identifier}">'
        "<QUAL>Example</QUAL></DID></ECUDOC></CANDELA>",
        encoding="utf-8",
    )

    with pytest.raises(DatabaseFormatError, match=message):
        CddParser().parse(path)


def test_cdd_rejects_duplicate_dids(tmp_path: Path) -> None:
    path = tmp_path / "duplicate-did.cdd"
    path.write_text(
        "<CANDELA><ECUDOC>"
        '<DID n="291"><QUAL>One</QUAL></DID>'
        '<DID n="291"><QUAL>Two</QUAL></DID>'
        "</ECUDOC></CANDELA>",
        encoding="utf-8",
    )

    with pytest.raises(DatabaseFormatError, match="duplicate DID 0x0123"):
        CddParser().parse(path)


@pytest.mark.skipif(
    not CDD_FIXTURES,
    reason="Optional vehicle-specific CDD fixtures are not included.",
)
def test_database_tools_load_cdd_catalog_and_reject_dbc_signal_operations() -> None:
    path = Path(__file__).with_name("SAIC_S11L_SW040_IPDV0.40.cdd")
    database = DatabaseService()
    tools = DatabaseTools(database, VirtualBusService())

    loaded = tools.load_database(str(path))

    assert loaded["status"] == "loaded"
    assert loaded["format"] == "cdd"
    assert loaded["data_identifier_count"] == 148
    assert loaded["service_count"] == 222
    assert database.summary()["format"] == "cdd"
    with pytest.raises(RuntimeError, match="CDD diagnostic catalog is loaded"):
        tools.decode_frame(0x123, "00")


@pytest.mark.skipif(
    not CDD_FIXTURES,
    reason="Optional vehicle-specific CDD fixtures are not included.",
)
def test_database_tools_query_services_and_did() -> None:
    database = DatabaseService()
    tools = DatabaseTools(database, VirtualBusService())
    tools.load_database(
        str(Path(__file__).with_name("SAIC_S11L_SW040_IPDV0.37.cdd"))
    )

    page = tools.list_diagnostic_services(search="DefaultSession_Start", limit=1)
    did = tools.get_did(61831)

    assert page["total_count"] == 1
    assert page["services"][0]["shortcut"] == "DefaultSession_Start"
    assert did["data_identifier"]["identifier_hex"] == "0xF187"
    assert did["data_identifier"]["fields"][0]["encoding"] == "bcd"


@pytest.mark.skipif(
    not CDD_FIXTURES,
    reason="Optional vehicle-specific CDD fixtures are not included.",
)
def test_cdd_service_query_rejects_invalid_pagination() -> None:
    database = DatabaseService()
    database.load(str(Path(__file__).with_name("SAIC_S11L_SW040_IPDV0.37.cdd")))

    with pytest.raises(ValueError, match="offset"):
        database.list_diagnostic_services(offset=-1)
    with pytest.raises(ValueError, match="limit"):
        database.list_diagnostic_services(limit=101)


@pytest.mark.skipif(
    not CDD_FIXTURES,
    reason="Optional vehicle-specific CDD fixtures are not included.",
)
def test_cdd_did_bcd_codec_preserves_leading_zeroes() -> None:
    database = DatabaseService()
    database.load(str(Path(__file__).with_name("SAIC_S11L_SW040_IPDV0.37.cdd")))
    values = {"ECU_Part_Number": "0012345678"}

    encoded = database.encode_data_identifier(61831, values)
    decoded = database.decode_data_identifier(61831, bytes.fromhex(encoded["data"]))

    assert encoded["data"] == "0012345678"
    assert decoded["fields"] == values


@pytest.mark.skipif(
    not CDD_FIXTURES,
    reason="Optional vehicle-specific CDD fixtures are not included.",
)
def test_cdd_did_codec_handles_compounds_arrays_and_linear_values() -> None:
    database = DatabaseService()
    database.load(str(Path(__file__).with_name("SAIC_S11L_SW040_IPDV0.37.cdd")))
    current_data = {
        "NCF_Part_Number": "1234567890",
        "Tester_Reference_Number": [1, 2, 3, 4, 5, 6],
        "Odometer_Reading": 0x123456,
        "Reserved": 0xABCD,
    }
    power_mode = {
        "Power_Mode": {
            "System_KL_R_status": "on",
            "System_KL_15_status": "off",
            "System_engine_run_active_status": "on",
            "System_KL_50_status": "off",
            "Global_KL_R_status": "on",
            "Global_KL_15_status": "off",
            "Global_engine_run_active_status": "on",
            "Global_KL_50_status": "off",
        }
    }

    current_encoded = database.encode_data_identifier(61729, current_data)
    current_decoded = database.decode_data_identifier(
        61729,
        bytes.fromhex(current_encoded["data"]),
    )
    mode_encoded = database.encode_data_identifier(53249, power_mode)
    mode_decoded = database.decode_data_identifier(
        53249,
        bytes.fromhex(mode_encoded["data"]),
    )
    scaled_encoded = database.encode_data_identifier(
        274,
        {"Supply_voltage": 12.3},
    )
    scaled_decoded = database.decode_data_identifier(274, b"\x7b")

    assert current_encoded["data"] == "1234567890010203040506123456abcd"
    assert current_decoded["fields"] == current_data
    assert mode_encoded["data"] == "aa"
    assert mode_decoded["fields"] == power_mode
    assert scaled_encoded["data"] == "7b"
    assert scaled_decoded["fields"]["Supply_voltage"] == 12.3


def test_cdd_did_codec_supports_little_endian_and_signed_scalars() -> None:
    data_identifier = CddDataIdentifier(
        identifier=1,
        name="Test",
        qualifier="Test",
        fields=(
            CddDataObject(
                name="Little endian",
                qualifier="little",
                reference=None,
                definition_type="IDENT",
                encoding="uns",
                bit_length=16,
                byte_order=12,
                minimum_size=1,
                maximum_size=1,
                quantity="atom",
            ),
            CddDataObject(
                name="Signed",
                qualifier="signed",
                reference=None,
                definition_type="IDENT",
                encoding="sgn",
                bit_length=8,
                byte_order=21,
                minimum_size=1,
                maximum_size=1,
                quantity="atom",
                signed=True,
            ),
        ),
    )
    codec = CddCodec()
    values = {"little": 0x1234, "signed": -2}

    payload = codec.encode(data_identifier, values)

    assert payload == bytes.fromhex("3412fe")
    assert codec.decode(data_identifier, payload)["fields"] == values


def test_cdd_did_codec_supports_variable_length_ascii_fields() -> None:
    data_identifier = CddDataIdentifier(
        identifier=2,
        name="Variable text",
        qualifier="Variable_Text",
        fields=(
            CddDataObject(
                name="Text",
                qualifier="Text",
                reference=None,
                definition_type="IDENT",
                encoding="asc",
                bit_length=8,
                byte_order=21,
                minimum_size=1,
                maximum_size=8,
                quantity="field",
            ),
        ),
    )
    codec = CddCodec()
    values = {"Text": "CANdela"}

    payload = codec.encode(data_identifier, values)

    assert payload == b"CANdela"
    assert codec.decode(data_identifier, payload)["fields"] == values


@pytest.mark.skipif(
    not CDD_FIXTURES,
    reason="Optional vehicle-specific CDD fixtures are not included.",
)
def test_cdd_did_codec_rejects_invalid_payload_and_values() -> None:
    database = DatabaseService()
    database.load(str(Path(__file__).with_name("SAIC_S11L_SW040_IPDV0.37.cdd")))

    with pytest.raises(ValueError, match="ended while reading"):
        database.decode_data_identifier(61831, b"\x12")
    with pytest.raises(ValueError, match="invalid digit"):
        database.decode_data_identifier(61831, bytes.fromhex("1a34567890"))
    with pytest.raises(KeyError, match="Missing value"):
        database.encode_data_identifier(61831, {})
    with pytest.raises(ValueError, match="decimal digits"):
        database.encode_data_identifier(61831, {"ECU_Part_Number": "12AB"})
    with pytest.raises(ValueError, match="outside the linear-compensation"):
        database.encode_data_identifier(274, {"Supply_voltage": 100.0})


@pytest.mark.skipif(
    not CDD_FIXTURES,
    reason="Optional vehicle-specific CDD fixtures are not included.",
)
def test_cdd_parser_result_is_a_typed_model() -> None:
    database = CddParser().parse(CDD_FIXTURES[0])

    assert isinstance(database, CddDatabase)
