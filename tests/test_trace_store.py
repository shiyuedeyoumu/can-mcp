import pytest

from can_mcp.core.filters import FrameFilter
from can_mcp.core.models import CanFrame
from can_mcp.core.errors import TraceNotStartedError
from can_mcp.services.trace_service import TraceService
from can_mcp.storage.trace_store import TraceStore


def test_trace_store_keeps_newest_frames_and_filters_queries() -> None:
    store = TraceStore(max_size=2)
    store.append(CanFrame(arbitration_id=1, data=b"\x01"))
    store.append(CanFrame(arbitration_id=2, data=b"\x02"))
    store.append(CanFrame(arbitration_id=2, data=b"\x03"))

    frames = store.query(FrameFilter(arbitration_id=2), limit=1)

    assert len(frames) == 1
    assert frames[0].data == b"\x03"


def test_trace_store_rejects_nonpositive_limits() -> None:
    with pytest.raises(ValueError):
        TraceStore().query(limit=0)


def test_trace_query_requires_trace_to_have_started() -> None:
    with pytest.raises(TraceNotStartedError, match="Trace has not been started"):
        TraceService().query()
