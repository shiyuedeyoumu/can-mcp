import can

from can_mcp.core.models import CanFrame
from can_mcp.services.trace_analysis import calculate_trace_stats, export_trace_frames


def test_trace_statistics_include_per_id_periods_and_load_estimate() -> None:
    frames = [
        CanFrame(arbitration_id=0x123, data=b"\x01", timestamp=1.0),
        CanFrame(arbitration_id=0x123, data=b"\x02", timestamp=1.1),
        CanFrame(arbitration_id=0x123, data=b"\x03", timestamp=1.3),
    ]

    stats = calculate_trace_stats(frames, bitrate=500_000)

    assert stats["frame_count"] == 3
    assert stats["estimated_bus_load_percent"] is not None
    entry = stats["per_id"][0]
    assert entry["count"] == 3
    assert abs(entry["average_period_seconds"] - 0.15) < 1e-9
    assert abs(entry["minimum_period_seconds"] - 0.1) < 1e-9
    assert abs(entry["maximum_period_seconds"] - 0.2) < 1e-9
    assert entry["jitter_stddev_seconds"] is not None


def test_trace_export_writes_csv_asc_and_blf(tmp_path) -> None:
    frames = [
        CanFrame(
            arbitration_id=0x123,
            data=b"\x01\x02",
            timestamp=1.0,
            is_rx=True,
        )
    ]

    for extension in (".csv", ".asc", ".blf"):
        path = tmp_path / f"trace{extension}"
        result = export_trace_frames(frames, str(path))
        assert result["status"] == "exported"
        assert result["frame_count"] == 1
        assert path.is_file()
        assert path.stat().st_size > 0
        reader = can.LogReader(path)
        try:
            exported = list(reader)
        finally:
            reader.stop()
        assert len(exported) == 1
        assert exported[0].arbitration_id == 0x123
