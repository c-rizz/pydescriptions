#!/usr/bin/env python3
"""Compile kyon's xacro into a urdf, resolving its descriptions through pydescriptions.

    python -m pydescriptions.xacro_compile_example
    python -m pydescriptions.xacro_compile_example --root ~/adarl_ws/src/descriptions
    python -m pydescriptions.xacro_compile_example --root ~/adarl_ws/src/descriptions --ref my-branch

Without --root the repositories come from the pinned cache. With it they are ordinary clones under
that directory: whatever is already there is used as-is, so you can edit the xacro and rerun, and
only missing repositories are cloned.
"""
from __future__ import annotations

import argparse
from pathlib import Path

from adarl.utils.base_utils import compile_xacro_string

from pydescriptions import resolve


def main(argv : list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--root", default=None,
                        help="directory holding one editable clone per repository; "
                             "without it the pinned cache is used")
    parser.add_argument("--ref", default=None,
                        help="branch/tag/sha to clone kyon at instead of the pinned commit "
                             "(only valid together with --root)")
    args = parser.parse_args(argv)

    # One call per model. The returned dict is both where the top-level xacro lives and exactly what
    # extra_pkg_paths wants, so pulling in another description (zed_description, dagana_urdf, ...) is
    # just adding its name here.
    descriptions = resolve("kyon_urdf",
                           root=args.root,
                           refs={"iit-kyon-ros-pkg": args.ref} if args.ref else None)

    model_path = Path(descriptions["kyon_urdf"])/"urdf/kyon.urdf.xacro"
    urdf_string = compile_xacro_string(model_definition_string=model_path.read_text(),
                                       model_kwargs={},
                                       extra_pkg_paths=descriptions)
    print(urdf_string)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
