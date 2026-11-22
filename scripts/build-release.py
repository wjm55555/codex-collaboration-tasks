"""Build a signed downloadable installer and its nested LAN publisher update."""
import argparse
from copy import copy
import json
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
from zipfile import ZIP_DEFLATED, ZipFile

root = Path(__file__).resolve().parents[1]
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--signing-key", required=True, type=Path)
args = parser.parse_args()

manifest = json.loads((root / "plugin/.codex-plugin/plugin.json").read_text())
version = manifest["version"]
files = [
    root / "setup.py", root / "使用说明.md", root / ".agents/plugins/marketplace.json",
    *sorted(root.glob("*.command")),
    *sorted(p for p in (root / "plugin").rglob("*") if p.is_file()
            and p.name != ".DS_Store" and "__pycache__" not in p.parts),
]
assert not any(p.name in ("connection.json", "host.json", "receiver.json")
               or p.suffix in (".pem", ".key", ".sqlite") for p in files)
out = root / "dist"
out.mkdir(exist_ok=True)
outer = out / "collaboration-tasks-macos.zip"

def sign(path, signature):
    subprocess.run(["/usr/bin/openssl", "dgst", "-sha256", "-sign", str(args.signing_key),
                    "-out", str(signature), str(path)], check=True)

with TemporaryDirectory(prefix="collaboration-signing-") as tmp:
    temp = Path(tmp)
    archive, signature = temp / (version + ".zip"), temp / (version + ".sig")
    with ZipFile(archive, "w", ZIP_DEFLATED) as inner:
        for path in files:
            inner.write(path, path.relative_to(root))
    sign(archive, signature)
    with ZipFile(outer, "w", ZIP_DEFLATED) as release, ZipFile(archive) as inner:
        for item in inner.infolist():
            entry = copy(item)
            entry.filename = "协作任务/" + entry.filename
            release.writestr(entry, inner.read(item))
        for path in (archive, signature):
            release.write(path, "协作任务/release/" + path.name)
    with ZipFile(outer) as release:
        assert release.testzip() is None
sign(outer, outer.with_suffix(".zip.sig"))
print(outer)
