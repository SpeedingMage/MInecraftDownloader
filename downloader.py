"""Vanilla Minecraft downloader and portable MultiMC instance exporter.

Uses only Python's standard library. Downloading never executes game code.
"""
from __future__ import annotations

import concurrent.futures
import contextlib
import dataclasses
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import platform
import re
import ssl
import threading
from typing import Callable
import urllib.request
import urllib.error
from urllib.parse import urlparse
import zipfile

MANIFEST_URL = "https://piston-meta.mojang.com/mc/game/version_manifest_v2.json"
ASSET_ROOT = "https://resources.download.minecraft.net"
LIBRARY_ROOT = "https://libraries.minecraft.net"
Event = Callable[[str, object], None]


def metadata_candidates(url: str) -> list[str]:
    """Use the legacy official hostname for identical content-addressed metadata."""
    parsed = urlparse(url)
    official_aliases = {
        "piston-meta.mojang.com": "launchermeta.mojang.com",
        "launchermeta.mojang.com": "piston-meta.mojang.com",
    }
    alias = official_aliases.get(parsed.netloc)
    if parsed.scheme == "https" and alias:
        return [url, parsed._replace(netloc=alias).geturl()]
    return [url]


def network_error_hint(error: Exception) -> str:
    """Explain common transport errors without weakening TLS verification."""
    reason = error.reason if isinstance(error, urllib.error.URLError) else error
    if isinstance(reason, ssl.SSLEOFError) or "UNEXPECTED_EOF_WHILE_READING" in str(reason):
        return ("TLS 连接在传输途中被关闭（SSL EOF）。请检查代理连接；"
                "可在界面选择自定义 HTTP 代理并填写实际地址后重试。")
    if isinstance(reason, ssl.SSLCertVerificationError):
        return "TLS 证书验证失败，请检查系统时间和代理证书配置。"
    return str(error)


class Cancelled(Exception):
    """The user cancelled the current operation."""


def check_cancel(cancel: threading.Event) -> None:
    if cancel.is_set():
        raise Cancelled("已取消；已校验的文件会保留，重试时可以复用。")


def safe_relative(value: str) -> str:
    """Accept portable, unambiguous relative paths only, including on Windows."""
    if not isinstance(value, str) or not value or "\\" in value or ":" in value:
        raise ValueError(f"不安全的相对路径：{value!r}")
    parts = value.split("/")
    if any(p in ("", ".", "..") or p.endswith((" ", ".")) for p in parts):
        raise ValueError(f"不安全的相对路径：{value!r}")
    for part in parts:
        if any(ord(c) < 32 or c in '<>\"|?*' for c in part):
            raise ValueError(f"不安全的文件名：{part!r}")
        if part.split(".")[0].upper() in {
            "CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)),
            *(f"LPT{i}" for i in range(1, 10)),
        }:
            raise ValueError(f"保留的设备名：{part!r}")
    return str(PurePosixPath(value))


def destination(root: Path, relative: str) -> Path:
    root = root.resolve()
    path = root.joinpath(*safe_relative(relative).split("/"))
    resolved = path.resolve()
    if not resolved.is_relative_to(root):
        raise ValueError(f"目标路径越界：{relative}（{resolved!s} 不在 {root!s} 中）")
    return path


def digest_file(path: Path, cancel: threading.Event | None = None) -> str:
    sha = hashlib.sha1()
    with path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            if cancel is not None:
                check_cancel(cancel)
            sha.update(block)
    return sha.hexdigest()


def valid_hash(value: str | None) -> str | None:
    if value is not None and not re.fullmatch(r"[0-9a-fA-F]{40}", value):
        raise ValueError("清单包含无效的 SHA-1")
    return value.lower() if value else None


@dataclasses.dataclass(frozen=True)
class Download:
    relative: str
    url: str
    sha1: str | None = None
    size: int | None = None

    def __post_init__(self):
        safe_relative(self.relative)
        valid_hash(self.sha1)
        if self.size is not None and self.size < 0:
            raise ValueError("清单包含负数文件大小")
        if urlparse(self.url).scheme not in ("http", "https"):
            raise ValueError("只接受 HTTP/HTTPS 下载地址")


