# CAN MCP

基于 MCP 与 `python-can` 的 CAN/CAN FD 服务框架，支持按需连接 PCAN 或 Vector 硬件；CAN 收发、trace 和筛选不依赖具体厂商，可通过 `python-can` 后端扩展其他设备。

## 安装、启动与停止

### Ubuntu（推荐）

Ubuntu 上建议使用仓库提供的 systemd 服务脚本管理服务，而不是直接运行 Python 文件。先进入已克隆的项目目录（将示例路径替换为实际路径），创建虚拟环境并安装项目：

```sh
cd /path/to/can-mcp
python3 -m venv venv
./venv/bin/python -m pip install --upgrade pip
./venv/bin/python -m pip install -e .
```

配置项目根目录的 `config.toml` 后，以当前登录用户安装并启动服务：

```sh
RUN_USER="$(id -un)" ./scripts/install-service.sh install
```

安装脚本会创建 `/opt/can-mcp` 软链接和 `can-mcp.service` unit，启用开机启动并立即启动服务。服务账户需要有对应的同名用户组和 `/home/<用户名>` 主目录。查看状态及日志：

```sh
sudo systemctl status can-mcp
sudo journalctl -u can-mcp -f
```

启动、停止或重启服务：

```sh
sudo systemctl start can-mcp
sudo systemctl stop can-mcp
sudo systemctl restart can-mcp
```

移除服务并禁用开机启动：

```sh
./scripts/install-service.sh uninstall
```

`./scripts/install-service.sh status` 也可用于查询服务状态。更新代码或 Python 依赖后，在项目虚拟环境中重新执行 `./venv/bin/python -m pip install -e .`，再运行 `sudo systemctl restart can-mcp`。若修改了 service unit 的生成配置，重新运行安装命令。

systemd 停止服务时，Uvicorn 最多等待现有 HTTP 连接 5 秒；仍未结束的连接会被取消，避免长期打开的 Streamable HTTP/MCP 会话让进程无限等待。

### Windows（PowerShell）

仓库目前没有提供 Windows 服务安装脚本。创建虚拟环境、安装项目后，可通过安装生成的命令行入口启动服务；在运行窗口按 `Ctrl+C` 停止：

```powershell
cd C:\path\to\can-mcp
python -m venv venv
.\venv\Scripts\python.exe -m pip install --upgrade pip
.\venv\Scripts\python.exe -m pip install -e .
.\venv\Scripts\can-mcp.exe
```

若需要后台常驻或开机启动，请使用 Windows 服务管理工具配置 `venv\Scripts\can-mcp.exe`，并将工作目录设为项目根目录。

服务器监听配置由项目根目录下的 `config.toml` 管理。CAN 接口在 Agent 调用 `connect_can` 时选择。HTTP 服务默认监听 `127.0.0.1:13399`，MCP Streamable HTTP 端点为 `http://127.0.0.1:13399/mcp`。

可通过 `config.toml` 中的 `[server] host` 和 `port` 修改 HTTP 监听地址及端口，例如将 `host` 设为 `0.0.0.0` 可监听所有网卡。仅在可信网络环境中开放非本机地址。连接前请确认 PCAN 驱动、硬件及对应的 `python-can` 后端已安装。仓库提供的 service 安装脚本面向 Linux/systemd。

MCP `serverInfo.version` 格式为 `<包版本>+<启动日期 UTC>.<构建标识>`，例如 `0.1.0+20261109.abc123def456`。可通过 `CAN_MCP_BUILD` 指定构建标识；`scripts/install-service.sh` 会在 Git 工作区安装 systemd 服务时自动写入短 commit ID，非 Git 工作区则标记为 `unknown`。

## 当前 MCP 工具

完整的逐工具总表（当前注册 65 个 tools，另列 MCP resources 和 `/metrics`）见 [docs/tool-list.md](./docs/tool-list.md)。

