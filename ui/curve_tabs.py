from __future__ import annotations

from .components import CurvePlot
from .curve_profiles import load_cached_base_curve_points
from .curve_profiles import profile_base_curve_points
from .curve_profiles import profile_curve_legend_label
from .curve_profiles import profile_curve_points
from .curve_profiles import profile_curve_tab_key
from .curve_profiles import profile_curve_tab_label
from .curve_profiles import profile_curve_target_point
from .curve_profiles import save_cached_base_curve_points


class CurveTabs:
    def __init__(
        self,
        *,
        QtWidgets,
        pg,
        tabs,
        fixed_tab_count: int,
        show_error,
    ):
        self.QtWidgets = QtWidgets
        self.pg = pg
        self.tabs = tabs
        self.fixed_tab_count = int(fixed_tab_count)
        self.show_error = show_error
        self.base_curve_points = load_cached_base_curve_points()
        self.profile_curve_widgets: dict[str, object] = {}
        self.fixed_tab_widgets = [
            tabs.widget(index) for index in range(min(self.fixed_tab_count, tabs.count()))
        ]
        self.sync_close_buttons()

    def set_base_points(self, points: list[tuple[float, float]]) -> None:
        self.base_curve_points = list(points)
        save_cached_base_curve_points(points)

    def open_profile(self, profile: dict) -> None:
        points = profile_curve_points(profile)
        if not points:
            self.show_error(
                "VF Curve",
                "No curve points are available for this profile.",
            )
            return
        key = profile_curve_tab_key(profile)
        widget = self.profile_curve_widgets.get(key)
        if widget is not None:
            index = self.tabs.indexOf(widget)
            if index >= 0:
                self.tabs.setCurrentIndex(index)
                return
            self.profile_curve_widgets.pop(key, None)
        plot = self._new_profile_plot(profile, points)
        # Cache opened profile plots so repeated opens return to the same tab.
        self.profile_curve_widgets[key] = plot.widget
        index = self.tabs.addTab(plot.widget, profile_curve_tab_label(profile))
        self.tabs.setCurrentIndex(index)
        self.sync_close_buttons()

    def close_tab(self, index: int) -> None:
        widget = self.tabs.widget(int(index))
        if widget is None or self._is_fixed_tab_widget(widget):
            self.sync_close_buttons()
            return
        self.tabs.removeTab(int(index))
        for key, curve_widget in list(self.profile_curve_widgets.items()):
            if curve_widget is widget:
                self.profile_curve_widgets.pop(key, None)
        if hasattr(widget, "deleteLater"):
            widget.deleteLater()
        self.sync_close_buttons()

    def sync_close_buttons(self) -> None:
        tab_bar = self.tabs.tabBar()
        for index in range(self.tabs.count()):
            if not self._is_fixed_tab_widget(self.tabs.widget(index)):
                continue
            for position in self._tab_button_positions():
                tab_bar.setTabButton(index, position, None)

    def _new_profile_plot(self, profile: dict, points: list[tuple[float, float]]):
        plot = CurvePlot(
            QtWidgets=self.QtWidgets,
            pg=self.pg,
            x_label="Voltage",
            x_units="mV",
            y_label="Clock",
            y_units="MHz",
            candidate_name=profile_curve_legend_label(profile),
        )
        base_points = profile_base_curve_points(profile) or self.base_curve_points
        if base_points:
            plot.set_source_points(base_points)
        plot.set_candidate_points(points, remember_previous=False)
        target = profile_curve_target_point(profile)
        if target is not None:
            plot.set_selected_point(target[0], target[1])
        return plot

    def _is_fixed_tab_widget(self, widget) -> bool:
        return any(widget is fixed_widget for fixed_widget in self.fixed_tab_widgets)

    def _tab_button_positions(self) -> list:
        position_enum = getattr(
            self.QtWidgets.QTabBar,
            "ButtonPosition",
            self.QtWidgets.QTabBar,
        )
        return [
            position
            for name in ("LeftSide", "RightSide")
            if (position := getattr(position_enum, name, None)) is not None
        ]
