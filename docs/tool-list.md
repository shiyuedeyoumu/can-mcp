# MCP 工具清单

当前服务注册 **65 个 MCP tools**。本清单按功能分类；工具名称以 MCP 注册名为准，说明已对照运行时 MCP tool descriptions 检查。服务端 tool descriptions 来源于各 handler 的 docstring；新增、移除或改变参数/行为时，请同步更新本表，并核对 `src/can_mcp/server.py` 中的注册。

## 物理 CAN

| 工具 | 范围 | 说明 |
|---|---|---|
| `connect_can` | PCAN / Vector | 连接指定的硬件接口和通道，可配置仲裁波特率、CAN FD 数据波特率、Vector 序列号及硬件 `listen_only`。不同 handle 可并发连接不同物理通道；省略 handle 使用 `default`，同一通道不能重复占用。 |
| `disconnect_can` | PCAN / Vector | 关闭指定 handle 的物理连接及接收监听器；重复断开返回 `already_disconnected`。 |
| `send_can_frame` | PCAN / Vector | 需 `confirm_physical_io=true`；在指定 handle 的物理连接发送标准/扩展 CAN 或 CAN FD 帧。经典 CAN 最多 8 字节；CAN FD payload 长度限于 0–8、12、16、20、24、32、48、64 字节，连接需先以 `fd=true` 建立。只听模式拒绝发送。 |
| `receive_can_frame` | PCAN / Vector | 从指定 handle 的物理接收队列等待一帧；超时返回 `null`。驱动标记为 error frame 的帧只更新状态和 trace，不进入普通接收队列。VirtualBus 请使用 `receive_virtual_frame`、`expect_virtual_frame` 或虚拟 trace 工具。 |
| `receive_can_frames` | PCAN / Vector | 首帧等待指定 timeout 后，批量排空指定 handle 队列中最多 `max_frames` 帧，降低高流量下逐次 MCP 调用开销。VirtualBus 请使用 `receive_virtual_frame`、`expect_virtual_frame` 或虚拟 trace 工具。 |
| `expect_frame` | PCAN / Vector | 只读等待指定 handle 的物理接收帧，可按 ID、前缀数据或逐位数据掩码匹配；在给定 timeout 内未命中会报超时。 |
| `get_status` | PCAN / Vector | 查询指定 handle 的物理连接参数、trace 状态、累计 TX/RX、接收缓冲丢帧、错误帧及驱动提供的 bus state；不支持读取的 bus state 为 `null`。 |
| `list_can_connections` | PCAN / Vector | 列出所有物理 CAN handle 的连接状态和累计计数；断开的连接标记 `stale=true`，以便保留其 trace 时也能识别陈旧 handle。 |
| `list_physical_can_feature_status` | PCAN / Vector | 列出已连接物理 CAN handle 的周期任务、trace 与本地 RX 故障注入状态。 |
| `list_pcan_feature_status` | PCAN / Vector | `list_physical_can_feature_status` 的兼容旧名。 |
| `configure_physical_receive_faults` | PCAN / Vector | 在本服务本地 RX/trace 观察路径模拟丢帧、延迟或乱序；不会在真实 CAN 线路注入错误或影响其他节点。 |
| `configure_pcan_receive_faults` | PCAN / Vector | `configure_physical_receive_faults` 的兼容旧名。 |
| `disable_physical_receive_faults` | PCAN / Vector | 关闭该物理 handle 的本地接收视图故障注入。 |
| `disable_pcan_receive_faults` | PCAN / Vector | `disable_physical_receive_faults` 的兼容旧名。 |
| `start_physical_cyclic` | PCAN / Vector | 需 `confirm_physical_io=true`；真实总线上周期发送固定帧，周期至少 100 ms、持续 1–60 秒并自动停止。 |
| `start_pcan_cyclic` | PCAN / Vector | `start_physical_cyclic` 的兼容旧名。 |
| `start_physical_frame_generator` | PCAN / Vector | 需 `confirm_physical_io=true`；以至少 100 ms 周期在真实总线上生成 counter/ramp/random/template 帧，持续 1–60 秒并自动停止。 |
| `start_pcan_frame_generator` | PCAN / Vector | `start_physical_frame_generator` 的兼容旧名。 |
| `stop_physical_cyclic` | PCAN / Vector | 停止由物理周期发送或帧生成器创建的任务。 |
| `stop_pcan_cyclic` | PCAN / Vector | `stop_physical_cyclic` 的兼容旧名。 |
| `run_physical_scenario` | PCAN / Vector | 需 `confirm_physical_io=true`；运行受限 send/wait/assert JSON 场景，最多 20 步、累计等待最多 60 秒；send 步骤真实发送 CAN 帧。 |
| `run_pcan_scenario` | PCAN / Vector | `run_physical_scenario` 的兼容旧名。 |
| `physical_uds_request` | PCAN / Vector | 需 `confirm_physical_io=true`；通过 ISO-TP 进行定向 UDS 只读请求，只允许 0x19/0x22，不支持写入、解锁、会话切换或 ECU 复位。默认 P2 响应等待为 1000 ms，可显式调整。 |
| `pcan_uds_request` | PCAN / Vector | `physical_uds_request` 的兼容旧名。 |
| `physical_uds_functional_request` | PCAN / Vector | 需 `confirm_physical_io=true`；在实体 CAN 上进行功能寻址 UDS 只读请求，只允许 0x19/0x22，可能触达多个 ECU。 |
| `pcan_uds_functional_request` | PCAN / Vector | `physical_uds_functional_request` 的兼容旧名。 |
| `forward_physical_frame` | PCAN / Vector | 需 `confirm_physical_io=true`；从一个物理 handle 的接收队列向另一个物理 handle 单次转发一帧，成功/超时均返回非空文本与结构化结果；不转发错误帧，不创建后台转发任务。 |
| `decode_error_frame` | 物理 / VirtualBus | 只读返回最近观察到的错误帧、累计错误帧数和可用 bus state；指定 `handle` 路由到同名物理或虚拟连接（同名时物理优先），`bus_handle` 保留为旧版物理选择参数。不推断厂商专有错误位。 |
| `enumerate_pcan_devices` | PCAN | 只探测、不打开通道；返回可用于连接的 PCAN channel、FD 能力及驱动提供的设备信息。 |
| `enumerate_vector_devices` | Vector | 只探测、不打开通道；返回 Vector channel 索引、serial、硬件通道类型及 FD 能力，结果可能包含 Vector Virtual CAN。 |