- `connect_can` / `disconnect_can`：调用时指定 `interface`（`pcan` 或 `vector`）和 `channel` 建立或关闭物理连接；可通过不同 `handle` 并发连接设备提供的多个通道，同一物理通道不能被多个 handle 同时占用。省略 handle 使用 `default`，同一 handle 再次连接会切换其设备或通道。`listen_only=true` 启用硬件静默模式并禁止 MCP 主动发送。
- `list_can_connections` / `get_status`：列出各物理 handle 的连接状态，或查询指定 handle 的接口、通道、波特率、CAN FD/静默模式设置、trace 状态及累计 RX/TX 计数。
- `send_can_frame` / `receive_can_frame` / `receive_can_frames` / `expect_frame` 以及物理 trace 工具：可指定 `handle` 选择目标物理连接；省略时使用 `default`，与旧调用兼容。`receive_can_frames` 首帧等待后批量排空队列；物理接收队列与 trace 分别可保留 300,000 帧以吸收突发流量，容量溢出时状态和 metrics 报告丢弃计数。物理接收工具不接受 VirtualBus handle；虚拟总线请用 `receive_virtual_frame`、`expect_virtual_frame` 或虚拟 trace 工具。
- `start_physical_cyclic` / `start_physical_frame_generator` / `stop_physical_cyclic`：在 PCAN 或 Vector 上发送限时周期帧或 counter/ramp/random/template 生成帧。每次启动必须传 `confirm_physical_io=true`；最短周期 100 ms，持续 1–60 秒并自动停止。原 `start_pcan_*` / `stop_pcan_cyclic` 工具名继续兼容。
- `run_physical_scenario`：在真实 PCAN 或 Vector 上运行受限的 JSON send/wait/assert 场景，最多 20 步、累计等待最多 60 秒；send 会真实发送，必须显式确认。旧名 `run_pcan_scenario` 仍可用。
- `physical_uds_request` / `physical_uds_functional_request`：通过 PCAN 或 Vector/ISO-TP 发送需显式确认的 UDS 只读请求，只允许 0x19/0x22；不提供解锁、会话切换、写入、例程控制或复位服务。旧 `pcan_uds_*` 工具名仍可用。
- `configure_physical_receive_faults` / `disable_physical_receive_faults` / `list_physical_can_feature_status`：在本服务的 PCAN 或 Vector 接收视图中模拟丢帧、延迟、乱序，并查看周期任务和注入状态；不会造成真实线路故障。旧 `*_pcan_*` 工具名仍可用。
- `forward_physical_frame`：在两个物理 CAN handle 间单次转发一帧，需 `confirm_physical_io=true`；错误帧不会转发，也不会创建后台桥接任务。
- `enumerate_pcan_devices`：调用 python-can 的 PCAN 探测接口枚举当前可用通道，并返回设备名、FD 能力及驱动提供的设备信息。
- `enumerate_vector_devices`：调用 python-can Vector 后端枚举通道，并返回通道索引、序列号、硬件通道和 CAN FD 能力。
- `connect_virtual_bus` / `disconnect_virtual_bus` / `list_virtual_buses`：创建、关闭和查看进程内的虚拟总线 handle。仅使用 python-can VirtualBus，不连接物理 CAN 硬件。
- `send_virtual_frame` / `receive_virtual_frame`：在虚拟总线 handle 间发送和接收模拟帧。
- `start_cyclic` / `stop_cyclic`：仅在 VirtualBus 上启动/停止周期发送任务；首帧立即发送，周期范围 10–60000 毫秒，断开 handle 时自动停止，不会向物理 CAN 硬件发送。
- `configure_virtual_faults` / `disable_virtual_faults`：仅在指定 VirtualBus 接收 handle 上注入丢帧、随机延迟和乱序；可设置随机种子复现结果，`list_virtual_buses` 返回配置和注入计数。
- `start_frame_generator`：仅 VirtualBus 周期生成 counter、ramp、random 或 hex template 报文；复用 `stop_cyclic` 停止，并遵守相同的周期及任务数量上限。
- `decode_error_frame`：只读返回最近观察到的 python-can error frame、错误帧计数及驱动可提供的 bus state；不生成错误帧，也不推断厂商专有错误位。
- `uds_request`：仅在 VirtualBus 模拟网络上发送 UDS 请求；通过 `request_id` / `response_id` 指定模拟 ECU 地址，自动处理 ISO-TP 单帧、多帧、流控和响应重组。需先创建同一 channel 上的虚拟客户端和模拟 ECU handle；支持普通寻址，不会连接 PCAN/Vector。
- `uds_functional_request`：仅在 VirtualBus 上向标准 ID（默认 `0x7DF`）广播最多 7 字节的 UDS 请求，并在 P2/P2* 窗口收集多个 ECU 的 ISO-TP 单帧响应；解析 UDS NRC 名称。
- `run_scenario`：在 VirtualBus 上执行最多 20 步的受限 JSON `send` / `wait` / `assert` 场景，累计等待最多 60 秒，返回逐步 PASS/FAIL；不执行 shell 或脚本。
- `forward_virtual_frame`：从源 handle 的 FIFO 队列取一帧并向目标 channel 单次转发，用于软件级网关联调；必须由同一源 channel 上的另一个 handle 发帧，因为 VirtualBus 不回送 handle 自身发送的报文。可设 `drain=true` 丢弃当前队列中的旧帧。目标 handle 用作发送端，因此自身不会收到回环副本，同一目标 channel 上的其他 handle 可收到。等待期间不会阻塞其他 MCP 工具调用；不创建后台任务，也不转发到 PCAN/Vector。
- `start_virtual_trace` / `stop_virtual_trace` / `get_virtual_trace`：按虚拟 handle 捕获、筛选、查询 trace；每次重新启动会清空旧缓存。
- `get_virtual_trace_stats` / `export_virtual_trace`：计算虚拟 trace 的 per-ID 统计，或导出 `.asc`、`.blf`、`.csv`。
- `expect_virtual_frame`：按 ID 及可选数据掩码等待 VirtualBus 帧，超时明确报错。
- `send_can_frame`：发送标准帧或扩展帧，可选择 CAN FD 和 BRS；必须显式传入 `confirm_physical_io=true` 才会发送到物理总线。
- `receive_can_frame`：等待并获取一帧；开启 trace 时和 trace 共用同一接收通道。
- `start_trace` / `stop_trace` / `get_trace`：捕获、停止和查询 RX/TX 报文，可按 ID、掩码、方向和帧格式筛选。TX 在 `send_can_frame` 成功后记录到 trace，并标记 `is_rx=false`。
- `get_trace_stats` / `export_trace`：统计物理总线 trace 的 per-ID 帧数、平均/最小/最大周期、周期标准差抖动及估算负载；导出 `.asc`、`.blf` 或 `.csv`。
- MCP resources：`can://status`、`can://recent-trace`、`can://dbc-summary` 分别提供默认物理状态、所有物理 handle、物理 CAN 任务/故障注入状态及 VirtualBus handle，最近 trace 和当前 DBC/CDD 数据库摘要；`pcan_features` 状态字段作为兼容别名保留。
- `GET /metrics`：输出 Prometheus text format 的累计 TX/RX/error frame、物理接收队列丢帧原因及 trace 覆盖计数、物理/虚拟本地接收注入计数、虚拟 handle 数和 trace 帧数。
- `expect_frame`：只读等待指定物理 handle（默认 `default`）上的特定 ID 和可选数据掩码；超时明确报错，不发送报文。
- `load_database`：从 `uploads/` 沙箱加载 DBC 消息/信号，或解析 CANdela CDD 的 ECU、诊断服务、DID 和数据对象目录；加载响应只含摘要和少量预览。
- `list_diagnostic_services` / `get_did`：在当前 CDD 中按名称搜索并分页列出诊断服务，或按 DID 数值查询完整数据对象元数据。
- `decode_did` / `encode_did`：根据当前 CDD 中的 DID 定义，将原始 DID payload 解码为字段值，或将 qualifier 到值的映射编码为原始 payload；不含 UDS 服务/DID 字节，也不发送 CAN 帧。
- CDD 编解码支持整数、BCD、ASCII、文本表映射、线性转换、嵌套结构和字段列表；未支持的编码或不明确布局会明确报错。DBS 暂未实现。要加载本机文件，需先通过 `upload_*` 工具上传；`load_database` 不接受服务器任意本地路径。
- `decode_frame` / `encode_frame`：用当前加载的 DBC 将帧解码为物理信号值，或将信号值编码为帧数据。`encode_frame` 只生成数据，不发送。
- `send_signal` / `read_signal`：按 DBC 编码并发送或接收解码信号；VirtualBus 可直接调用，PCAN/Vector 物理发送必须传 `confirm_physical_io=true`。

