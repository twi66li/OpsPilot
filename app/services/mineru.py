"""MinerU 精准解析 API。入库线程负责租约和解析产物的持久化。"""

from pathlib import Path, PurePosixPath
from tempfile import TemporaryFile
from time import monotonic, sleep
from urllib.parse import quote, urlsplit
from zipfile import BadZipFile, ZipFile

from httpx import Client, HTTPError, InvalidURL, TimeoutException
from pypdf import PdfReader

from app.config import Settings

MAX_ZIP_SIZE = 128 * 1024 * 1024
MAX_CONTENT_LIST_SIZE = 10 * 1024 * 1024


def extract_content_list(path: Path, settings: Settings, check_lease) -> bytes:
    key = settings.mineru_api_key.get_secret_value().strip()
    if not key:
        raise ValueError("MinerU 在线解析需要配置 MINERU_API_KEY")
    if PdfReader(path).is_encrypted:
        raise ValueError("暂不支持加密PDF，请先解密")

    deadline = monotonic() + settings.mineru_timeout_seconds
    stage = "申请上传地址"

    def remaining() -> float:
        check_lease()
        seconds = deadline - monotonic()
        if seconds <= 0:
            raise ValueError("MinerU 在线解析超时，可稍后重试")
        return min(seconds, 30.0)

    def https_url(url: str) -> str:
        if urlsplit(url).scheme != "https" or not urlsplit(url).hostname:
            raise ValueError("MinerU 在线解析要求 HTTPS 地址")
        return url

    base = https_url(settings.mineru_api_base.rstrip("/"))
    headers = {"Authorization": f"Bearer {key}"}
    try:
        with Client() as client:
            def api(method, endpoint, **kwargs):
                response = client.request(method, base + endpoint, headers=headers, timeout=remaining(), **kwargs)
                response.raise_for_status()
                result = response.json()
                if result.get("code") != 0:
                    raise ValueError(f"MinerU {stage}失败，请检查密钥、额度或服务状态")
                return result["data"]

            # 使用固定文件名，避免将用户的原文件名一并发给解析服务。
            batch = api("POST", "/file-urls/batch", json={"files": [{"name": "document.pdf"}], "model_version": "vlm"})
            stage = "上传 PDF"
            with path.open("rb") as pdf:
                client.put(https_url(batch["file_urls"][0]), content=pdf, timeout=remaining()).raise_for_status()

            stage = "查询解析结果"
            endpoint = "/extract-results/batch/" + quote(batch["batch_id"], safe="")
            while True:
                results = api("GET", endpoint)["extract_result"]
                if results:
                    result = results[0]
                    if result["state"] == "failed":
                        raise ValueError("MinerU 解析 PDF 失败，可重试或手工上传 content_list.json")
                    if result["state"] == "done":
                        break
                sleep(min(2.0, remaining()))

            stage = "下载解析结果"
            with TemporaryFile() as archive, client.stream(
                "GET", https_url(result["full_zip_url"]), timeout=remaining()
            ) as response:
                response.raise_for_status()
                for chunk in response.iter_bytes(chunk_size=65536):
                    remaining()
                    if archive.tell() + len(chunk) > MAX_ZIP_SIZE:
                        raise ValueError("MinerU 解析压缩包超过 128 MB，请拆分 PDF")
                    archive.write(chunk)
                with ZipFile(archive) as result_zip:
                    for entry in result_zip.infolist():
                        name = PurePosixPath(entry.filename).name
                        if not entry.is_dir() and (name == "content_list.json" or name.endswith("_content_list.json")):
                            if entry.file_size > MAX_CONTENT_LIST_SIZE:
                                raise ValueError("MinerU content_list.json 超过 10 MB，请拆分 PDF")
                            return result_zip.read(entry)
                raise ValueError("MinerU 解析结果缺少兼容的 content_list.json")
    except TimeoutException:
        raise ValueError(f"MinerU {stage}超时，可稍后重试") from None
    except (HTTPError, InvalidURL, BadZipFile, KeyError, IndexError, TypeError, AttributeError):
        raise ValueError(f"MinerU {stage}失败，请检查网络、密钥和服务状态") from None