## 物理 CAN trace

| 工具 | 范围 | 说明 |
|---|---|---|
| `start_trace` | PCAN / Vector | 开始指定 handle 的物理内存 trace；可在捕获时按 ID/掩码、标准/扩展帧、CAN/CAN FD、RX/TX 过滤。启动会清空该 handle 的旧缓存，最多保存 300,000 帧；已运行时重复调用不重置缓存。 |
| `stop_trace` | PCAN / Vector | 停止指定 handle 的物理 trace，但保留已捕获帧供查询、统计和导出；不会断开总线。 |
| `get_trace` | PCAN / Vector | 查询指定 handle 已捕获的物理 RX/TX 帧，支持 ID/掩码、帧格式、方向和 limit 过滤；未启动过 trace 会报错，停止后仍可查询。 |
| `get_trace_stats` | PCAN / Vector | 对指定 handle 已捕获物理 trace 按 CAN ID 统计帧数、平均/最小/最大周期、抖动和估算负载；负载是近似值，不等同物理测量。 |
| `export_trace` | PCAN / Vector | 按可选 ID/掩码、帧格式、方向和 limit 筛选指定 handle 的物理 trace，导出到 `uploads/` 为 `.asc`、`.blf` 或 `.csv`；不提供文件名时生成唯一 ASC 文件。 |

## 数据库与 DBC 信号

