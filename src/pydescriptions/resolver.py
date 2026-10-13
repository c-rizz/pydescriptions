#!/usr/bin/env python3
"""Resolve ROS description packages to filesystem paths, cloning their repositories on demand.

Two modes, both through :func:`resolve`:

- **cache** (no ``root``): repositories are cloned at the commit pinned in ``index.toml`` into a
  content-addressed cache and reused from there. Reproducible, and not meant to be edited.
- **workspace** (``root=...``): repositories live as ordinary clones under a directory of your
  choosing. Whatever is already there is used exactly as-is, so you can edit and commit from it;
  only missing repositories are cloned. This is the mode to use while developing descriptions.

Cloning is safe to trigger from many processes at once, which matters because training spawns a
collector process per worker and they all build environments simultaneously.
"""
from __future__ import annotations

import fcntl
import os
import shutil
import subprocess
import sys
import tempfile
import tomllib
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

_SHA_CHARS = 40
_warned : set[str] = set()


def _log(msg : str) -> None:
    print(f"[pydescriptions] {msg}", file=sys.stderr)


def _warn_once(key : str, msg : str) -> None:
    if key not in _warned:
        _warned.add(key)
        _log(f"WARNING: {msg}")


@dataclass
class RepoEntry:
    """One description repository and the ROS packages it provides."""
    name : str
    url : str
    commit : str
    packages : tuple[str,...]
    package_subdirs : dict[str,str] = field(default_factory=dict)

    def subdir_of(self, package : str) -> str:
        """Subdirectory of a package inside the repository (defaults to the package name)."""
        return self.package_subdirs.get(package, package)


def is_sha(ref : str) -> bool:
    """Whether ``ref`` is a full commit sha rather than a branch or tag."""
    return len(ref) == _SHA_CHARS and all(c in "0123456789abcdef" for c in ref.lower())


def entry_from_dict(name : str, data : dict, require_sha : bool = False) -> RepoEntry:
    """Build a :class:`RepoEntry` from a dict, as used in the index or in ``extra_repos``.

    ``commit`` and ``ref`` are interchangeable; only the index insists on a full sha.
    """
    ref = str(data.get("commit", data.get("ref", "")))
    missing = [k for k in ("url", "packages") if k not in data] + ([] if ref else ["commit/ref"])
    if missing:
        raise RuntimeError(f"repo '{name}' is missing {missing}")
    if require_sha and not is_sha(ref):
        raise RuntimeError(f"repo '{name}' has commit '{ref}', which is not a full {_SHA_CHARS}-character "
                           f"sha. The index takes only pinned commits: the cache is keyed by them, and a "
                           f"moving ref would hand different machines different geometry. Pass a branch "
                           f"through resolve(refs=...) instead.")
    return RepoEntry(name=name,
                     url=str(data["url"]),
                     commit=ref,
                     packages=tuple(data["packages"]),
                     package_subdirs=dict(data.get("package_subdirs", {})))


def cache_dir() -> Path:
    """Where repositories are cached in cache mode."""
    env = os.environ.get("PYDESCRIPTIONS_CACHE")
    if env:
        return Path(env).expanduser()
    xdg = os.environ.get("XDG_CACHE_HOME")
    base = Path(xdg).expanduser() if xdg else Path.home()/".cache"
    return base/"pydescriptions"


def load_index() -> dict[str, RepoEntry]:
    """Load the repository index."""
    path = Path(__file__).parent/"index.toml"
    with open(path, "rb") as f:
        data = tomllib.load(f)
    repos : dict[str, RepoEntry] = {}
    for name, entry in data.get("repos", {}).items():
        try:
            repos[name] = entry_from_dict(name, entry, require_sha=True)
        except RuntimeError as e:
            raise RuntimeError(f"in {path}: {e}") from None
    return repos


def package_index() -> dict[str, RepoEntry]:
    """Map every ROS package name in the index to the repository providing it."""
    packages : dict[str, RepoEntry] = {}
    for entry in load_index().values():
        for package in entry.packages:
            previous = packages.get(package)
            if previous is not None and previous.name != entry.name:
                raise RuntimeError(f"package '{package}' is provided by both '{previous.name}' and "
                                   f"'{entry.name}'")
            packages[package] = entry
    return packages


