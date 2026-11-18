from can_mcp.adapters.can.pcan import PcanBusAdapter
from can_mcp.adapters.can.vector import VectorBusAdapter


class DeviceTools:
    def enumerate_pcan_devices(self) -> dict[str, object]:
        """枚举 PCAN 驱动当前检测到的 CAN 通道。

        此工具只探测设备，不会打开通道或改变当前连接。返回的 channel 可直接
        用于 connect_can。无设备、驱动不可用或后端无法自动探测时，devices
        可能为空；请检查 PCAN-Basic 驱动和硬件连接。

        Returns:
            包含 interface="pcan"、count 和 devices 的对象。设备项由 python-can
            提供，通常包含 channel、supports_fd、device_name、device_id 和
            channel_condition 等信息。
        """
        devices = PcanBusAdapter.detect_channels()
        return {
            "interface": "pcan",
            "count": len(devices),
            "devices": devices,
        }

    def enumerate_vector_devices(self) -> dict[str, object]:
        """枚举 Vector XL 驱动检测到的 Vector CAN 通道和硬件。

        此工具只探测设备，不会打开通道或改变当前连接。返回的 channel 是从 0
        开始的 python-can 通道索引，可直接传给 connect_can；serial 可用于选择
        具体硬件。结果也可能包含 Vector Virtual CAN 通道，选择物理设备时根据
        hw_type、serial 和 hw_channel 判断。VN1640A 需安装与 Python 位数匹配的
        Vector XL Driver Library；若设备不可见，请检查驱动和 Vector Hardware
        Configuration。

        Returns:
            包含 interface="vector"、count 和 devices 的对象。每项含
            channel、serial、channel_index、hw_type、hw_index、hw_channel
            和 supports_fd 等可序列化字段。
        """
        devices = VectorBusAdapter.detect_channels()
        return {
            "interface": "vector",
            "count": len(devices),
            "devices": devices,
        }
