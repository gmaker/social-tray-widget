"""Launcher — starts the widget whatever this folder happens to be called.

The code is a package and uses relative imports, so it must be imported *as* a
package. The usual `python -m social_widget` does that, but only if the checkout
directory is named exactly `social_widget` and you run from its parent — and a
`git clone` names the directory after the repo instead. So register this
directory under the package name here, and the folder name stops mattering.

    python run.py          # or just double-click start.bat
"""

from __future__ import annotations

import importlib.util
import os
import sys

_NAME = "social_widget"
_HERE = os.path.dirname(os.path.abspath(__file__))


def _load_package():
    if _NAME in sys.modules:
        return sys.modules[_NAME]
    spec = importlib.util.spec_from_file_location(
        _NAME, os.path.join(_HERE, "__init__.py"),
        submodule_search_locations=[_HERE],
    )
    pkg = importlib.util.module_from_spec(spec)
    # Register before exec: relative imports inside the package resolve through
    # sys.modules, so it has to be findable while it is still loading.
    sys.modules[_NAME] = pkg
    spec.loader.exec_module(pkg)
    return pkg


if __name__ == "__main__":
    _load_package()
    from social_widget.main import main
    main()
