#!/usr/bin/env python3
"""
Vector 发送端（独立进程）
官方文档：https://python-can.readthedocs.io/en/stable/interfaces/vector.html
"""

import can
import time
import datetime

# ==================== 配置区域 ====================
INTERFACE = "vector"
# VN1640A：Channel 1 -> 0, Channel 2 -> 1, Channel 3 -> 2, Channel 4 -> 3
# VN5620:Channel 5 -> 4, Channel 6 -> 5
# 逻辑通道索引（从 0 开始）
CHANNEL_TX = 2          # VN1640A 物理通道3（发送）

# 与接收端保持一致的应用名
# APP_NAME = "can-mcp"
APP_NAME = ""
# APP_NAME = None

BITRATE = 500000

# 硬件序列号（可选）
SERIAL = None
# SERIAL = 33156

# 只听模式：发送端必须为 False（文档默认 False）
LISTEN_ONLY = False

# 启动后延迟发送，给接收端留出就绪时间
START_DELAY = 2.0

# 周期性发送参数
PERIODIC_COUNT = 20
PERIODIC_INTERVAL = 0.1
# ==================================================


def now_str():
    return datetime.datetime.now().strftime("%H:%M:%S.%f")[:-3]


def main():
    print("=" * 60)
    print("Vector 发送端")
    print(f"  interface    = {INTERFACE}")
    print(f"  channel_tx   = {CHANNEL_TX}")
    print(f"  app_name     = '{APP_NAME}'")
    print(f"  serial       = {SERIAL}")
    print(f"  bitrate      = {BITRATE}")
    print(f"  listen_only  = {LISTEN_ONLY}")
    print("=" * 60)

    try:
        bus = can.Bus(
            interface=INTERFACE,
            channel=CHANNEL_TX,
            app_name=APP_NAME,
            serial=SERIAL,
            bitrate=BITRATE,
            listen_only=LISTEN_ONLY,
            receive_own_messages=False,
        )
    except can.CanError as e:
        print(f"\n❌ 打开发送通道失败：{e}")
        print("\n请检查 Vector Hardware Config 中通道绑定与波特率配置。")
        return

    try:
        print(f"\n等待 {START_DELAY} 秒，确保接收端已就绪...")
        time.sleep(START_DELAY)

        # ---- 第一步：发送 3 帧测试报文 ----
        print("\n--- 发送 3 帧测试报文（0x100~0x102）---")
        for i in range(3):
            msg = can.Message(
                arbitration_id=0x100 + i,
                data=[0x11 + i, 0x22, 0x33, 0x44, 0x55, 0x66, 0x77, 0x88],
                is_extended_id=False,
            )
            try:
                # 文档：timeout > 0 时等待 ACK 或发送队列就绪
                bus.send(msg, timeout=1.0)
                print(
                    f"📤 [{now_str()}] ID=0x{msg.arbitration_id:X} | "
                    f"data={msg.data.hex(' ').upper()}"
                )
            except can.CanError as e:
                print(f"❌ [{now_str()}] 发送失败：{e}")
            time.sleep(0.2)

        # ---- 第二步：周期性发送 0x100 ----
        print(
            f"\n--- 周期性发送 0x100，共 {PERIODIC_COUNT} 次，"
            f"间隔 {PERIODIC_INTERVAL * 1000:.0f}ms ---"
        )
        for n in range(PERIODIC_COUNT):
            data = [n & 0xFF, 0xAA, 0xBB, 0xCC, 0xDD, 0xEE, 0x77, 0x88]
            msg = can.Message(arbitration_id=0x100, data=data, is_extended_id=False)
            try:
                bus.send(msg, timeout=1.0)
                print(
                    f"📤 [{now_str()}] #{n:02d} ID=0x100 | "
                    f"data={msg.data.hex(' ').upper()}"
                )
            except can.CanError as e:
                print(f"❌ [{now_str()}] #{n:02d} 发送失败：{e}")
            time.sleep(PERIODIC_INTERVAL)

        print("\n发送端完成。")

    finally:
        bus.shutdown()
        print("发送通道已关闭。")


if __name__ == "__main__":
    main()