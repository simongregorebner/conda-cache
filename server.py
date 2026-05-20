"""
conda-proxy: A lightweight caching proxy for conda package channels.

Usage:
    pip install fastapi uvicorn httpx
    python server.py --config config.toml
    python server.py --config config.toml --host 0.0.0.0 --port 8080

Then point conda/mamba at it:
    conda install -c http://localhost:8000/channels/defaults numpy
    # or set in ~/.condarc:
    #   channels:
    #     - http://localhost:8000/channels/defaults
"""

import asyncio
import hashlib
import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

import httpx
import tomllib
import uvicorn
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, StreamingResponse

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("conda-proxy")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

DEFAULT_CONFIG = {
    "server": {
        "host": "127.0.0.1",
        "port": 8000,
        "cache_dir": "./conda_cache",
        "log_level": "info",
    },
    "cache": {
        # Repodata (repodata.json / repodata.json.zst / current_repodata.json)
        # is re-fetched from upstream if older than this many seconds.
        "repodata_ttl": 300,  # 5 minutes
        # Package files (.conda / .tar.bz2) are immutable – cache forever.
        # Set to -1 to never evict. Positive value = max age in seconds.
        "package_ttl": -1,
    },
    # channels section is populated by the config file, e.g.:
    # [channels.defaults]
    # url = "https://repo.anaconda.com/pkgs/main"
    #
    # [channels.conda-forge]
    # url = "https://conda.anaconda.org/conda-forge"
    "channels": {},
}


def load_config(path: Optional[str]) -> dict:
    cfg = {k: dict(v) for k, v in DEFAULT_CONFIG.items()}
    if path is None:
        log.warning(
            "No config file supplied – using built-in defaults with no channels."
        )
        return cfg
    with open(path, "rb") as fh:
        user = tomllib.load(fh)
    # Deep-merge top-level sections
    for section, values in user.items():
        if section in cfg and isinstance(cfg[section], dict):
            cfg[section].update(values)
        else:
            cfg[section] = values
    if not cfg.get("channels"):
        log.warning("Config loaded but no [channels.*] sections found.")
    return cfg


# ---------------------------------------------------------------------------
# Cache helpers
# ---------------------------------------------------------------------------

REPODATA_FILES = {
    "repodata.json",
    "repodata.json.bz2",
    "repodata.json.zst",
    "current_repodata.json",
    "current_repodata.json.bz2",
    "repodata_from_packages.json",
    "repodata_from_packages.json.bz2",
    "channeldata.json",
}

PACKAGE_EXTENSIONS = {".conda", ".tar.bz2"}


def is_repodata(filename: str) -> bool:
    return filename in REPODATA_FILES


def is_package(filename: str) -> bool:
    return any(filename.endswith(ext) for ext in PACKAGE_EXTENSIONS)


def cache_path_for(cache_root: Path, channel_name: str, subpath: str) -> Path:
    """Map a request subpath to a local cache file."""
    safe = subpath.lstrip("/")
    return cache_root / channel_name / safe


def is_cache_fresh(path: Path, ttl: int) -> bool:
    """Return True when the cached file exists and is within TTL."""
    if not path.exists():
        return False
    if ttl < 0:
        return True  # cache forever
    age = time.time() - path.stat().st_mtime
    return age < ttl


# ---------------------------------------------------------------------------
# Application factory
# ---------------------------------------------------------------------------