@dataclasses.dataclass
class Plan:
    version: str
    manifest: dict
    files: list[Download]
    generated: dict[str, bytes]
    aliases: dict[str, str]

    @property
    def total_bytes(self) -> int:
        return sum(file.size or 0 for file in self.files)


class Network:
    """Each worker gets its own opener, honoring system or explicit HTTP proxy."""

    def __init__(self, proxy: str = "system", timeout: float = 30,
                 log: Callable[[str], None] | None = None):
        if proxy not in ("system", "direct"):
            parsed = urlparse(proxy)
            if parsed.scheme not in ("http", "https") or not parsed.hostname:
                raise ValueError("自定义代理格式应为 http://127.0.0.1:7897")
        self.proxy = proxy
        self.timeout = timeout
        self.local = threading.local()
        self.log = log or (lambda _: None)

    def open(self, url: str):
        if not hasattr(self.local, "opener"):
            handlers = []
            if self.proxy == "direct":
                handlers.append(urllib.request.ProxyHandler({}))
            elif self.proxy != "system":
                handlers.append(urllib.request.ProxyHandler({"http": self.proxy, "https": self.proxy}))
            self.local.opener = urllib.request.build_opener(*handlers)
        request = urllib.request.Request(url, headers={"User-Agent": "VanillaPackDownloader/1.1"})
        try:
            return self.local.opener.open(request, timeout=self.timeout)
        except urllib.error.HTTPError as error:
            error.close()
            raise

    def json(self, url: str, cancel: threading.Event, sha1: str | None = None) -> tuple[dict, bytes]:
        last_error = None
        candidates = metadata_candidates(url)
        attempts = max(3, len(candidates) * 2)
        for attempt in range(attempts):
            check_cancel(cancel)
            candidate = candidates[attempt % len(candidates)]
            try:
                with self.open(candidate) as response:
                    blocks = []
                    length = 0
                    while chunk := response.read(65536):
                        check_cancel(cancel)
                        length += len(chunk)
                        if length > 64 * 1024 * 1024:
                            raise ValueError("JSON 清单超过 64 MiB")
                        blocks.append(chunk)
                raw = b"".join(blocks)
                if sha1 and hashlib.sha1(raw).hexdigest() != valid_hash(sha1):
                    raise ValueError("JSON 清单 SHA-1 校验失败")
                result = json.loads(raw)
                if not isinstance(result, dict):
                    raise ValueError("清单根节点必须是对象")
                return result, raw
            except Cancelled:
                raise
            except Exception as error:
                last_error = error
                self.log(f"读取清单失败（{attempt + 1}/{attempts}，{urlparse(candidate).hostname}）：{network_error_hint(error)}")
                # A retry constructs a fresh opener and rereads system proxy settings.
                if hasattr(self.local, "opener"):
                    del self.local.opener
                if attempt < attempts - 1:
                    next_url = candidates[(attempt + 1) % len(candidates)]
                    if next_url != candidate:
                        self.log(f"将切换官方元数据域名：{urlparse(next_url).hostname}")
                    if cancel.wait(min(attempt + 1, 3)):
                        check_cancel(cancel)
        raise RuntimeError(f"获取清单失败：{url}\n{network_error_hint(last_error)}") from last_error


def list_versions(network: Network, cancel: threading.Event) -> list[dict]:
    manifest, _ = network.json(MANIFEST_URL, cancel)
    return manifest["versions"]


def environment() -> tuple[str, str, str]:
    name = {"Windows": "windows", "Darwin": "osx", "Linux": "linux"}.get(platform.system())
    if name is None:
        raise ValueError("暂不支持当前操作系统")
    arch = platform.machine().lower()
    arch = "amd64" if arch in ("x86_64", "amd64") else "aarch64" if arch in ("arm64", "aarch64") else arch
    # The first release targets the user's Windows x64 machine. Avoid incomplete
    # native downloads being silently advertised as a portable ARM installation.
    if name != "windows" or arch != "amd64":
        raise ValueError("此版本下载目标为 Windows x64，请在 Windows x64 上运行。")
    return name, arch, platform.version()