`send_can_frame` 必须传入 `confirm_physical_io=true` 才会真实发送。成功发送的 TX 帧会带本机时间戳、通道和 `is_rx=false`，并在对应 handle 的 trace 已运行时写入 trace。`get_trace` 必须在至少启动过一次 trace 后调用；trace 停止后仍可查询缓存，再次启动会清空旧缓存。接收和 trace 查询结果同时提供 JSON 文本及结构化内容，以兼容不同 MCP 客户端。物理每个 handle 的接收队列和 trace 最多各保留 300,000 帧；VirtualBus 仍使用 1,000 帧接收队列和 10,000 帧 trace 缓存。接收队列满时丢弃最早的待取帧，并分别报告最旧帧/新帧丢弃数；批量接收工具可降低逐次 MCP 调用开销。驱动标记为 error frame 的帧会更新错误计数、最近错误帧和 trace，但不会进入普通接收队列或 UDS/转发帧流；服务以 DEBUG 级别记录驱动原始消息供诊断。CAN FD payload 长度限于 0–8、12、16、20、24、32、48 或 64 字节；发送 FD 帧前必须以 `fd=true` 连接。PCAN FD 使用 80 MHz 时钟和 80% sample point 自动计算 timing；数据段比特率未指定时取 `max(2 Mbps, 仲裁段比特率)`。

