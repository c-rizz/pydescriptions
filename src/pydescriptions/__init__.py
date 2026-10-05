"""Index of robot-description repositories, cloned on demand and resolved to filesystem paths.

The result is shaped for the xacro compiler, which takes a ``package name -> path`` dict:

>>> from pydescriptions import resolve
>>> from adarl.utils.base_utils import compile_xacro_string
>>> extra = resolve("kyon_urdf", "dagana_urdf")
>>> compile_xacro_string(xacro_string, extra_pkg_paths=extra)         # doctest: +SKIP

That is cache mode: each repository is cloned once at the commit pinned in ``index.toml`` and
reused from a shared cache. To work on the descriptions instead, pass a directory of your own and
they become ordinary clones you can edit and commit from:

>>> extra = resolve(["kyon_urdf", "zed_description"],
...                 root="~/adarl_ws/src/descriptions",
...                 refs={"iit-kyon-ros-pkg": "my-feature-branch"},
...                 extra_repos={"zed-ros2-wrapper": {"url": "...", "ref": "main",
...                                                   "packages": ["zed_description"]}})

Anything already present under ``root`` is used as-is; only missing repositories are cloned.
"""
from pydescriptions.resolver import (RepoEntry, cache_dir, entry_from_dict, fetch_repo, get_path,
                                     is_sha, load_index, package_index, resolve, workspace_root)

__all__ = ["RepoEntry", "cache_dir", "entry_from_dict", "fetch_repo", "get_path", "is_sha",
           "load_index", "package_index", "resolve", "workspace_root"]
