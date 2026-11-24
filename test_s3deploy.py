import hashlib, tempfile, unittest
from pathlib import Path
import s3deploy as d


class Plan(unittest.TestCase):
    def test_classifies_new_changed_same(self):
        local = {"index.html": "aaa", "styles.css": "bbb", "images/a.webp": "ccc"}
        live = {"index.html": "aaa", "styles.css": "OLD"}
        p = d.plan(local, live)
        self.assertEqual(p, {"new": ["images/a.webp"], "changed": ["styles.css"], "same": ["index.html"]})

    def test_prefix_is_applied_to_keys(self):
        p = d.plan({"index.html": "aaa"}, {"acme/index.html": "aaa"}, prefix="acme")
        self.assertEqual(p["same"], ["index.html"])

    def test_force_reuploads_identical_files(self):
        p = d.plan({"index.html": "aaa"}, {"index.html": "aaa"}, force=True)
        self.assertEqual(p["changed"], ["index.html"])


class Metadata(unittest.TestCase):
    def test_html_carries_charset(self):
        self.assertEqual(d.content_type("a/b.html"), "text/html; charset=utf-8")

    def test_unknown_extension_is_octet_stream(self):
        self.assertEqual(d.content_type("file.bin"), "application/octet-stream")

    def test_pages_short_lived_assets_immutable(self):
        self.assertIn("max-age=300", d.cache_control("index.html"))
        self.assertIn("immutable", d.cache_control("styles.css"))
        self.assertIn("immutable", d.cache_control("images/x.WEBP"))


class Invalidation(unittest.TestCase):
    def test_trailing_wildcard_ok(self):
        self.assertEqual(d.invalidation_paths(("/*", "/images/*")), ["/*", "/images/*"])

    def test_mid_path_wildcard_refused(self):
        with self.assertRaises(ValueError):
            d.invalidation_paths(("/*.html",))


class Verify(unittest.TestCase):
    def test_origin_reports_only_mismatches(self):
        local = {"a": "1", "b": "2"}
        self.assertEqual(d.verify_origin(local, {"a": "1", "b": "X"}), ["b"])
        self.assertEqual(d.verify_origin(local, {"a": "1"}), ["b"])

    def test_edge_compares_bytes_and_separates_network_failures(self):
        body = b"<html>new</html>"
        local = {"index.html": hashlib.md5(body).hexdigest(), "styles.css": hashlib.md5(b"css").hexdigest()}
        def fetch(url):
            if "index.html" in url: return body
            if "styles.css" in url: return b"old css"
            return None
        bad = d.verify_edge(local, fetch, ["https://edge.example"])
        self.assertEqual(bad, [("https://edge.example", "styles.css", "differs")])
        bad = d.verify_edge(local, lambda u: None, ["https://edge.example"])
        self.assertEqual({b[2] for b in bad}, {"network"})

    def test_edge_skips_sample_files_the_build_lacks(self):
        self.assertEqual(d.verify_edge({"other.html": "x"}, lambda u: b"", ["https://e"]), [])


class Manifest(unittest.TestCase):
    def test_manifest_hashes_every_file_with_posix_paths(self):
        with tempfile.TemporaryDirectory() as t:
            (Path(t) / "sub").mkdir(); (Path(t) / "sub" / "a.txt").write_bytes(b"hello"); (Path(t) / "b.txt").write_bytes(b"")
            m = d.local_manifest(Path(t))
            self.assertEqual(m, {"b.txt": hashlib.md5(b"").hexdigest(), "sub/a.txt": hashlib.md5(b"hello").hexdigest()})


if __name__ == "__main__":
    unittest.main()