被动监控时可通过 `connect_can(..., listen_only=true)` 启用硬件静默模式。PCAN 使用 python-can 的 `BusState.PASSIVE`，Vector 使用其 `listen_only` 参数；服务层同时拒绝发送请求。静默模式的实际行为仍取决于硬件和驱动实现。

虚拟总线与物理 CAN 工具相互隔离：要让两个 VirtualBus handle 通信，创建时指定相同的 `channel`；不同 channel 互不收发。此能力仅适用于进程内模拟和软件集成测试，不代表真实硬件时序或电气特性。

虚拟故障注入配置应用在指定 handle 的接收路径，因此只影响该 handle 看见的流量，不改变发送端或其他接收 handle。`drop_rate` 和 `reorder_rate` 范围为 0–1，`max_delay_ms` 范围为 0–60000；设置 `seed` 可重现随机决策。延迟在 0 与上限之间随机分布；乱序由相邻帧交换顺序实现。延迟队列每个 handle 最多缓存 10,000 帧；队列溢出或关闭/断开注入时，尚未交付的帧计入丢弃计数并清理。

`start_frame_generator` 的 counter 模式递增 payload 指定字节，ramp 模式以大端格式递增 1–4 字节数值，random 模式按可选 `seed` 生成 payload，template 模式接受十六进制模板并用 `{counter}` 或 `{value}` 替换为单字节。帧生成器只用于 VirtualBus；启动首帧立即发送，任务数上限和周期范围与周期发送一致。

物理设备专用的周期发送、生成器、场景、UDS、DBC 信号发送和单帧转发工具要求显式确认实体 I/O；周期/生成器有 60 秒自动停止上限。物理 UDS 目前只允许 ReadDTCInformation (0x19) 与 ReadDataByIdentifier (0x22)。PCAN/Vector 接收故障注入只改变本地工具/trace 观察，不会注入真实总线错误；`forward_physical_frame` 是单次调用，不提供常驻网关或自动桥接。

`decode_error_frame` 只报告 python-can 实际暴露的错误帧标志、原始数据和驱动 bus state。指定 `handle` 时路由到同名物理或虚拟连接（若两种连接同名则物理优先）；`bus_handle` 保留为旧版物理选择参数。若适配器不提供 bus state，结果为 `null`；错误帧 payload 的厂商/控制器位定义不统一，因此不会做未经驱动确认的解释。Prometheus `/metrics` 自定义路由不继承 MCP 工具鉴权；部署时应将服务绑定到可信接口并通过网络策略限制访问。

物理 PCAN/Vector 总线支持多个并发 handle；实际并发数量受已枚举设备及可用物理通道限制。同一硬件通道不能被多个 handle 同时占用。虚拟总线也支持多个并发 handle，但不等同于真实硬件时序或电气特性。

