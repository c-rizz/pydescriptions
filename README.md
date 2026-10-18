# pydescriptions

An index of robot-description repositories. This package holds a list of repositories 
with a pinned commit for each, clones them on demand, and resolves ROS package names 
to filesystem paths.

It does not require heavy dependencies and is pip-installable.
For xacro compilation you can use (xacro_standalone)[https://github.com/c-rizz/xacro_standalone/tree/extra_find_pkg_path] and the wrapper provided by (ADARL)[https://github.com/c-rizz/adarl].

It is used programmatically; there is no CLI. The result is shaped for the ADARL xacro compiler, which
takes a `package name -> path` dict:

```python
from pydescriptions import resolve
from adarl.utils.base_utils import compile_xacro_string
from pathlib import Path

# GETTING KYON URDF

descriptions = resolve("kyon_urdf","zed_description")
kyon_xacro_string = Path(descriptions["kyon_urdf"]+"/urdf/kyon.urdf.xacro").read_text()
kyon_urdf_string = compile_xacro_string(model_definition_string=xacro_string,
                                   model_kwargs={"camera_front_down":"true"},
                                   extra_pkg_paths=descriptions)

# GETTING CENTAURO URDF

descriptions = resolve("centauro_urdf","dagana_urdf")
centauro_xacro_string = Path(descriptions["centauro_urdf"]+"/urdf/centauro.urdf.xacro").read_text()
centauro_urdf_string = compile_xacro_string(model_definition_string=xacro_string,
                                            model_kwargs={
                                                "realsense":"false",
                                                "velodyne" :"false",
                                                "end_effector_left":"dagana",
                                                "floating_joint":"true"},
                                            extra_pkg_paths=descriptions)
```

## Runnable example

`xacro_compile_example.py` compiles kyon's xacro to a urdf, in either mode:

```bash
python -m pydescriptions.xacro_compile_example
```

## The two modes

**Cache** (no `root`) — each repository is cloned once at the commit pinned in `index.toml` into
`$PYDESCRIPTIONS_CACHE` (else `$XDG_CACHE_HOME/pydescriptions`, else `~/.cache/pydescriptions`),
laid out as `<repo>/<sha>/`. Keying by sha means several versions coexist and switching is free.
Reproducible, and nothing in it is meant to be edited.

**Workspace** (`root=...`) — repositories live as ordinary clones under a directory you choose:

```python
extra = resolve(["kyon_urdf", "zed_description"],
                root="~/adarl_ws/src/descriptions",
                refs={"iit-kyon-ros-pkg": "my-feature-branch"},
                extra_repos={"zed-ros2-wrapper": {
                    "url": "https://github.com/stereolabs/zed-ros2-wrapper.git",
                    "ref": "main", "packages": ["zed_description"]}})
```

Anything already under `root` is used **exactly as it is** — never checked out, never pulled, since
it is your working copy and may hold uncommitted edits. Only missing repositories are cloned, as
full clones at the requested ref, so you can branch and commit from them. That makes the edit loop
just: call `resolve`, edit the files, rerun.

| argument | meaning |
|---|---|
| `root` | workspace directory, one editable clone per repository. None uses the cache |
| `refs` | per-repository branch/tag/sha to clone when missing. Only valid with `root` |
| `extra_repos` | repositories absent from the index, `{name: {url, commit\|ref, packages}}` |
| `allow_download` | False to fail instead of cloning |


## Adding a description

```toml
[repos.zed-ros2-wrapper]
url = "https://github.com/stereolabs/zed-ros2-wrapper.git"
commit = "0123456789abcdef0123456789abcdef01234567"   # full sha, never a branch
packages = ["zed_description"]
```

Repositories are the unit, because one repository ships several ROS packages and they must come
from the same checkout. A package's subdirectory defaults to its own name; list exceptions under
`package_subdirs`.

`commit` must be a full 40-character sha; branches and tags are rejected here deliberately. The
cache is keyed by the commit, so a moving ref would hand different machines different geometry while
every config and log still said the same thing. Branches are fine through `refs=`, which only
applies to workspace mode — resolution then warns, since the result depends on when and where it ran.

Repositories you do not want in the index at all can go straight to `extra_repos=`.

## Concurrency

Cloning is safe from many processes at once, which matters because training spawns a collector
process per worker and they all build environments simultaneously. The first process in takes an
exclusive `flock`, clones into a temporary directory and `rename`s it into place; the others block
on the lock and then find the finished checkout. So N workers starting together produce exactly one
clone, and no process ever sees a partial one. This applies to both modes.