def create_app(config: dict) -> FastAPI:
    cache_root = Path(config["server"]["cache_dir"]).expanduser().resolve()
    cache_root.mkdir(parents=True, exist_ok=True)

    repodata_ttl: int = config["cache"]["repodata_ttl"]
    package_ttl: int = config["cache"]["package_ttl"]

    channels: dict[str, str] = {
        name: ch_cfg["url"].rstrip("/")
        for name, ch_cfg in config.get("channels", {}).items()
    }

    log.info("Cache directory : %s", cache_root)
    log.info("Repodata TTL    : %s s", repodata_ttl)
    log.info("Package TTL     : %s s  (-1 = forever)", package_ttl)
    for name, url in channels.items():
        log.info("Channel %-20s → %s", name, url)

    # Shared async HTTP client (initialised in lifespan)
    http_client: httpx.AsyncClient = None  # type: ignore[assignment]

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        nonlocal http_client
        http_client = httpx.AsyncClient(
            follow_redirects=True,
            timeout=httpx.Timeout(30.0, connect=10.0),
        )
        yield
        await http_client.aclose()

    app = FastAPI(title="conda-proxy", version="0.1.0", lifespan=lifespan)

    # ------------------------------------------------------------------
    # Routes
    # ------------------------------------------------------------------

    @app.get("/")
    async def index():
        return {
            "service": "conda-proxy",
            "channels": {name: f"/channels/{name}/" for name in channels},
        }

    @app.get("/channels/{channel_name}/{subpath:path}")
    async def proxy(channel_name: str, subpath: str, request: Request):
        """
        Proxy and cache requests for a configured channel.

        conda/mamba will request paths like:
          /channels/<channel>/linux-64/repodata.json
          /channels/<channel>/linux-64/numpy-1.26.0-py311h...conda
          /channels/<channel>/channeldata.json
        """
        if channel_name not in channels:
            raise HTTPException(
                status_code=404,
                detail=f"Unknown channel '{channel_name}'. "
                f"Available: {list(channels.keys())}",
            )

        upstream_base = channels[channel_name]
        filename = Path(subpath).name

        # Choose TTL based on file type
        if is_repodata(filename):
            ttl = repodata_ttl
        elif is_package(filename):
            ttl = package_ttl
        else:
            # Unknown files (icons, etc.) – short TTL
            ttl = 60

        local_file = cache_path_for(cache_root, channel_name, subpath)

        # ---- Serve from cache if fresh --------------------------------
        if is_cache_fresh(local_file, ttl):
            log.debug("CACHE HIT  %s/%s", channel_name, subpath)
            return FileResponse(
                path=local_file,
                media_type=_media_type(filename),
                headers={"X-Conda-Proxy-Cache": "HIT"},
            )

        # ---- Fetch from upstream --------------------------------------
        upstream_url = f"{upstream_base}/{subpath}"
        log.info("UPSTREAM   %s", upstream_url)

        try:
            upstream_resp = await http_client.get(
                upstream_url,
                headers=_forward_headers(request),
            )
        except httpx.RequestError as exc:
            log.error("Upstream request failed: %s", exc)
            # If we have a stale cache entry, serve it with a warning
            if local_file.exists():
                log.warning("Serving stale cache for %s/%s", channel_name, subpath)
                return FileResponse(
                    path=local_file,
                    media_type=_media_type(filename),
                    headers={"X-Conda-Proxy-Cache": "STALE"},
                )
            raise HTTPException(status_code=502, detail=f"Upstream unreachable: {exc}")

        if upstream_resp.status_code == 404:
            raise HTTPException(
                status_code=404, detail=f"Not found upstream: {subpath}"
            )

        if upstream_resp.status_code != 200:
            log.warning(
                "Upstream returned %s for %s", upstream_resp.status_code, upstream_url
            )
            raise HTTPException(
                status_code=upstream_resp.status_code,
                detail="Upstream error",
            )

        # ---- Write to cache atomically --------------------------------
        local_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = local_file.with_suffix(local_file.suffix + ".tmp")
        try:
            with open(tmp, "wb") as fh:
                async for chunk in upstream_resp.aiter_bytes(chunk_size=65536):
                    fh.write(chunk)
            tmp.replace(local_file)
            log.info(
                "CACHED     %s  (%s bytes)",
                local_file.relative_to(cache_root),
                local_file.stat().st_size,
            )
        except Exception as exc:
            tmp.unlink(missing_ok=True)
            log.error("Failed to cache %s: %s", local_file, exc)
            raise HTTPException(status_code=500, detail="Cache write error")

        return FileResponse(
            path=local_file,
            media_type=_media_type(filename),
            headers={"X-Conda-Proxy-Cache": "MISS"},
        )

    # ------------------------------------------------------------------
    # Cache management endpoints
    # ------------------------------------------------------------------

    @app.delete("/admin/cache/{channel_name}")
    async def purge_channel_cache(channel_name: str):
        """Delete all cached files for a channel (forces re-fetch)."""
        if channel_name not in channels:
            raise HTTPException(status_code=404, detail="Unknown channel")
        import shutil

        target = cache_root / channel_name
        if target.exists():
            shutil.rmtree(target)
            log.info("Purged cache for channel '%s'", channel_name)
        return {"purged": channel_name}

    @app.delete("/admin/cache/{channel_name}/repodata")
    async def purge_repodata(channel_name: str):
        """Delete only the repodata files for a channel (forces index refresh)."""
        if channel_name not in channels:
            raise HTTPException(status_code=404, detail="Unknown channel")
        count = 0
        for path in (cache_root / channel_name).rglob("*"):
            if path.is_file() and is_repodata(path.name):
                path.unlink()
                count += 1
        log.info("Purged %d repodata file(s) for channel '%s'", count, channel_name)
        return {"purged_repodata_files": count, "channel": channel_name}

    @app.get("/admin/cache/stats")
    async def cache_stats():
        """Show disk usage per channel."""
        stats = {}
        for ch in channels:
            ch_dir = cache_root / ch
            if not ch_dir.exists():
                stats[ch] = {"files": 0, "bytes": 0}
                continue
            files = list(ch_dir.rglob("*"))
            total = sum(f.stat().st_size for f in files if f.is_file())
            stats[ch] = {"files": sum(1 for f in files if f.is_file()), "bytes": total}
        return stats

    return app


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_MEDIA_TYPES = {
    ".json": "application/json",
    ".bz2": "application/x-bzip2",
    ".zst": "application/zstd",
    ".conda": "application/x-conda",
    ".tar.bz2": "application/x-bzip2",
    ".html": "text/html",
}


def _media_type(filename: str) -> str:
    for ext, mt in _MEDIA_TYPES.items():
        if filename.endswith(ext):
            return mt
    return "application/octet-stream"


def _forward_headers(request: Request) -> dict:
    """Pass a safe subset of client headers upstream."""
    keep = {"user-agent", "accept", "accept-encoding", "accept-language"}
    return {k: v for k, v in request.headers.items() if k.lower() in keep}


# ---------------------------------------------------------------------------
# CLI entry-point
# ---------------------------------------------------------------------------


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Conda channel proxy/cache server")
    parser.add_argument("--config", "-c", default=None, help="Path to config.toml")
    parser.add_argument("--host", default=None, help="Bind host (overrides config)")
    parser.add_argument(
        "--port", type=int, default=None, help="Bind port (overrides config)"
    )
    parser.add_argument(
        "--reload", action="store_true", help="Auto-reload on code changes (dev)"
    )
    args = parser.parse_args()

    config = load_config(args.config)

    host = args.host or config["server"]["host"]
    port = args.port or config["server"]["port"]
    log_level = config["server"].get("log_level", "info")

    app = create_app(config)

    uvicorn.run(
        app,
        host=host,
        port=port,
        log_level=log_level,
        reload=args.reload,
    )


if __name__ == "__main__":
    main()
