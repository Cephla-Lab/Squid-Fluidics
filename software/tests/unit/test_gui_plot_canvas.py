# tests/unit/test_gui_plot_canvas.py
"""A plot that can still be read when it is short.

The sensor plots are embedded (Squid puts them in a column under its
instrument controls), where they get a fraction of the height the standalone
tab gives them. Two things went wrong there, and neither showed at full
height: matplotlib's default layout gives the title and the x axis a fixed
*fraction* of the figure, so below about 390 px the "Seconds Ago" label was
cut off; and the canvas declared no minimum height, so a layout short of room
squeezed it to a sliver rather than take the room from something that could
scroll. These pin both, on the real canvas under Qt's offscreen platform.
"""

import pytest

from matplotlib.transforms import Bbox
from qtpy.QtWidgets import QVBoxLayout, QWidget

from fluidics.control.flow_sensor import FlowSensorSimulation
from fluidics.qt.sensor_plots import FlowSensorWidget


@pytest.fixture
def flow_plot(qapp):
    """A FlowSensorWidget that has drawn one reading -- the labels exist from
    the first draw on, not from construction."""
    sensor = FlowSensorSimulation(index=1, name="syringe_draw")
    widget = FlowSensorWidget(sensor, draw_protection=True)
    widget._on_reading(500.0, 1000.0)
    yield widget
    widget.deleteLater()
    sensor.close()


def shown_at(qapp, widget, height):
    """The widget's canvas at exactly this height, the way Qt would give it:
    the figure follows the canvas in resizeEvent, which is only delivered once
    the window holding it is shown -- so the check that it did follow."""
    canvas = widget.canvas
    canvas.setFixedHeight(height)
    widget.resize(800, 1000)
    widget.show()
    qapp.processEvents()
    canvas.draw()
    assert round(canvas.figure.bbox.height / canvas.devicePixelRatioF()) == height
    return canvas


def overflow(canvas):
    """How far what the figure draws reaches past the figure, in pixels
    (top, bottom); nothing is cut off when both are zero.

    From the title's and the two axes' own extents, not Axes.get_tightbbox:
    newer matplotlib (3.10) leaves out of that box the part of a label lying
    outside the figure -- the very thing being measured -- so it reads "fits"
    for a 130 px y label on a 104 px canvas. The per-artist extents say the
    same on 3.5 and 3.10.
    """
    renderer = canvas.get_renderer()
    axes = canvas.figure.axes[0]
    drawn = Bbox.union([axes.title.get_window_extent(renderer),
                        axes.xaxis.get_tightbbox(renderer),
                        axes.yaxis.get_tightbbox(renderer)])
    return (max(0, round(drawn.y1 - canvas.figure.bbox.height)), max(0, round(-drawn.y0)))


def test_the_labels_of_a_short_plot_are_not_cut_off(qapp, flow_plot):
    canvas = shown_at(qapp, flow_plot, 200)
    assert overflow(canvas) == (0, 0)


def test_the_canvas_declares_the_height_its_labels_need(qapp, flow_plot):
    needed = flow_plot.canvas.minimumSizeHint().height()
    assert needed > 0
    canvas = shown_at(qapp, flow_plot, needed)
    assert overflow(canvas) == (0, 0)


def test_the_declared_height_is_the_least_not_a_comfortable_one(qapp, flow_plot):
    """A minimum that is generous takes room from everything sharing the
    column. At three quarters of it, something is already cut off."""
    needed = flow_plot.canvas.minimumSizeHint().height()
    canvas = shown_at(qapp, flow_plot, needed * 3 // 4)
    assert overflow(canvas) != (0, 0)


def test_a_layout_short_of_room_cannot_flatten_the_plot(qapp, flow_plot):
    host = QWidget()
    layout = QVBoxLayout(host)
    layout.addWidget(flow_plot)
    try:
        host.resize(800, 100)      # far less than the widget's controls alone
        host.show()
        qapp.processEvents()
        canvas = flow_plot.canvas
        canvas.draw()
        # Not "at least its own hint": newer matplotlib does declare 10 x 10,
        # and a 10 px plot honours that. What the hint is for is this.
        assert overflow(canvas) == (0, 0)
    finally:
        layout.removeWidget(flow_plot)
        flow_plot.setParent(None)
        host.deleteLater()
