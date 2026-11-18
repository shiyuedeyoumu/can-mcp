from can_mcp.adapters.can.base import PythonCanBusAdapter
from can_mcp.adapters.can.pcan import PcanBusAdapter
from can_mcp.adapters.can.vector import VectorBusAdapter
from can_mcp.adapters.can.base import CanConfig, PythonCanBusAdapter


_ADAPTERS: dict[str, type[PythonCanBusAdapter]] = {
    "pcan": PcanBusAdapter,
    "vector": VectorBusAdapter,
}


def create_can_adapter(config: CanConfig) -> PythonCanBusAdapter:
    adapter_class = _ADAPTERS.get(config.interface.lower())
    if adapter_class is None:
        supported = ", ".join(sorted(_ADAPTERS))
        raise ValueError(
            f"Unsupported CAN interface '{config.interface}'. "
            f"Supported interfaces: {supported}."
        )
    return adapter_class(config)
