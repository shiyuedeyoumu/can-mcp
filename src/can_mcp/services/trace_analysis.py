import math
from collections import defaultdict
from pathlib import Path

import can

from can_mcp.core.filters import FrameFilter
from can_mcp.core.models import CanFrame


def calculate_trace_stats(
    frames: list[CanFrame],
    bitrate: int = 500_000,
) -> dict[str, object]:
    if isinstance(bitrate, bool) or not isinstance(bitrate, int) or bitrate <= 0:
        raise ValueError("bitrate must be greater than zero.")

    grouped: dict[tuple[int, bool, bool], list[CanFrame]] = defaultdict(list)
    for frame in frames:
        grouped[(frame.arbitration_id, frame.is_extended_id, frame.is_fd)].append(frame)

    per_id: list[dict[str, object]] = []
    estimated_frame_bits = 0
    for (arbitration_id, is_extended_id, is_fd), id_frames in sorted(grouped.items()):
        ordered = sorted(
            (frame for frame in id_frames if frame.timestamp is not None),
            key=lambda frame: frame.timestamp,
        )
        periods = [
            current.timestamp - previous.timestamp
            for previous, current in zip(ordered, ordered[1:])
        ]
        average_period = sum(periods) / len(periods) if periods else None
        jitter = (
            math.sqrt(
                sum((period - average_period) ** 2 for period in periods)
                / len(periods)
            )
            if periods and average_period is not None
            else None
        )
        per_id.append(
            {
                "arbitration_id": arbitration_id,
                "is_extended_id": is_extended_id,
                "is_fd": is_fd,
                "count": len(id_frames),
                "average_period_seconds": average_period,
                "minimum_period_seconds": min(periods) if periods else None,
                "maximum_period_seconds": max(periods) if periods else None,
                "jitter_stddev_seconds": jitter,
            }
        )
        for frame in id_frames:
            base_bits = 67 if frame.is_extended_id else 47
            if frame.is_fd:
                base_bits += 20
            estimated_frame_bits += int((base_bits + len(frame.data) * 8) * 1.2)

    timestamps = [
        frame.timestamp for frame in frames if frame.timestamp is not None
    ]
    duration = max(timestamps) - min(timestamps) if len(timestamps) > 1 else 0.0
    bus_load = (
        estimated_frame_bits / bitrate / duration * 100
        if duration > 0
        else None
    )
    return {
        "frame_count": len(frames),
        "duration_seconds": duration,
        "bitrate": bitrate,
        "estimated_bus_load_percent": bus_load,
        "per_id": per_id,
        "bus_load_note": (
            "Approximate only; uses frame length plus estimated protocol overhead "
            "and does not model exact bit stuffing, arbitration, or CAN FD bit rates."
        ),
    }


def export_trace_frames(
    frames: list[CanFrame],
    path: str,
    frame_filter: FrameFilter | None = None,
) -> dict[str, object]:
    destination = Path(path).expanduser()
    destination.parent.mkdir(parents=True, exist_ok=True)
    extension = destination.suffix.lower()
    writer_types = {
        ".asc": can.ASCWriter,
        ".blf": can.BLFWriter,
        ".csv": can.CSVWriter,
    }
    writer_type = writer_types.get(extension)
    if writer_type is None:
        raise ValueError("Trace export path must use .asc, .blf or .csv extension.")

    selected = [
        frame
        for frame in frames
        if frame_filter is None or frame_filter.matches(frame)
    ]
    writer = writer_type(destination)
    try:
        for frame in selected:
            writer.on_message_received(
                can.Message(
                    timestamp=frame.timestamp if frame.timestamp is not None else 0.0,
                    arbitration_id=frame.arbitration_id,
                    data=frame.data,
                    is_extended_id=frame.is_extended_id,
                    is_fd=frame.is_fd,
                    bitrate_switch=frame.bitrate_switch,
                    is_rx=frame.is_rx,
                    channel=0,
                    check=True,
                )
            )
    finally:
        writer.stop()
    return {
        "status": "exported",
        "path": str(destination.resolve()),
        "format": extension[1:],
        "frame_count": len(selected),
    }
