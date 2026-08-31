import importlib
import pathlib
import sys


def test_gui_import_inserts_vendored_warp_path(monkeypatch):
    root = pathlib.Path(__file__).resolve().parents[1]
    vendored_warp = str(root / 'NvidiaWarp-GarmentCode')

    for name in ['gui', 'gui.callbacks', 'gui.gui_pattern']:
        sys.modules.pop(name, None)

    monkeypatch.syspath_prepend(vendored_warp)
    module = importlib.import_module('gui')

    assert module is not None
    assert vendored_warp in sys.path