| 工具 | 范围 | 说明 |
|---|---|---|
| `load_database` | 离线 | 仅通过 `upload_finish` 返回的 `uploads/` 相对标识加载 DBC 或 CANdela CDD；不接受服务器任意本地路径。加载响应包含统一的 `format` 字段、摘要和小型预览，不回传整个 CDD 目录。CDD 目录与 DID 编解码可用下方工具。 |
| `list_diagnostic_services` | CDD | 按名称、限定符或快捷名称搜索当前 CDD 诊断服务，支持 offset/limit 分页（每页最多 100 条）。 |
| `get_did` | CDD | 按 DID 十进制数值查询完整的 CDD DID 数据对象及编码元数据；`identifier=61831` 即 `0xF187`。 |
| `decode_did` | CDD | 按 DID 和十六进制原始 payload 解码字段；payload 不包含 UDS 服务/DID 前缀，字段以 CDD qualifier 为键。支持整数、BCD、ASCII、TEXTTBL、LINCOMP、嵌套结构和列表。 |
| `encode_did` | CDD | 按 DID 和 qualifier 到值的映射生成十六进制原始 payload；仅编码数据，不生成 UDS 服务/DID 前缀，也不发送 CAN 帧。 |
| `decode_frame` | 离线 | 用当前已加载 DBC 按 CAN ID、payload 和标准/扩展帧标志解码，返回消息信息及按 DBC 缩放的信号值。 |
| `encode_frame` | 离线 | 用当前 DBC 将消息名和信号物理值编码为 CAN ID/payload；仅生成数据，不发送。 |
| `send_signal` | VirtualBus / PCAN / Vector | 将 DBC 消息的信号值编码并发送到指定 handle；物理总线必须传 `confirm_physical_io=true`。 |
| `read_signal` | VirtualBus / PCAN / Vector | 在指定 handle 等待 DBC 消息帧并返回指定信号的解码物理值；超时明确报错。 |

## VirtualBus 生命周期与收发

| 工具 | 范围 | 说明 |
|---|---|---|
| `connect_virtual_bus` | VirtualBus | 创建进程内 python-can VirtualBus handle；相同 channel 的不同 handles 可互通，不同 channel 隔离。每服务最多 16 个 handle；不打开物理硬件。 |
| `disconnect_virtual_bus` | VirtualBus | 关闭指定 handle，并停止其周期任务、trace 和故障注入资源；不影响其他 handles。 |
| `list_virtual_buses` | VirtualBus | 列出 handles 的 channel/FD 配置、TX/RX 与错误计数、周期任务数、trace 状态/帧数及故障注入配置和计数。 |
| `send_virtual_frame` | VirtualBus | 向指定虚拟 handle 发送一帧；支持标准/扩展 ID、经典 CAN/CAN FD 和 BRS，FD handle 必须以 `fd=true` 创建。 |
| `receive_virtual_frame` | VirtualBus | 从指定 handle 接收一帧；超时返回 `null`。只有同 channel 的其他 handle 所发的帧会被接收，VirtualBus 不回送本 handle 自己发送的帧。 |
| `forward_virtual_frame` | VirtualBus | 从源 handle FIFO 取一帧并通过目标 handle 发到其 channel；可用 `drain=true` 丢弃当前排队旧帧。目标 handle 自己不接收回环副本，等待期间不阻塞其他工具调用。 |
| `expect_virtual_frame` | VirtualBus | 在指定虚拟 handle 按 ID、可选标准/扩展标志及数据前缀/掩码等待接收帧；timeout 未命中会报错。 |

## VirtualBus trace

| 工具 | 范围 | 说明 |
|---|---|---|
| `start_virtual_trace` | VirtualBus | 对指定 handle 开始内存 trace，可按 ID/掩码、扩展帧、CAN FD、RX/TX 过滤；每次新启动清空旧缓存。 |
| `stop_virtual_trace` | VirtualBus | 停止捕获并保留虚拟 trace 缓存，以供查询、统计或导出。 |
| `get_virtual_trace` | VirtualBus | 查询虚拟 trace，支持 ID/掩码、帧格式、方向过滤和 limit；只在至少启动过一次 trace 后可查询。 |
| `get_virtual_trace_stats` | VirtualBus | 按 CAN ID 统计虚拟 trace 帧数、周期、抖动和估算负载；CAN FD 双速率及精确位填充不在估算内。 |
| `export_virtual_trace` | VirtualBus | 按筛选条件和 limit 导出虚拟 trace 到 `uploads/` 为 `.asc`、`.blf` 或 `.csv`；不提供文件名时生成唯一 ASC 文件。 |