def _global_git_config() -> Path:
    """A global git config for our own git calls, trusting the checkouts we create ourselves.

    The checkouts often sit on a mount that reports a different owner than the process running git
    (an sshfs workspace, or a container running as root), and git then refuses to work in them:
    "detected dubious ownership in repository". ``safe.directory`` lifts that, but git only honours
    it from a system or global config -- explicitly not from ``-c safe.directory=...`` -- so the only
    way to set it programmatically is to point ``GIT_CONFIG_GLOBAL`` at a config of our own.

    That would otherwise hide the user's real global config, which is where url rewrites and
    credential helpers live and which private repositories need, so it is included first.

    The scope of the exception is only this package's git subprocesses; git run anywhere else keeps
    its ownership checks.
    """
    # One shared file per user rather than one per process: a few thousand workers must not leave a
    # few thousand temporary files behind.
    path = Path(tempfile.gettempdir())/f"pydescriptions-gitconfig-{os.getuid()}"
    existing = os.environ.get("GIT_CONFIG_GLOBAL")
    if existing:
        candidates = [Path(existing)]
    else:
        xdg = os.environ.get("XDG_CONFIG_HOME")
        base = Path(xdg).expanduser() if xdg else Path.home()/".config"
        candidates = [base/"git"/"config", Path.home()/".gitconfig"]
    content = "".join(f"[include]\n\tpath = {c}\n" for c in candidates if c.is_file())
    content += "[safe]\n\tdirectory = *\n"
    # Written atomically: every worker rewrites it, and a torn read would break all of them.
    tmp = path.with_name(f"{path.name}.{os.getpid()}")
    tmp.write_text(content)
    os.replace(tmp, path)
    return path


def _git(args : list[str], cwd : Path | None = None) -> str:
    env = dict(os.environ, GIT_CONFIG_GLOBAL=str(_global_git_config()))
    done = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, env=env)
    if done.returncode != 0:
        # git's own message is the only useful part of a failure, so it has to reach the caller
        # rather than being hidden behind a bare exit status.
        where = f" (in {cwd})" if cwd is not None else ""
        output = done.stderr.strip() or done.stdout.strip() or "<no output>"
        raise RuntimeError(f"'git {' '.join(args)}' failed with exit {done.returncode}{where}:\n{output}")
    return done.stdout.strip()


def _remote_hint(url : str, dest : Path, ref : str) -> str:
    """Advice for a failure to reach ``url``, which is almost always missing credentials.

    Both ways out are spelled out, including the manual clone: nothing here ever touches a checkout
    that already exists, so a directory the user populates by hand is picked up as it is.
    """
    manual = (f"  - or clone it yourself, and it will be used as it is:\n"
              f"        git clone {url} {dest}\n"
              f"        git -C {dest} checkout {ref}")
    if url.startswith("git@") or url.startswith("ssh://"):
        return ("\nThis is an ssh url, so it needs an ssh key the remote accepts. Either:\n"
                "  - make a key available to this process:\n"
                "        eval $(ssh-agent)\n"
                "        ssh-add ~/.ssh/id_ed25519      # or whichever key the remote knows\n"
                "    and check it works with: ssh -T git@github.com\n"
                f"{manual}")
    return ("\nCheck network access to the remote, and credentials if the repository is private.\n"
            f"{manual}")


def _locked_publish(dest : Path, lock_path : Path, build : Callable[[Path], None]) -> Path:
    """Build ``dest`` exactly once across processes, publishing it atomically.

    The first process in takes an exclusive lock and builds into a temporary directory, which is
    renamed into place; the others block on the lock and find the finished result. So a few thousand
    workers starting together do the work once and never observe a partial checkout.
    """
    if dest.is_dir():
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "w") as lock_file:
        fcntl.flock(lock_file, fcntl.LOCK_EX)
        if dest.is_dir():  # somebody else finished while we waited
            return dest
        tmp = dest.parent/f".tmp-{os.getpid()}-{dest.name}"
        shutil.rmtree(tmp, ignore_errors=True)
        try:
            build(tmp)
            os.rename(tmp, dest)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    return dest


def fetch_repo(entry : RepoEntry) -> Path:
    """Path of ``entry``'s checkout in the cache, cloning it at the pinned commit if missing."""
    dest = cache_dir()/entry.name/entry.commit

    def build(tmp : Path) -> None:
        tmp.mkdir(parents=True)
        _git(["init", "-q"], cwd=tmp)
        _git(["remote", "add", "origin", entry.url], cwd=tmp)
        try:
            # Fetching a bare sha needs the server to allow it (github does); fall back to a full
            # fetch for servers that do not.
            _git(["fetch", "-q", "--depth", "1", "origin", entry.commit], cwd=tmp)
        except RuntimeError:
            # If the full fetch fails too, the problem is the remote or the credentials rather than
            # the shallow fetch, so report that one and drop the first.
            try:
                _git(["fetch", "-q", "origin"], cwd=tmp)
            except RuntimeError as e:
                raise RuntimeError(f"could not fetch {entry.name} from {entry.url}.\n{e}"
                                   f"{_remote_hint(entry.url, dest, entry.commit)}") from None
        _git(["checkout", "-q", "--detach", entry.commit], cwd=tmp)
        if (tmp/".gitmodules").is_file():
            _git(["submodule", "update", "--init", "--recursive", "--depth", "1"], cwd=tmp)

    if not dest.is_dir():
        _log(f"fetching {entry.name} at {entry.commit[:12]} from {entry.url}")
    return _locked_publish(dest, dest.parent/f".{entry.commit}.lock", build)


