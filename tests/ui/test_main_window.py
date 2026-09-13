from ui.bridge import EngineBridge
from ui.demo import make_demo_engine
from ui.main_window import MainWindow


def test_live_updates_light_up_the_diagram(qtbot):
    engine = make_demo_engine("targets/f411")
    bridge = EngineBridge(engine)
    win = MainWindow(engine, bridge)
    qtbot.addWidget(win)
    win.show()
    engine.start()
    try:
        qtbot.waitUntil(lambda: len(win.diagram_state.active_edges) > 0,
                        timeout=4000)
        assert win.rate_label.text() != ""
    finally:
        engine.stop()


def test_run_stop_holds_data_path_display_on_data_path_tab(qtbot):
    """Per-page run/stop (spec point 1): toggling run_stop_act while
    the Data Path tab is current holds the diagram+Inspector display
    (self._datapath_stopped) exactly like the old global Freeze did -
    and the action's own label switches Stop<->Run."""
    engine = make_demo_engine("targets/f411")
    bridge = EngineBridge(engine)
    win = MainWindow(engine, bridge)
    qtbot.addWidget(win)
    win.show()
    engine.start()
    try:
        qtbot.waitUntil(lambda: win.last_update is not None, timeout=4000)
        assert win.tabs.currentIndex() == 0
        assert win.run_stop_act.text() == "Stop"

        win.run_stop_act.setChecked(True)
        assert win.run_stop_act.text() == "Run"
        assert win._datapath_stopped
        held = win.rate_label.text()
        qtbot.wait(300)
        assert win.rate_label.text() == held      # held display

        win.run_stop_act.setChecked(False)
        assert win.run_stop_act.text() == "Stop"
        assert not win._datapath_stopped
    finally:
        engine.stop()


def test_run_stop_acts_on_scope_tab_independently_of_data_path(qtbot):
    """The same toolbar action, while the Scope tab is current, must
    only set scope_page's own independent stop flag - Data Path's own
    flag (and vice versa) stays untouched, and the action's checked/
    text state follows whichever tab is current across a switch."""
    engine = make_demo_engine("targets/f411")
    bridge = EngineBridge(engine)
    win = MainWindow(engine, bridge)
    qtbot.addWidget(win)
    win.show()
    win.tabs.setCurrentIndex(1)             # switch to Scope
    assert win.tabs.currentIndex() == 1
    assert not win.run_stop_act.isChecked()

    win.run_stop_act.setChecked(True)
    assert win.scope_page.is_stopped()
    assert not win._datapath_stopped
    assert win.run_stop_act.text() == "Run"

    # switching back to Data Path must reflect ITS OWN (still
    # running) flag, not Scope's stopped one.
    win.tabs.setCurrentIndex(0)
    assert win.tabs.currentIndex() == 0
    assert not win.run_stop_act.isChecked()
    assert win.run_stop_act.text() == "Stop"

    # and switching back to Scope must show it still stopped.
    win.tabs.setCurrentIndex(1)
    assert win.run_stop_act.isChecked()
    assert win.run_stop_act.text() == "Run"

    win.run_stop_act.setChecked(False)
    assert not win.scope_page.is_stopped()


def test_scope_big_button_syncs_toolbar_while_scope_tab_active(qtbot):
    """spec point 1: the scope page's own big Run/Stop button (and,
    transitively, spacebar) is synced with the toolbar action while
    Scope is the active tab."""
    engine = make_demo_engine("targets/f411")
    bridge = EngineBridge(engine)
    win = MainWindow(engine, bridge)
    qtbot.addWidget(win)
    win.show()
    win.tabs.setCurrentIndex(1)

    win.scope_page.run_stop_btn.click()
    assert win.scope_page.is_stopped()
    assert win.run_stop_act.isChecked()
    assert win.run_stop_act.text() == "Run"

    win.scope_page.run_stop_btn.click()
    assert not win.scope_page.is_stopped()
    assert not win.run_stop_act.isChecked()
    assert win.run_stop_act.text() == "Stop"


def test_target_lost_stops_animating_and_recovers(qtbot):
    """FIX 3 regression: on_state() must not keep implying liveness
    once the poller reports anything other than "running" - active
    edges and progress text are cleared and the rate label goes back
    to the "--" placeholder, rather than continuing to show the last
    live frame of a now-dead target. Recovery is a plain
    apply_update() replay, same as any other poll tick."""
    engine = make_demo_engine("targets/f411")
    bridge = EngineBridge(engine)
    win = MainWindow(engine, bridge)
    qtbot.addWidget(win)
    win.show()
    engine.start()
    try:
        qtbot.waitUntil(lambda: len(win.diagram_state.active_edges) > 0,
                        timeout=4000)
        last_update = win.last_update
        assert last_update is not None

        win.on_state("target_lost")

        assert win.diagram_state.active_edges == set()
        assert "--" in win.rate_label.text()

        win.apply_update(last_update)

        assert len(win.diagram_state.active_edges) > 0
    finally:
        engine.stop()


def test_tabs_switch_inspector_visibility(qtbot):
    """UX rework: the central widget is a Data Path/Scope QTabWidget
    (replacing the old diagram-plus-docked-scope stack) - the
    Inspector dock must be visible only on the Data Path tab (a
    full-page Scope has no room for it) while the Event log dock stays
    visible on both tabs. The tab bar is the only switch (the old
    toolbar Scope shortcut was removed as a redundant second one)."""
    engine = make_demo_engine("targets/f411")
    bridge = EngineBridge(engine)
    win = MainWindow(engine, bridge)
    qtbot.addWidget(win)
    win.show()

    assert win.tabs.currentIndex() == 0
    assert win.inspector_dock.isVisible()
    assert win.log_dock.isVisible()

    win.tabs.setCurrentIndex(1)
    assert win.tabs.currentIndex() == 1
    assert not win.inspector_dock.isVisible()
    assert win.log_dock.isVisible()
    # lazy construction: the Scope tab's page is real now.
    assert win.scope_page is not None
    assert win.scope_page.isVisible()

    win.tabs.setCurrentIndex(0)
    assert win.tabs.currentIndex() == 0
    assert win.inspector_dock.isVisible()
    assert win.log_dock.isVisible()
    # the page persists across tabs away/back - not destroyed, just
    # hidden (its own hideEvent/showEvent, unchanged, handle pausing
    # its repaint timer - see test_scope_page.py's
    # test_repaint_timer_stops_when_hidden for that half).
    assert win.scope_page is not None
    assert not win.scope_page.isVisible()