## VirtualBus 诊断、周期发送与测试

| 工具 | 范围 | 说明 |
|---|---|---|
| `uds_request` | VirtualBus | 对虚拟 ECU 发起物理寻址 UDS 请求，自动处理 ISO-TP 单帧/多帧和响应重组；显式提供请求/响应 ID。`timeout` 是事务总时限，P2/P2* 控制响应窗口，S3 仅作为测试参数返回，不实现 ECU 会话状态机。 |
| `uds_functional_request` | VirtualBus | 默认向 `0x7DF` 发送标准 ID 功能寻址请求，限 ISO-TP 单帧（最多 7 字节），在 P2/P2* 窗口收集多个 ECU 响应并解码 NRC。 |
| `run_scenario` | VirtualBus | 执行 JSON `send` / `wait` / `assert` 步骤，最多 20 步、累计等待最多 60 秒；返回逐步 PASS/FAIL，不执行 shell 或任意脚本。 |
| `start_cyclic` | VirtualBus | 按 10–60,000 ms 周期持续发送固定帧；首帧立即发送，最多 64 个周期任务，断开 handle 时自动停止。 |
| `stop_cyclic` | VirtualBus | 停止 `start_cyclic` 或 `start_frame_generator` 返回的 `cyclic_id`；任务已不存在时返回 `not_found`。 |
| `configure_virtual_faults` | VirtualBus | 仅对指定 handle 的接收路径设置 `drop_rate`/`reorder_rate`（0–1）和随机 `max_delay_ms`（0–60,000）；可选 `seed` 复现随机决策，不影响物理总线。 |
| `disable_virtual_faults` | VirtualBus | 关闭指定 handle 的故障注入；未交付延迟帧会丢弃，相关丢弃数计入注入指标。 |
| `start_frame_generator` | VirtualBus | 创建周期生成任务：counter 修改指定 payload 字节，ramp 递增指定 1–4 字节大端字段，random 可用 seed 重现，template 可用 `{counter}`/`{value}` 占位符。首帧立即发送，使用 `stop_cyclic` 停止。 |

## 文件传输

文件只能通过以下五个工具进入或离开 `uploads/` 沙箱；MCP 客户端只看到 `uploads/` 相对标识，不会看到服务端物理路径。文件和未完成上传在 24 小时后清理，单文件最大 64 MB，单块最大 512 KB。

| 工具 | 范围 | 说明 |
|---|---|---|
| `upload_begin` | uploads 沙箱 | 开始上传或通过相同 `upload_id` 续传；限制扩展名白名单，可选声明 SHA-256。 |
| `upload_chunk` | uploads 沙箱 | 按精确 offset 追加 base64 数据块；偏移错位或块超限明确报错。 |
| `upload_finish` | uploads 沙箱 | 校验大小和 SHA-256 并完成上传；返回可供 `load_database` 使用的 `uploads/` 相对标识，校验失败删除临时文件。 |
| `download_file` | uploads 沙箱 | 按 offset 分块读取文件，返回 base64 数据、大小和 SHA-256；拒绝沙箱外路径。 |
| `list_files` | uploads 沙箱 | 列出未过期文件的沙箱相对标识、大小和 UTC 修改时间；过期文件会被清理。 |

## 非 tool MCP 接口

下列能力不是 MCP tools，不计入 65 个工具：

| 接口 | 说明 |
|---|---|
| Resource `can://status` | 默认物理连接状态、所有物理 handle、物理 CAN 任务/故障注入状态（并保留 `pcan_features` 兼容字段）和 VirtualBus handle 列表。 |
| Resource `can://recent-trace` | 最近的物理及虚拟 trace 帧。 |
| Resource `can://dbc-summary` | 当前加载的 DBC 摘要。 |
| HTTP `GET /metrics` | Prometheus text format 指标，包括物理接收队列不同丢弃原因、trace 覆盖计数及物理/虚拟本地故障注入计数。 |
