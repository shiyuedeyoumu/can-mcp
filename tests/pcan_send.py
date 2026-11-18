import can
import time

def send_single_message():
    # 1. 配置并打开 PCAN 总线
    # channel 需根据你的硬件修改，常用的是 PCAN_USBBUS1
    with can.Bus(interface='pcan', channel='PCAN_USBBUS3', bitrate=500000) as bus:

        # 2. 创建 CAN 报文
        # arbitration_id: 仲裁ID
        # data: 数据字节列表 (0-8字节)
        # is_extended_id: True 为扩展帧(29位ID)，False 为标准帧(11位ID)
        msg = can.Message(
            arbitration_id=0x123,
            data=[0x11, 0x22, 0x33, 0x44, 0x55, 0x66, 0x77, 0x88],
            is_extended_id=False
        )

        # 3. 发送报文
        try:
            bus.send(msg)
            print(f"报文发送成功: ID=0x{msg.arbitration_id:X}, 数据={msg.data.hex()}")
        except can.CanError as e:
            print(f"报文发送失败: {e}")

if __name__ == "__main__":
    send_single_message()