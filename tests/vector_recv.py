#!/usr/bin/env python3
"""
Vector 接收端（独立进程）
官方文档：https://python-can.readthedocs.io/en/stable/interfaces/vector.html
"""

import can
import time
import datetime

# ==================== 配置区域 ====================
INTERFACE = "vector"

# 逻辑通道索引（从 0 开始）
# VN1640A：Channel 1 -> 0, Channel 2 -> 1, Channel 3 -> 2, Channel 4 -> 3
# VN5620:Channel 5 -> 4, Channel 6 -> 5
CHANNEL_RX = 0          # VN1640A 物理通道1（接收）

# Vector Hardware Config 中的应用名
# 文档：app_name 默认为 'CANalyzer'；设为 None 时 channel 应为全局索引
# 若不想依赖 VHC 手动配置，可配合 serial 参数使用（见下方说明）
# APP_NAME = "can-mcp"    # 与你在 VHC 中创建的应用名一致
APP_NAME = ""         # 备选：绕过应用配置，直接使用全局通道映射
# APP_NAME = None       # 备选：channel 需改为全局索引（见 detect_available_configs 输出）

BITRATE = 500000

# 硬件序列号（可选）
# 文档：若设置 serial，channel 仅指向该硬件上的通道，且 app_name 不必预先定义
# 从之前诊断输出可知你的 VN1640A 序列号为 33156
SERIAL = None           # 例如设为 33156 可强制绑定到该硬件
# SERIAL = 33156

# 只听模式（文档默认 False）
# 设为 True 时只监听、不发送、不产生 ACK；调试收发时务必保持 False
LISTEN_ONLY = False

# 接收过滤器：只接收 0x100~0x102，避免总线噪声干扰
USE_FILTER = True
# ==================================================


def on_message_received(msg: can.Message):
    """Notifier 回调：每收到一帧打印详细信息"""
    ts = datetime.datetime.fromtimestamp(msg.timestamp).strftime("%H:%M:%S.%f")[:-3]
    print(
        f"📥 [{ts}] ID=0x{msg.arbitration_id:X} | "
        f"DLC={msg.dlc} | data={msg.data.hex(' ').upper()} | "
        f"ext={msg.is_extended_id}"
    )


def main():
    # 构建过滤器
    can_filters = None
    if USE_FILTER:
        can_filters = [
            {"can_id": 0x100, "can_mask": 0x7FC, "extended": False},
        ]

    print("=" * 60)
    print("Vector 接收端")
    print(f"  interface    = {INTERFACE}")
    print(f"  channel_rx   = {CHANNEL_RX}  (逻辑索引，从 0 开始)")
    print(f"  app_name     = '{APP_NAME}'")
    print(f"  serial       = {SERIAL}")
    print(f"  bitrate      = {BITRATE}")
    print(f"  listen_only  = {LISTEN_ONLY}")
    print(f"  filter       = {'启用（0x100~0x102）' if USE_FILTER else '关闭'}")
    print("=" * 60)

    # 根据文档，VectorBus 支持 channel 为 int / Sequence / comma-separated str
    # 这里使用单个 int 作为接收通道
    try:
        bus = can.Bus(
            interface=INTERFACE,
            channel=CHANNEL_RX,
            app_name=APP_NAME,
            serial=SERIAL,
            bitrate=BITRATE,
            listen_only=LISTEN_ONLY,
            receive_own_messages=False,
            can_filters=can_filters,
        )
    except can.CanError as e:
        print(f"\n❌ 打开接收通道失败：{e}")
        print("\n请依次检查：")
        print(f"  1. Vector Hardware Config 中 app='{APP_NAME}' 下 channel={CHANNEL_RX} 是否已绑定到物理通道3")
        print("  2. 波特率是否与发送端一致（均为 500k）")
        print("  3. 是否有 CANoe/CANalyzer 独占该通道")
        return

    # 使用 Notifier 持续监听
    notifier = can.Notifier(bus, [on_message_received])

    # 文档：poll_interval 默认 0.01s；Notifier 启动需要短暂时间
    time.sleep(1.0)
    print("\n✅ 接收端已就绪，正在监听... 按 Ctrl+C 退出。\n")

    try:
        while True:
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("\n收到 Ctrl+C，正在停止...")
    finally:
        notifier.stop()
        bus.shutdown()
        print("接收端已退出。")


if __name__ == "__main__":
    main()