def rule_allows(rules: list[dict] | None, os_name: str, arch: str, os_version: str) -> bool:
    if not rules:
        return True
    allowed = False
    for rule in rules:
        restriction = rule.get("os", {})
        matches = (
            ("name" not in restriction or restriction["name"] == os_name)
            and ("arch" not in restriction or re.search(restriction["arch"], arch) is not None)
            and ("version" not in restriction or re.search(restriction["version"], os_version) is not None)
            and all(value is False for value in rule.get("features", {}).values())
        )
        if matches:
            allowed = rule["action"] == "allow"
    return allowed


def artifact_path(name: str) -> str:
    name, separator, extension = name.partition("@")
    parts = name.split(":")
    if len(parts) not in (3, 4):
        raise ValueError(f"无效的 Maven 坐标：{name}")
    group, artifact, version = parts[:3]
    classifier = "-" + parts[3] if len(parts) == 4 else ""
    return safe_relative(f"{group.replace('.', '/')}/{artifact}/{version}/{artifact}-{version}{classifier}.{extension if separator else 'jar'}")


def make_plan(entry: dict, network: Network, cancel: threading.Event,
              target: tuple[str, str, str] | None = None) -> Plan:
    version = entry["id"]
    if not re.fullmatch(r"[A-Za-z0-9_+.-]+", version) or version in (".", ".."):
        raise ValueError("版本 ID 不适合用作本地文件名")
    safe_relative(version)
    manifest, raw = network.json(entry["url"], cancel, entry.get("sha1"))
    if manifest.get("id") != version or "inheritsFrom" in manifest:
        raise ValueError("仅支持版本 ID 一致且不依赖父版本的官方原版清单")
    os_name, arch, os_version = target or environment()
    files: dict[str, Download] = {}

    def add(relative: str, info: dict, fallback: str | None = None):
        item = Download(relative, info.get("url") or fallback or "", info.get("sha1"), info.get("size"))
        previous = files.get(relative)
        if previous is not None and previous != item:
            raise ValueError(f"清单中的目标路径冲突：{relative}")
        files[relative] = item

    add(f"versions/{version}/{version}.jar", manifest["downloads"]["client"])
    generated = {f"versions/{version}/{version}.json": raw}
    for library in manifest.get("libraries", []):
        check_cancel(cancel)
        if not rule_allows(library.get("rules"), os_name, arch, os_version):
            continue
        downloads = library.get("downloads")
        if downloads is None:
            path = artifact_path(library["name"])
            add("libraries/" + path, {}, library.get("url", LIBRARY_ROOT).rstrip("/") + "/" + path)
        elif artifact := downloads.get("artifact"):
            path = artifact.get("path") or artifact_path(library["name"])
            add("libraries/" + path, artifact)
        native = library.get("natives", {}).get(os_name)
        if native:
            native = native.replace("${arch}", "64" if arch in ("amd64", "aarch64") else "32")
            info = (downloads or {}).get("classifiers", {}).get(native)
            if info is None:
                raise ValueError(f"原生库缺少下载信息：{library['name']} / {native}")
            add("libraries/" + info["path"], info)

    index_info = manifest.get("assetIndex")
    if not index_info:
        raise ValueError("该旧版本未提供资源索引，暂不支持完整下载。")
    index_id = safe_relative(index_info["id"])
    if "/" in index_id:
        raise ValueError("无效资源索引 ID")
    # Older mutable indexes can carry stale hashes; HMCL applies this same gate.
    index_hash = index_info.get("sha1")
    verify_hash = index_hash if index_hash and index_hash in index_info["url"] else None
    index, index_raw = network.json(index_info["url"], cancel, verify_hash)
    generated[f"assets/indexes/{index_id}.json"] = index_raw
    aliases = {}
    for logical, info in index["objects"].items():
        sha1 = valid_hash(info["hash"])
        if sha1 is None:
            raise ValueError("资源对象缺少 SHA-1")
        relative = f"assets/objects/{sha1[:2]}/{sha1}"
        add(relative, {"url": f"{ASSET_ROOT}/{sha1[:2]}/{sha1}", "sha1": sha1, "size": info["size"]})
        if index.get("virtual"):
            aliases[f"assets/virtual/{index_id}/{safe_relative(logical)}"] = relative
        if index.get("map_to_resources"):
            aliases[f"resources/{safe_relative(logical)}"] = relative
    logging_file = manifest.get("logging", {}).get("client", {}).get("file")
    if logging_file:
        add("assets/log_configs/" + safe_relative(logging_file["id"]), logging_file)
    return Plan(version, manifest, list(files.values()), generated, aliases)


