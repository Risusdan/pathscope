import pathlib
import pytest
from collections import OrderedDict
from core.target.layout_io import (LayoutPatchError, patch_layout_text,
                                   save_layout)

FIX = pathlib.Path("tests/fixtures/layout_roundtrip.topology.yaml")


def _orig():
    return FIX.read_text()


def _blocks_from(text):
    # helper: parse the current layout section values with PyYAML so
    # tests express "same as before except adc1 moved". "legend" is
    # not a block (no w/h), so it is excluded here.
    import yaml
    doc = yaml.safe_load(text)
    return OrderedDict((k, (v["x"], v["y"], v["w"], v["h"]))
                       for k, v in doc["layout"].items()
                       if k != "legend")


def test_untouched_file_regions_are_byte_identical():
    orig = _orig()
    out = patch_layout_text(orig, _blocks_from(orig), {}, None)
    head_orig = orig.split("layout:")[0]
    head_out = out.split("layout:")[0]
    assert head_out == head_orig          # every byte before layout:


def test_moved_block_changes_only_its_layout_line():
    orig = _orig()
    blocks = _blocks_from(orig)
    x, y, w, h = blocks["adc1"]
    blocks["adc1"] = (x + 10, y, w, h)
    out = patch_layout_text(orig, blocks, {}, None)
    changed = [l for l in out.splitlines() if "adc1" in l and "x:" in l]
    assert len(changed) == 1 and ("x: %d" % (x + 10)) in changed[0]
    import yaml
    assert yaml.safe_load(out)["layout"]["adc1"]["x"] == x + 10


def test_edge_points_rewritten_in_place_preserving_rest_of_line():
    orig = _orig()
    key = ("adc1", "mux0", "DR")          # match the fixture's edge
    out = patch_layout_text(orig, _blocks_from(orig),
                            {key: [(1, 2), (3, 4)]}, None)
    line = next(l for l in out.splitlines()
                if "adc1" in l and "points" in l)
    assert "[[1, 2], [3, 4]]" in line
    # when_select/label spacing on the same entry untouched:
    assert out.count("when_select") == orig.count("when_select")


def test_edge_without_points_gains_entry_only_when_asked():
    orig = _orig()
    key = ("mux0", "dma2", "S0")          # fixture's pointless edge
    out = patch_layout_text(orig, _blocks_from(orig),
                            {key: [(5, 5)]}, None)
    import yaml
    doc = yaml.safe_load(out)
    edge = next(e for e in doc["edges"]
                if e["from"] == "mux0" and e["to"] == "dma2")
    assert edge["points"] == [[5, 5]]


def test_legend_key_emitted_and_replaced():
    orig = _orig()
    out = patch_layout_text(orig, _blocks_from(orig), {}, (700, 400))
    assert "legend:" in out.split("layout:")[1]
    out2 = patch_layout_text(out, _blocks_from(out), {}, (100, 100))
    assert "x: 100" in [l for l in out2.splitlines()
                        if "legend" in l][0]


def test_save_layout_atomic_and_validated(tmp_path):
    p = tmp_path / "t.topology.yaml"
    p.write_text(_orig())
    def bad_validate(path):
        raise ValueError("boom")
    with pytest.raises(LayoutPatchError):
        save_layout(str(p), _blocks_from(_orig()), {}, None,
                    validate=bad_validate)
    assert p.read_text() == _orig()       # original intact
