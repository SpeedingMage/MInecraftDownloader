"""Deterministic local-server integration tests; no game accounts or remote APIs."""
from concurrent.futures import ThreadPoolExecutor
import copy
import dataclasses
import hashlib
import io
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import sys
import ssl
import tempfile
import threading
import unittest
from unittest.mock import patch
import zipfile
import urllib.error
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import downloader as d


class DownloaderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.payloads = {}
        cls.requests = []

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                cls.requests.append(self.path)
                if self.path not in cls.payloads:
                    self.send_error(404)
                    return
                body = cls.payloads[self.path]
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_):
                pass

        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.output = Path(self.tmp.name)
        self.cancel = threading.Event()
        self.network = d.Network("direct", timeout=2)
        self.payloads.clear()
        self.requests.clear()

    def tearDown(self):
        self.tmp.cleanup()

    def file_info(self, path, body):
        self.payloads[path] = body
        return {"url": self.base + path, "size": len(body), "sha1": hashlib.sha1(body).hexdigest()}

    def fixture(self):
        client = self.file_info("/client.jar", b"test minecraft client")
        lib = self.file_info("/library.jar", b"test ordinary library")
        lib["path"] = "org/example/lib/1/lib-1.jar"
        native = self.file_info("/native.jar", b"test native library")
        native["path"] = "org/example/lib/1/lib-1-natives-windows.jar"
        asset = b"test sound asset"
        sha = hashlib.sha1(asset).hexdigest()
        self.payloads[f"/{sha[:2]}/{sha}"] = asset
        index = {"virtual": True, "map_to_resources": True,
                 "objects": {"sound/test.ogg": {"hash": sha, "size": len(asset)},
                             "sound/duplicate.ogg": {"hash": sha, "size": len(asset)}}}
        index_raw = json.dumps(index).encode()
        index_sha = hashlib.sha1(index_raw).hexdigest()
        index_info = self.file_info(f"/{index_sha}/index.json", index_raw)
        index_info["id"] = "test-assets"
        logging = self.file_info("/logging.xml", b"test logging config")
        logging["id"] = "client.xml"
        manifest = {
            "id": "test-1", "downloads": {"client": client},
            "mainClass": "net.minecraft.client.main.Main",
            "javaVersion": {"majorVersion": 21}, "assetIndex": index_info,
            "libraries": [
                {"name": "org.example:lib:1", "downloads": {"artifact": lib, "classifiers": {"natives-windows": native}},
                 "natives": {"windows": "natives-windows"}},
                {"name": "mac:excluded:1", "rules": [{"action": "allow", "os": {"name": "osx"}}],
                 "downloads": {"artifact": {"path": "excluded.jar", "url": self.base + "/never"}}},
            ],
            "logging": {"client": {"file": logging}},
        }
        raw = json.dumps(manifest).encode()
        self.payloads["/version.json"] = raw
        entry = {"id": "test-1", "url": self.base + "/version.json", "sha1": hashlib.sha1(raw).hexdigest(), "type": "release"}
        self.payloads["/versions.json"] = json.dumps({"versions": [entry]}).encode()
        with patch.object(d, "ASSET_ROOT", self.base):
            plan = d.make_plan(entry, self.network, self.cancel, ("windows", "amd64", "10.0"))
        return plan, entry

    def test_download_export_and_reuse(self):
        plan, entry = self.fixture()
        self.assertEqual(len(plan.files), 5)  # Duplicate resources are deduplicated.
        with patch.object(d, "MANIFEST_URL", self.base + "/versions.json"):
            self.assertEqual(d.list_versions(self.network, self.cancel)[0]["id"], entry["id"])
        root, archive = d.install(plan, self.output, self.network, self.cancel, workers=3)
        self.assertTrue((root / "resources/sound/test.ogg").is_file())
        self.assertTrue((root / "assets/virtual/test-assets/sound/test.ogg").is_file())
        # Unrelated files must never leak into the exported instance.
        (root / "accounts.json").write_text("PRIVATE", encoding="utf-8")
        requested = len(self.requests)
        events = []
        d.install(plan, self.output, self.network, self.cancel, lambda *event: events.append(event), workers=3)
        self.assertEqual(len(self.requests), requested)
        self.assertTrue(any(kind == "packing" for kind, _ in events))
        with zipfile.ZipFile(archive) as zipped:
            names = zipped.namelist()
            self.assertIn("instance.cfg", names)
            self.assertIn(".minecraft/versions/test-1/test-1.jar", names)
            self.assertNotIn(".minecraft/accounts.json", names)
            pack = json.loads(zipped.read("mmc-pack.json"))
            self.assertEqual(pack["components"][0]["uid"], "net.minecraft")
            self.assertEqual(pack["components"][0]["version"], "test-1")
            inventory = json.loads(zipped.read("download-manifest.json"))
            for item in inventory["files"]:
                body = zipped.read(item["path"])
                self.assertEqual(hashlib.sha1(body).hexdigest(), item["sha1"])
                self.assertEqual(len(body), item["size"])
            self.assertIsNone(zipped.testzip())

    def test_corruption_is_repaired(self):
        plan, _ = self.fixture()
        root, _ = d.install(plan, self.output, self.network, self.cancel, pack=False)
        file = root / plan.files[0].relative
        file.write_bytes(b"x" * file.stat().st_size)
        count = len(self.requests)
        d.install(plan, self.output, self.network, self.cancel, pack=False)
        self.assertEqual(len(self.requests), count + 1)
        self.assertEqual(d.digest_file(file), plan.files[0].sha1)

    def test_incremental_game_directory_repairs_only_missing_and_bad_files(self):
        plan, _ = self.fixture()
        root = self.output / ".minecraft"
        root.mkdir()
        for item in plan.files:
            path = d.destination(root, item.relative)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(self.payloads[urlparse(item.url).path])
        # Seed a real-looking existing directory, including a reformatted manifest.
        version_path = root / "versions/test-1/test-1.json"
        original_manifest = json.dumps(plan.manifest, indent=4).encode()
        version_path.write_bytes(original_manifest)
        protected = {"saves/world/level.dat": b"world", "mods/custom.jar": b"mod",
                     "accounts.json": b"private", "options.txt": b"settings",
                     "versions/other/other.json": b"other version"}
        for relative, content in protected.items():
            path = d.destination(root, relative)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        missing, corrupt = plan.files[:2]
        (root / missing.relative).unlink()
        (root / corrupt.relative).write_bytes(b"X" * corrupt.size)
        count = len(self.requests)
        events = []
        actual, archive = d.install(plan, root, self.network, self.cancel,
                                    lambda *event: events.append(event), workers=2)
        self.assertEqual(actual, root)
        self.assertEqual(len(self.requests) - count, 2)
        self.assertEqual(archive.parent, root / "downloader-exports")
        self.assertFalse((root / "Minecraft-test-1").exists())
        self.assertEqual(version_path.read_bytes(), original_manifest)
        progress = [value for kind, value in events if kind == "progress"]
        self.assertEqual(progress[-1][3], len(plan.files) - 2)
        for relative, content in protected.items():
            self.assertEqual((root / relative).read_bytes(), content)
        with zipfile.ZipFile(archive) as zipped:
            for relative in protected:
                self.assertNotIn(".minecraft/" + relative, zipped.namelist())

    def test_two_versions_share_verified_libraries_and_assets(self):
        plan, _ = self.fixture()
        root = self.output / ".minecraft"
        d.install(plan, root, self.network, self.cancel, pack=False)
        manifest = copy.deepcopy(plan.manifest)
        manifest["id"] = "test-2"
        files = [dataclasses.replace(item, relative=item.relative.replace("versions/test-1/test-1.jar", "versions/test-2/test-2.jar")) for item in plan.files]
        generated = {key: value for key, value in plan.generated.items() if not key.startswith("versions/")}
        generated["versions/test-2/test-2.json"] = json.dumps(manifest).encode()
        other = d.Plan("test-2", manifest, files, generated, plan.aliases)
        count = len(self.requests)
        d.install(other, root, self.network, self.cancel, pack=False)
        self.assertEqual(len(self.requests) - count, 1)
        self.assertTrue((root / "versions/test-1/test-1.json").exists())
        self.assertTrue((root / "versions/test-2/test-2.json").exists())
        self.assertEqual(d.install_layout(root, "test-1").lock_file,
                         d.install_layout(root, "test-2").lock_file)

    def test_modified_manifest_is_preserved_before_download(self):
        plan, _ = self.fixture()
        root = self.output / ".minecraft"
        version = root / "versions/test-1/test-1.json"
        version.parent.mkdir(parents=True)
        raw = b'{"id":"test-1","mainClass":"custom.ModLoader"}'
        version.write_bytes(raw)
        count = len(self.requests)
        with self.assertRaisesRegex(ValueError, "自定义"):
            d.install(plan, root, self.network, self.cancel)
        self.assertEqual(len(self.requests), count)
        self.assertEqual(version.read_bytes(), raw)
        self.assertFalse((root / "versions/test-1/test-1.jar").exists())

    def test_layout_keeps_ordinary_output_and_ignores_directory_name_case(self):
        direct = d.install_layout(self.output / ".MineCraft", "test-1")
        self.assertTrue(direct.incremental)
        self.assertEqual(direct.game_root, self.output / ".MineCraft")
        ordinary = d.install_layout(self.output / "Games", "test-1")
        self.assertFalse(ordinary.incremental)
        self.assertEqual(ordinary.game_root, self.output / "Games/Minecraft-test-1/.minecraft")
        self.assertEqual(ordinary.archive_dir, self.output / "Games")

    def test_bad_hash_never_replaces_existing_file(self):
        root = self.output
        (root / "target.jar").write_bytes(b"previous valid content")
        info = self.file_info("/bad", b"wrong content")
        item = d.Download("target.jar", info["url"], "0" * 40, info["size"])
        with self.assertRaisesRegex(RuntimeError, "SHA-1"):
            d.download_one(item, root, self.network, self.cancel)
        self.assertEqual(self.requests.count("/bad"), 3)
        self.assertEqual((root / "target.jar").read_bytes(), b"previous valid content")
        self.assertFalse((root / "target.jar.part").exists())

    def test_wrong_size_fails(self):
        info = self.file_info("/size", b"abc")
        with self.assertRaisesRegex(RuntimeError, "大小"):
            d.download_one(d.Download("size.bin", info["url"], info["sha1"], 100),
                           self.output, self.network, self.cancel)
        self.assertFalse((self.output / "size.bin").exists())

    def test_cancel_mid_download_does_not_publish(self):
        body = b"x" * 200000
        info = self.file_info("/large", body)

        class CancellingNetwork:
            def open(inner, url):
                response = self.network.open(url)
                original = response.read

                def read(size):
                    chunk = original(size)
                    self.cancel.set()
                    return chunk

                response.read = read
                return response

        with self.assertRaises(d.Cancelled):
            d.download_one(d.Download("cancel.bin", info["url"], info["sha1"], len(body)),
                           self.output, CancellingNetwork(), self.cancel)
        self.assertFalse((self.output / "cancel.bin").exists())
        self.assertFalse((self.output / "cancel.bin.part").exists())

    def test_cancel_pack_preserves_previous_archive(self):
        plan, _ = self.fixture()
        root, archive = d.install(plan, self.output, self.network, self.cancel)
        previous = archive.read_bytes()
        self.cancel.set()
        with self.assertRaises(d.Cancelled):
            d.package_instance(plan, root, self.output, self.cancel, lambda *_: None)
        self.assertEqual(archive.read_bytes(), previous)
        self.assertFalse(archive.with_name(archive.name + ".part").exists())

    def test_unsafe_paths_rejected(self):
        for value in ("../escape", "/absolute", "C:/escape", "a\\b", "a/../b", "a//b",
                      "x/CON.txt", "a/evil.", "x\x00y", "a/evil:stream"):
            with self.subTest(path=value), self.assertRaises(ValueError):
                d.destination(self.output, value)

    def test_rules_and_maven_paths(self):
        target = ("windows", "amd64", "10.0")
        self.assertTrue(d.rule_allows(None, *target))
        self.assertTrue(d.rule_allows([{"action": "allow", "os": {"name": "windows"}}], *target))
        self.assertFalse(d.rule_allows([{"action": "allow", "os": {"name": "osx"}}], *target))
        self.assertFalse(d.rule_allows([{"action": "allow"}, {"action": "disallow", "os": {"arch": "amd64"}}], *target))
        self.assertFalse(d.rule_allows([{"action": "allow", "features": {"is_demo_user": True}}], *target))
        self.assertEqual(d.artifact_path("org.example:test:1:natives-windows"), "org/example/test/1/test-1-natives-windows.jar")

    def test_metadata_hash_is_verified(self):
        _, entry = self.fixture()
        entry["sha1"] = "0" * 40
        with self.assertRaisesRegex(RuntimeError, "SHA-1"):
            d.make_plan(entry, self.network, self.cancel, ("windows", "amd64", "10.0"))

    def test_failed_install_does_not_export(self):
        plan, _ = self.fixture()
        plan.files[0] = d.Download(plan.files[0].relative, self.base + "/missing", "0" * 40)
        with self.assertRaises(RuntimeError):
            d.install(plan, self.output, self.network, self.cancel, workers=2)
        self.assertFalse(list(self.output.glob("*.zip")))
        self.assertFalse((self.output / "Minecraft-test-1/.minecraft/versions/test-1/test-1.json").exists())

    def test_output_lock_excludes_other_worker(self):
        path = self.output / "download.lock"
        with d.output_lock(path):
            def acquire():
                with d.output_lock(path):
                    pass
            with ThreadPoolExecutor(max_workers=1) as pool:
                with self.assertRaisesRegex(RuntimeError, "另一个下载器"):
                    pool.submit(acquire).result()
        with d.output_lock(path):
            pass

    def test_official_metadata_fallback_after_ssl_eof(self):
        raw = b'{"objects": {}}'
        url = "https://piston-meta.mojang.com/v1/packages/test/34.json"
        alternate = "https://launchermeta.mojang.com/v1/packages/test/34.json"
        messages = []
        network = d.Network(log=messages.append)
        failed = urllib.error.URLError(ssl.SSLEOFError(8, "UNEXPECTED_EOF_WHILE_READING"))
        with patch.object(network, "open", side_effect=[failed, io.BytesIO(raw)]) as request:
            document, result = network.json(url, self.cancel, hashlib.sha1(raw).hexdigest())
        self.assertEqual(document, {"objects": {}})
        self.assertEqual(result, raw)
        self.assertEqual([call.args[0] for call in request.call_args_list], [url, alternate])
        self.assertTrue(any("SSL EOF" in message for message in messages))
        self.assertTrue(any("切换官方" in message for message in messages))

    def test_fallback_still_checks_hash(self):
        raw = b'{"objects": {}}'
        url = "https://piston-meta.mojang.com/v1/packages/test/34.json"
        with patch.object(self.network, "open", side_effect=lambda _: io.BytesIO(raw)) as request:
            with self.assertRaisesRegex(RuntimeError, "SHA-1"):
                self.network.json(url, self.cancel, "0" * 40)
        self.assertEqual(request.call_count, 4)

    def test_cancel_during_metadata_backoff(self):
        url = "https://piston-meta.mojang.com/v1/packages/test/34.json"

        def fail(_):
            self.cancel.set()
            raise urllib.error.URLError(ssl.SSLEOFError(8, "UNEXPECTED_EOF_WHILE_READING"))

        with patch.object(self.network, "open", side_effect=fail) as request:
            with self.assertRaises(d.Cancelled):
                self.network.json(url, self.cancel)
        self.assertEqual(request.call_count, 1)

    def test_only_expected_official_hosts_have_aliases(self):
        for url in ("http://piston-meta.mojang.com/test", "https://example.com/test",
                    "https://piston-meta.mojang.com.evil.invalid/test"):
            self.assertEqual(d.metadata_candidates(url), [url])


if __name__ == "__main__":
    unittest.main()