def download_one(item: Download, root: Path, network: Network,
                 cancel: threading.Event) -> tuple[str, int, bool]:
    """Verify existing files; atomically replace incomplete or invalid downloads."""
    check_cancel(cancel)
    path = destination(root, item.relative)
    if path.is_file() and (item.size is None or path.stat().st_size == item.size):
        if item.sha1 and digest_file(path, cancel) == item.sha1.lower():
            return item.relative, path.stat().st_size, True
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = destination(root, item.relative + ".part")
    last_error = None
    for attempt in range(3):
        check_cancel(cancel)
        try:
            sha = hashlib.sha1()
            length = 0
            with network.open(item.url) as response, partial.open("wb") as stream:
                while chunk := response.read(65536):
                    check_cancel(cancel)
                    stream.write(chunk)
                    length += len(chunk)
                    sha.update(chunk)
            if item.size is not None and length != item.size:
                raise ValueError(f"文件大小不匹配：期望 {item.size}，实际 {length}")
            if item.sha1 and sha.hexdigest() != item.sha1.lower():
                raise ValueError("SHA-1 校验失败")
            check_cancel(cancel)
            os.replace(partial, path)
            return item.relative, length, False
        except Cancelled:
            raise
        except Exception as error:
            last_error = error
            if attempt < 2 and cancel.wait(attempt + 1):
                check_cancel(cancel)
        finally:
            partial.unlink(missing_ok=True)
    raise RuntimeError(f"下载失败：{item.relative}\n{network_error_hint(last_error)}") from last_error


