from can_mcp.core.filters import FrameFilter
from can_mcp.core.models import CanFrame


def test_filter_matches_masked_identifier_and_frame_flags() -> None:
    frame = CanFrame(
        arbitration_id=0x18FEF100,
        data=b"\x01",
        is_extended_id=True,
        is_fd=True,
        is_rx=True,
    )
    frame_filter = FrameFilter(
        arbitration_id=0x18FE0000,
        arbitration_id_mask=0x1FFF0000,
        is_extended_id=True,
        is_fd=True,
        is_rx=True,
    )

    assert frame_filter.matches(frame)


def test_filter_rejects_mismatched_identifier() -> None:
    frame = CanFrame(arbitration_id=0x123, data=b"\x00")

    assert not FrameFilter(arbitration_id=0x124).matches(frame)