UDS/ISO-TP 模拟依赖 `can-isotp`。诊断工具仅使用 VirtualBus，不连接物理 ECU。`uds_request` 的 request/response ID 必须显式提供；如需 CAN FD，虚拟 handle 创建时须指定 `fd=true`，请求调用时再指定 `is_fd=true`。P2/P2* 为毫秒级等待窗口；事务总时限由 `timeout` 限制。NRC 0x78 会继续等待 P2*；其他负响应会返回 NRC 数值及名称。`suppress_positive_response=true` 会设置 UDS 子功能位 0x80 并在发送后立即返回。`s3_ms` 暴露会话测试参数，但 VirtualBus MCP 不实现 ECU 会话状态机或自动维持会话。功能寻址目前只支持标准 CAN 上最多 7 字节的请求和多个 ISO-TP 单帧 ECU 响应。

不提供 SecurityAccess seed/key 算法、外部命令执行或 ECU 解锁辅助。

DBC 信号支持依赖 `cantools`。`load_database` 为兼容 Vector CANdb++ 可接受的 DBC 使用 `strict=False` 加载，返回的 `warnings` 会列出宽松校验和已检测到的问题；信号超出消息 payload 范围的报文会被拒绝解码/编码，避免产生不可靠结果。加载 DBC 后，`decode_frame` / `encode_frame` 可用于离线信号转换；`send_signal` / `read_signal` 可用于 VirtualBus、PCAN 和 Vector，物理发送需显式确认。仓库提供小型示例 DBC 用于测试数据库加载、信号转换和虚拟总线读写。CDD 以 CANdela XML 格式解析 ECU、服务、DID 和 DID 数据对象/编码元数据；加载响应为有界摘要，可通过 `list_diagnostic_services` 搜索/分页及 `get_did` 按 DID 数值获取细节。`decode_did` / `encode_did` 支持整数、BCD、ASCII、TEXTTBL 选择映射、LINCOMP 线性转换、按顺序排列的嵌套结构和字段列表；输入输出仅为 DID 原始数据，不含 UDS 服务/DID 前缀，也不执行总线收发。遇到未支持的编码类型、字节序或不明确布局时会报错。车辆专用 CDD、Seed&Key 二进制和大型 J1939 数据库不随公开源码分发；依赖这些外部样例的回归测试会在样例缺失时标记为跳过。DBS 解析尚未实现。Trace 负载为基于帧长度和估算协议开销的近似值，不建模精确 bit stuffing、仲裁及 CAN FD 双比特率。Trace 文件按本机给定路径写入；`.asc`、`.blf`、`.csv` 由 python-can writer 生成。

## 扩展方式

- CAN 后端按硬件接口拆分：`adapters/can/pcan.py` 中的 `PcanBusAdapter` 负责 PCAN，`adapters/can/vector.py` 中的 `VectorBusAdapter` 负责 Vector VN1640A 等 Vector XL 设备；两者使用各自的 python-can Bus 参数，并复用 `adapters/can/base.py` 中的公共报文收发逻辑。Agent 调用 `connect_can` 时提供 `interface` 和 `channel`：PCAN 使用通道名（如 `PCAN_USBBUS1`），Vector 使用从 0 开始的通道索引，可用 `serial` 绑定具体硬件，`app_name` 默认是 `CANalyzer`。VN1640A 需要安装与 Python 位数匹配的 Vector XL Driver Library，并在 Vector Hardware Configuration 中配置应用/通道（使用 `serial` 时可按 python-can 文档免预先配置应用）。先调用 `enumerate_vector_devices` 查询实际通道索引与序列号。同一 `handle` 再次连接会切换该连接；不同 handle 可同时占用不同物理通道。新增厂商接口时，为其创建独立适配类，并在 `adapters/can/factory.py` 注册。
- 报文筛选：在 `core/filters.py` 扩展 `FrameFilter`，trace 和查询复用相同规则。
- 数据库：实现 `DatabaseParser`，将解析器注册到 `DatabaseService`。数据库格式支持应以目标 CDD/DBS 版本及可用解析库为准。

## 开发测试

```powershell
python -m pip install -e ".[dev]"
python -m pytest
```
