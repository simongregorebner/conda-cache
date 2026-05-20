# Overview

This is a lightweight, dependency-minimal caching proxy for conda package channels.
Inspired by [Quetz](https://github.com/mamba-org/quetz) but focused on a single
use-case: **transparently proxy and cache upstream channels** so your team or
CI pipelines can install packages quickly from a local mirror.

---

## Features

- **Proxy any conda channel** (Anaconda, conda-forge, bioconda, custom)
- **Disk-based cache** – packages cached forever; repodata with configurable TTL
- **Stale-while-offline** – serves stale cached files if upstream is unreachable
- **Atomic writes** – no corrupt partial files on disk
- **Admin endpoints** – purge cache or force repodata refresh via HTTP
- Single-file Python server (~200 lines), easy to audit and extend

---

## Requirements

```bash
pip install -r requirements.txt
```

---

## Quickstart

```bash
# 1. Edit config.toml to set the channels you want to proxy
# 2. Start the server
python server.py --config config.toml

# 3. Install packages via the proxy
mamba install -c http://localhost:8000/channels/conda-forge -c http://localhost:8000/channels/defaults numpy
```

### CLI options

```
python server.py --config config.toml   # use config file
                 --host 0.0.0.0         # bind to all interfaces
                 --port 8080            # custom port
                 --reload               # auto-reload on code changes (dev)
```

---

## Configuration (`config.toml`)

```toml
[cache]
cache_dir = "./conda_cache"
repodata_ttl = 300    # seconds; 0 = always re-fetch
package_ttl  = -1     # -1 = cache forever (recommended for packages)

[channels.conda-forge]
url = "https://conda.anaconda.org/conda-forge"

[channels.pytorch]
url = "https://conda.anaconda.org/pytorch"
```

---

## URL structure

```
http://localhost:8000/channels/<channel-alias>/<subdir>/<filename>
```

Examples:

```
http://localhost:8000/channels/conda-forge/linux-64/repodata.json
http://localhost:8000/channels/conda-forge/linux-64/numpy-1.26.0-py311h...conda
http://localhost:8000/channels/defaults/channeldata.json
```

---

## Admin API

| Method | Path | Description |
|--------|------|-------------|
| `GET`    | `/` | List configured channels |
| `GET`    | `/admin/cache/stats` | Disk usage per channel |
<!-- | `DELETE` | `/admin/cache/{channel}` | Purge all files for a channel | -->
| `DELETE` | `/admin/cache/{channel}/repodata` | Purge only repodata (force index refresh) |

---

## Using with conda / mamba

**Ad-hoc:**
```bash
mamba install -c http://localhost:8000/channels/conda-forge numpy pandas
```

**In `~/.condarc`:**
```yaml
channels:
  - http://localhost:8000/channels/conda-forge
```

**In `environment.yml`:**
```yaml
name: myenv
channels:
  - http://localhost:8000/channels/conda-forge
dependencies:
  - numpy
  - pandas
```

---

## Deployment tips

- Run behind **nginx** or **caddy** for TLS termination.
- Mount `cache_dir` on an SSD or NFS share for team use.
- Use `repodata_ttl = 0` in CI to always get fresh package indices.


# Development

Build and run the server with Docker:

```bash
docker build . -t conda-cache
docker run -p 8000:8000 -v $(pwd)/config.toml:/app/config.toml -v $(pwd)/conda-cache:/cache conda-cache
```
