"""The spin-box arrows have to be clickable, because they are now the only way to nudge.

The wheel no longer changes these values at all, so clicking an arrow (or typing) is the
whole interface. A user reported that Up did nothing and merely focused the text box while
Down worked -- the sub-controls kept the style's native metric while the field grew with the
text setting, leaving a narrow sliver beside a much taller edit area.
"""

from __future__ import annotations

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QMouseEvent
from PySide6.QtWidgets import (
    QApplication,
    QDoubleSpinBox,
    QStyle,
    QStyleOptionSpinBox,
    QVBoxLayout,
    QWidget,
)

from yada.ui import theme

SCALES = (1.0, 1.6, 2.0)


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def _themed_spin(qapp, scale):
    theme.apply_text_scale(qapp, scale)
    qapp.setPalette(theme.blue_palette())
    qapp.setStyleSheet(theme.blue_stylesheet(qapp.font().pointSizeF()))
    host = QWidget()
    layout = QVBoxLayout(host)
    spin = QDoubleSpinBox()
    spin.setRange(0.1, 4.0)
    spin.setSingleStep(0.1)
    spin.setValue(1.0)
    layout.addWidget(spin)
    host.resize(320, 90)
    host.show()
    qapp.processEvents()
    return host, spin


def _rects(spin):
    opt = QStyleOptionSpinBox()
    opt.initFrom(spin)
    opt.subControls = QStyle.SubControl.SC_SpinBoxUp | QStyle.SubControl.SC_SpinBoxDown
    opt.rect = spin.rect()
    style = spin.style()
    get = lambda sc: style.subControlRect(  # noqa: E731
        QStyle.ComplexControl.CC_SpinBox, opt, sc, spin
    )
    return (
        get(QStyle.SubControl.SC_SpinBoxUp),
        get(QStyle.SubControl.SC_SpinBoxDown),
        get(QStyle.SubControl.SC_SpinBoxEditField),
    )


def _click(qapp, spin, rect):
    spin.setValue(1.0)
    qapp.processEvents()
    pos = QPointF(rect.center())
    gpos = QPointF(spin.mapToGlobal(rect.center()))
    for kind in (QMouseEvent.Type.MouseButtonPress, QMouseEvent.Type.MouseButtonRelease):
        qapp.sendEvent(
            spin,
            QMouseEvent(
                kind,
                pos,
                gpos,
                Qt.MouseButton.LeftButton,
                Qt.MouseButton.LeftButton,
                Qt.KeyboardModifier.NoModifier,
            ),
        )
    qapp.processEvents()
    return spin.value()


@pytest.mark.parametrize("scale", SCALES)
def test_both_arrows_change_the_value(qapp, scale):
    host, spin = _themed_spin(qapp, scale)
    assert host is not None
    up, down, _edit = _rects(spin)

    assert _click(qapp, spin, up) == pytest.approx(1.1), "Up must increment"
    assert _click(qapp, spin, down) == pytest.approx(0.9), "Down must decrement"


@pytest.mark.parametrize("scale", SCALES)
def test_the_arrows_do_not_overlap_the_text_field(qapp, scale):
    """Overlap is what turned a click on Up into a click in the text box."""
    host, spin = _themed_spin(qapp, scale)
    assert host is not None
    up, down, edit = _rects(spin)

    assert not up.isEmpty() and not down.isEmpty()
    assert not edit.intersects(up), "Up overlaps the edit field"
    assert not edit.intersects(down), "Down overlaps the edit field"


@pytest.mark.parametrize("scale", SCALES)
def test_the_arrows_leave_no_dead_strip_between_them(qapp, scale):
    """A fixed button height left a gap in the middle where clicks did nothing at all."""
    host, spin = _themed_spin(qapp, scale)
    assert host is not None
    up, down, _edit = _rects(spin)

    gap = down.top() - up.bottom() - 1
    assert gap <= 2, f"{gap}px of the button column does nothing when clicked"


@pytest.mark.parametrize("scale", SCALES)
def test_the_arrows_grow_with_the_text_setting(qapp, scale):
    """The original bug: a 14px target beside a 35px field at the larger text sizes."""
    host, spin = _themed_spin(qapp, scale)
    assert host is not None
    up, _down, _edit = _rects(spin)

    assert up.width() >= 18, f"up button is only {up.width()}px wide at scale {scale}"
    assert up.width() >= spin.height() * 0.5, (
        "the button column should scale with the control, not stay at a native metric"
    )


def test_every_asset_directory_is_bundled_by_the_spec():
    """The arrows and the tick are `image: url(...)`, and Qt draws nothing for a missing file.

    `icons` was absent from the spec's `datas` for ten releases, so the white checkbox tick
    existed only in a source checkout -- every released build drew an empty box. The spec
    now enumerates the asset directories; this asserts that it does, because the failure is
    invisible in the build log and in every test that runs from source.
    """
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    spec = (root / "packaging" / "yada.spec").read_text(encoding="utf-8")
    assets = root / "src" / "yada" / "assets"

    present = sorted(p.name for p in assets.iterdir() if p.is_dir())
    assert "icons" in present and "sounds" in present, present
    assert "ASSET_ROOT.iterdir()" in spec or "_asset_dirs" in spec, (
        "the spec must enumerate asset directories rather than list them by hand"
    )
    for name in present:
        assert any(f.is_file() for f in (assets / name).rglob("*")), (
            f"{name} is empty, so bundling it would ship nothing"
        )


def test_the_stylesheet_only_references_assets_that_exist():
    """A stylesheet url() pointing at a missing file fails silently, in every direction."""
    import re
    from pathlib import Path

    from yada.ui import theme

    for scale in SCALES:
        sheet = theme.blue_stylesheet(9.0 * scale)
        for url in re.findall(r'url\("([^"]+)"\)', sheet):
            assert Path(url).is_file(), f"stylesheet references a missing asset: {url}"