def workspace_root(entry : RepoEntry, root : str | Path, ref : str | None = None,
                   allow_download : bool = True) -> Path:
    """Path of ``entry`` inside the workspace directory ``root``, cloning it there if missing.

    An existing directory is used exactly as it is -- never checked out, never updated: it is your
    working copy and may hold uncommitted edits. Only a missing one is cloned, at ``ref``
    (defaulting to the entry's pinned commit), as a full clone you can commit from.
    """
    root = Path(root).expanduser()
    dest = root/entry.name
    ref = entry.commit if ref is None else ref
    if not is_sha(ref):
        _warn_once(f"ref:{entry.name}:{ref}",
                   f"'{entry.name}' is taken at '{ref}', which is not a pinned commit, so what this "
                   f"resolves to can change between machines and over time")
    if dest.is_dir():
        return dest
    if not allow_download:
        raise FileNotFoundError(f"'{entry.name}' is not in {root} and downloading is disabled")

    def build(tmp : Path) -> None:
        # Full clone, not the cache's shallow one: a workspace checkout is meant to be worked in.
        try:
            _git(["clone", entry.url, str(tmp)])
        except RuntimeError as e:
            raise RuntimeError(f"could not clone {entry.name} from {entry.url}.\n{e}"
                               f"{_remote_hint(entry.url, dest, ref)}") from None
        _git(["checkout", ref], cwd=tmp)

    _log(f"cloning {entry.name} at {ref} into {dest}")
    return _locked_publish(dest, root/f".{entry.name}.lock", build)


def resolve(*packages : str | Sequence[str],
            root : str | Path | None = None,
            refs : dict[str,str] | None = None,
            extra_repos : dict[str,dict] | None = None,
            allow_download : bool = True) -> dict[str,str]:
    """Paths of description packages, ready for ``compile_xacro_string(extra_pkg_paths=...)``.

    >>> extra = resolve("kyon_urdf", "dagana_urdf")                       # cache mode
    >>> extra = resolve(["kyon_urdf", "zed_description"],                 # workspace mode
    ...                 root="~/adarl_ws/src/descriptions",
    ...                 refs={"iit-kyon-ros-pkg": "my-feature-branch"})

    Parameters
    ----------
    packages
        Package names, either as separate arguments or as a single sequence.
    root
        Workspace directory holding one editable clone per repository. None uses a cache instead.
    refs
        Per-repository branch/tag/sha to clone when a checkout is missing, overriding the index's
        pinned commit. Only valid together with ``root``, since the cache is keyed by commit.
    extra_repos
        Repositories that are not in the index, as ``{name: {url, commit|ref, packages}}``. Lets an
        experiment declare its descriptions in one call without touching ``index.toml``.
    allow_download
        False to fail instead of cloning anything.

    Returns
    -------
    dict[str,str]
        ``package name -> path``.
    """
    names : list[str] = []
    for package in packages:
        if isinstance(package, str):
            names.append(package)
        else:
            names.extend(package)

    if refs and root is None:
        raise ValueError("refs= only applies with root=: the cache is keyed by the pinned commit, so "
                         "there is nothing for a ref to change there")

    known = package_index()
    if extra_repos:
        for repo_name, data in extra_repos.items():
            entry = entry_from_dict(repo_name, data)
            for package in entry.packages:
                known[package] = entry
    refs = refs or {}

    resolved : dict[str,str] = {}
    for package in names:
        entry = known.get(package)
        if entry is None:
            raise KeyError(f"unknown description package '{package}'. Known packages: {sorted(known)}. "
                           f"Pass it through extra_repos, or add it to the index.")
        if root is not None:
            base = workspace_root(entry, root, ref=refs.get(entry.name), allow_download=allow_download)
        else:
            dest = cache_dir()/entry.name/entry.commit
            if not dest.is_dir() and not allow_download:
                raise FileNotFoundError(f"'{entry.name}' is not in the cache ({dest}) and downloading is "
                                        f"disabled")
            base = fetch_repo(entry)
        path = base/entry.subdir_of(package)
        if not path.is_dir():
            raise FileNotFoundError(f"package '{package}' should be at {path}, but that directory does "
                                    f"not exist")
        resolved[package] = str(path)
    return resolved


def get_path(package : str, **kwargs) -> str:
    """Path of a single description package. Takes the same keywords as :func:`resolve`."""
    return resolve(package, **kwargs)[package]