@contextlib.contextmanager
def output_lock(path: Path):
    """OS lock releases automatically if the downloader process crashes."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as stream:
        stream.seek(0, 2)
        if stream.tell() == 0:
            stream.write(b"0")
            stream.flush()
        stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            raise RuntimeError("另一个下载器正在使用这个版本的输出目录。") from error
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == "nt":
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream, fcntl.LOCK_UN)


def atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".part")
    temporary.write_bytes(data)
    os.replace(temporary, path)


def package_instance(plan: Plan, root: Path, output: Path, cancel: threading.Event,
                     emit: Event) -> Path:
    """Export only the download inventory, never unrelated files or accounts."""
    archive = destination(output, f"Minecraft-{plan.version}-Windows-x64.zip")
    partial = destination(output, archive.name + ".part")
    cfg = f"[General]\nInstanceType=OneSix\nname=Minecraft {plan.version}\niconKey=grass\nOverrideJavaLocation=false\nOverrideMemory=false\n"
    pack = {"formatVersion": 1, "components": [{
        "uid": "net.minecraft", "version": plan.version, "important": True,
        "cachedName": "Minecraft", "cachedVersion": plan.version,
    }]}
    version_java = plan.manifest.get("javaVersion", {}).get("majorVersion", "启动器自动选择")
    instructions = (
        f"Minecraft {plan.version} / Windows x64\n\n"
        "方式一：在 HMCL、Prism Launcher 或 MultiMC 的导入实例/整合包界面选择本 ZIP。\n"
        "不同启动器使用各自的全局缓存，导入时可能再次下载元数据、客户端、依赖或资源，不能保证离线导入。\n"
        "方式二：解压后，把 .minecraft 作为已有游戏目录添加到支持传统目录布局的启动器。\n"
        "若选择对话框隐藏 .minecraft，请开启显示隐藏文件。新版 HMCL 可能还会迁移目录布局。\n"
        f"游戏所需 Java 主版本：{version_java}。本包不包含 Java 运行时和启动器。\n"
        "本包不包含账号、访问令牌、存档或个人设置。正常登录和游戏授权由启动器处理。\n"
        "download-manifest.json 记录本包每个游戏文件的大小和 SHA-1。\n"
    )
    names = sorted({item.relative for item in plan.files} | plan.generated.keys() | plan.aliases.keys())
    inventory = []
    try:
        with zipfile.ZipFile(partial, "w", compression=zipfile.ZIP_DEFLATED,
                             compresslevel=1, allowZip64=True) as zipped:
            zipped.writestr("instance.cfg", cfg)
            zipped.writestr("mmc-pack.json", json.dumps(pack, indent=2))
            zipped.writestr("IMPORT-README.txt", instructions)
            for index, relative in enumerate(names, 1):
                check_cancel(cancel)
                source = destination(root, plan.aliases.get(relative, relative))
                sha = hashlib.sha1()
                size = 0
                with source.open("rb") as src, zipped.open(".minecraft/" + relative, "w", force_zip64=True) as dst:
                    while chunk := src.read(1024 * 1024):
                        check_cancel(cancel)
                        sha.update(chunk)
                        size += len(chunk)
                        dst.write(chunk)
                inventory.append({"path": ".minecraft/" + relative, "size": size, "sha1": sha.hexdigest()})
                if index == len(names) or index % 25 == 0:
                    emit("packing", (index, len(names)))
            zipped.writestr("download-manifest.json", json.dumps({
                "formatVersion": 1, "minecraft": plan.version, "target": "windows-x64",
                "javaMajorVersion": plan.manifest.get("javaVersion", {}).get("majorVersion"),
                "files": inventory,
            }, ensure_ascii=False, indent=2))
        check_cancel(cancel)
        os.replace(partial, archive)
        return archive
    finally:
        partial.unlink(missing_ok=True)


def install(plan: Plan, output: Path, network: Network, cancel: threading.Event,
            emit: Event = lambda *_: None, workers: int = 8, pack: bool = True) -> tuple[Path, Path | None]:
    output = output.resolve()
    root = destination(output, f"Minecraft-{plan.version}/.minecraft")
    output.mkdir(parents=True, exist_ok=True)
    if not 1 <= workers <= 16:
        raise ValueError("并发数必须在 1 到 16 之间")
    with output_lock(destination(output, f"Minecraft-{plan.version}/download.lock")):
        root.mkdir(parents=True, exist_ok=True)
        for relative, data in plan.generated.items():
            check_cancel(cancel)
            atomic_write(destination(root, relative), data)
        emit("total", (len(plan.files), plan.total_bytes))
        completed = reused = transferred = 0
        # Keep only a small bounded window of pending work, even for huge indexes.
        iterator = iter(plan.files)
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
            pending = {}

            def submit_next():
                item = next(iterator, None)
                if item is not None:
                    pending[pool.submit(download_one, item, root, network, cancel)] = item

            for _ in range(workers * 2):
                submit_next()
            try:
                while pending:
                    check_cancel(cancel)
                    ready, _ = concurrent.futures.wait(pending, timeout=0.2,
                                                      return_when=concurrent.futures.FIRST_COMPLETED)
                    for future in ready:
                        pending.pop(future)
                        relative, size, cached = future.result()
                        completed += 1
                        reused += cached
                        transferred += 0 if cached else size
                        emit("progress", (completed, len(plan.files), relative, reused, transferred))
                        submit_next()
            except BaseException:
                cancel.set()
                for future in pending:
                    future.cancel()
                raise
        check_cancel(cancel)
        # Legacy resource aliases are physical copies in the folder as well as ZIP.
        for relative, original in plan.aliases.items():
            check_cancel(cancel)
            target = destination(root, relative)
            target.parent.mkdir(parents=True, exist_ok=True)
            source = destination(root, original)
            temporary = destination(root, relative + ".part")
            try:
                with source.open("rb") as src, temporary.open("wb") as dst:
                    while chunk := src.read(1024 * 1024):
                        check_cancel(cancel)
                        dst.write(chunk)
                os.replace(temporary, target)
            finally:
                temporary.unlink(missing_ok=True)
        emit("log", f"下载完成：{completed} 个文件，其中 {reused} 个复用已校验文件。")
        archive = package_instance(plan, root, output, cancel, emit) if pack else None
        return root, archive
