"""Fixed rows of literal Rich Text, without Group measurement and text wrapping."""
from rich.segment import Segment
from textual.geometry import Region
from textual.strip import Strip
from textual.widgets import Static


class IdlePanel(Static):
    def __init__(self, content, **kwargs):
        super().__init__(content, **kwargs)
        self._frame = content

    @property
    def content(self):
        return self._frame

    def render(self):
        return self._frame

    def update(self, content, *, layout=True):
        previous = getattr(self._frame, "renderables", ())
        lines = content.renderables
        self._frame = content
        if layout or len(previous) != len(lines):
            self.refresh(layout=True)
        else:
            # Rich Text equality ignores the base style, so compare it too.
            changed = [Region(0, y, self.size.width, 1) for y, (old, new) in enumerate(zip(previous, lines))
                       if old != new or old.style != new.style]
            if changed:
                self.refresh(*changed)

    def get_content_width(self, container, viewport):
        return max((line.cell_len for line in getattr(self.content, "renderables", ())), default=0)

    def get_content_height(self, container, viewport, width):
        return len(getattr(self.content, "renderables", ()))

    def render_line(self, y):
        lines = getattr(self.content, "renderables", ())
        if y >= len(lines):
            return Strip.blank(self.size.width)
        line = lines[y]
        # idle._row emits ordered, disjoint spans covering the entire picture row.
        # Status/compact rows have only a base style. No general span sorting or wrapping is needed.
        value = line.plain
        segments = ([Segment(value[span.start:span.end], span.style) for span in line.spans]
                    if line.spans else [Segment(value)])
        style = self.visual_style.rich_style + self.app.console.get_style(line.style)
        return Strip(segments, line.cell_len).apply_style(style).adjust_cell_length(
            self.size.width, self.visual_style.rich_style)
