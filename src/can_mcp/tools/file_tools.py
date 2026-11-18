from can_mcp.services.file_transfer_service import FileTransferService


class FileTools:
    def __init__(self, files: FileTransferService) -> None:
        self._files = files

    def upload_begin(
        self,
        name: str,
        size: int,
        sha256: str | None = None,
        upload_id: str | None = None,
    ) -> dict[str, object]:
        """开始或续传文件到 uploads/ 沙箱，单文件上限 64 MB。

        文件只能通过 upload_begin/upload_chunk/upload_finish/download_file/list_files
        这五个工具进出；服务端物理路径对客户端不可见。仅允许 .dbc、.cdd、.dbs、
        .asc、.blf 和 .csv。续传时提供原 upload_id，返回 offset 后从该位置继续。

        Args:
            name: 仅文件名，不接受目录或路径。
            size: 文件总字节数，最大 64 MB。
            sha256: 可选的完整文件 SHA-256 十六进制摘要，upload_finish 时验证。
            upload_id: 可选已有 ID，用于续传同一上传。
        """
        return self._files.upload_begin(name, size, sha256, upload_id)

    def upload_chunk(
        self,
        upload_id: str,
        offset: int,
        data: str,
    ) -> dict[str, object]:
        """按 offset 顺序追加一个 base64 文件块，单块最多 512 KB。

        文件只能通过 upload_begin/upload_chunk/upload_finish/download_file/list_files
        这五个工具进出；服务端物理路径对客户端不可见。offset 必须等于服务端已收字节数。

        Args:
            upload_id: upload_begin 返回的 ID。
            offset: 当前块在文件中的字节偏移。
            data: base64 编码的块内容。
        """
        return self._files.upload_chunk(upload_id, offset, data)

    def upload_finish(self, upload_id: str) -> dict[str, object]:
        """校验并完成上传，返回可供 load_database 使用的 uploads/ 相对标识。

        文件只能通过 upload_begin/upload_chunk/upload_finish/download_file/list_files
        这五个工具进出；服务端物理路径对客户端不可见。若 SHA-256 不匹配，临时文件会删除。

        Args:
            upload_id: upload_begin 返回的 ID。
        """
        return self._files.upload_finish(upload_id)

    def download_file(
        self,
        path: str,
        offset: int = 0,
        chunk_size: int = FileTransferService.MAX_CHUNK_SIZE,
    ) -> dict[str, object]:
        """从 uploads/ 沙箱分块读取文件；仅接受 list_files/upload_finish 返回的标识。

        文件只能通过 upload_begin/upload_chunk/upload_finish/download_file/list_files
        这五个工具进出；服务端物理路径对客户端不可见。返回 data 为 base64。

        Args:
            path: uploads/ 相对文件标识，不接受绝对路径或路径穿越。
            offset: 起始字节偏移。
            chunk_size: 读取字节数，范围 1–512 KB。
        """
        return self._files.download_file(path, offset, chunk_size)

    def list_files(self) -> list[dict[str, object]]:
        """列出 uploads/ 沙箱内未过期的文件及大小、UTC 修改时间。

        文件只能通过 upload_begin/upload_chunk/upload_finish/download_file/list_files
        这五个工具进出；服务端物理路径对客户端不可见。上传文件在 24 小时后过期清理。
        """
        return self._files.list_files()
