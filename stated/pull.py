"""Bring an attached layer up to date from the address where its publisher lists its versions.

The publisher keeps a file, versions.json, beside the layer's files:

    {"layer": "base", "versions": [
        {"version": "2026.11.24", "file": "base-2026.11.24.sqlite.zst"},
        {"version": "2026.11.25", "follows": "2026.11.24", "file": "changes/base-2026.11.25.sqlite.gz", "sha256": "..."}]}

An entry with `follows` is a change file (layers.apply_changes): what is new since that version, gzipped, with
the hash of the file as served. An entry without is a whole layer, the way a new workspace starts. From the
version a workspace holds, the change files are followed one to the next and applied in turn.
"""

from dataclasses import dataclass
from hashlib import sha256
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin
from urllib.request import Request, urlopen
import zlib

from .layers import LayerAttachment

BASE = "https://data.statedlabs.com/base/"  # Where the base layer's versions are listed.
_AGENT = {"User-Agent": "Stated/0.0.1"}


@dataclass(frozen=True)
class LayerPull:
    layer: str
    was: str  # The version held before.
    version: str  # The version held now: the same when there was nothing newer.
    applied: list[LayerAttachment]  # One for each change file, in the order applied.


def pull(store, address: str = BASE, *, open_url=urlopen) -> LayerPull:
    if os.environ.get("STATED_FETCH", "").strip() == "0":
        raise ValueError("fetching from the web is off in this session: it reads only what the graph holds (STATED_FETCH=0)")
    if not isinstance(address, str) or not address.startswith(("http://", "https://")):
        raise ValueError("the address must be an http or https address")
    address = address.rstrip("/") + "/"
    try:
        with open_url(Request(address + "versions.json", headers=_AGENT), timeout=60) as response:
            listing = json.loads(response.read(5_000_000))
        name, entries = listing["layer"], listing["versions"]
        steps = {entry["follows"]: (entry["version"], entry["file"], entry["sha256"]) for entry in entries if "follows" in entry}
        listed = {entry["version"] for entry in entries}
    except (URLError, TimeoutError) as error:
        raise _unfetched(address + "versions.json", error) from None
    except (ValueError, KeyError, TypeError):
        raise ValueError(f"{address}versions.json is not a list of a layer's versions") from None
    held = next((layer for layer in store.layers() if layer.name == name and not layer.writable), None)
    if held is None:
        raise ValueError(f"{address} lists versions of the layer {name!r}, which this workspace does not hold")
    if held.version not in listed:
        raise ValueError(f"{address} does not list version {held.version} of {name}, so nothing there follows it")
    version, applied = held.version, []
    with TemporaryDirectory() as work:
        while version in steps:
            following, file, digest = steps[version]
            url = urljoin(address, file)
            if not url.startswith(address):
                raise ValueError(f"{address}versions.json names a file outside its own address: {file}")
            path = Path(work) / "change.sqlite"
            _download(url, digest, path, open_url)
            result = store.apply_layer_changes(path, follows=version)
            applied.append(result)
            if result.version != following:
                raise ValueError(f"{url} holds version {result.version}, not the {following} its list gives; "
                                 f"{name} is now at {result.version}")
            path.unlink()
            version = following
    return LayerPull(name, held.version, version, applied)


def _unfetched(url: str, error: Exception) -> ValueError:
    if isinstance(error, HTTPError):
        error.close()  # It holds the response.
    return ValueError(f"could not fetch {url}: {error}")


def _download(url: str, digest: str, path: Path, open_url) -> None:
    """Save a change file, unpacked if gzipped, having checked the bytes served against the hash listed."""
    found, unpack = sha256(), zlib.decompressobj(wbits=31) if url.endswith(".gz") else None
    try:
        with open_url(Request(url, headers=_AGENT), timeout=60) as response, path.open("wb") as file:
            while block := response.read(1 << 20):
                found.update(block)
                file.write(unpack.decompress(block) if unpack else block)
            if unpack:
                file.write(unpack.flush())
    except (URLError, TimeoutError, zlib.error) as error:
        raise _unfetched(url, error) from None
    if found.hexdigest() != digest:
        raise ValueError(f"{url} does not match the hash its list gives; nothing of it was applied")
