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
    """Per-page run/stop (spec point 1): the Data Path page's OWN
    Run/Stop button holds the diagram+Inspector display
    (self._datapath_stopped) exactly like the old global Freeze did -
    and the button's own label switches Stop<->Run."""
    engine = make_demo_engine("targets/f411")
    bridge = EngineBridge(engine)
    win = MainWindow(engine, bridge)
    qtbot.addWidget(win)
    win.show()
    engine.start()
    try:
        qtbot.waitUntil(lambda: win.last_update is not None, timeout=4000)
        assert win.tabs.currentIndex() == 0
        assert win.dp_run_stop_btn.text() == "Stop"

        win.dp_run_stop_btn.click()
        assert win.dp_run_stop_btn.text() == "Run"
        assert win.dp_run_stop_btn.isChecked()
        assert win._datapath_stopped
        held = win.rate_label.text()
        qtbot.wait(300)
        assert win.rate_label.text() == held      # held display

        win.dp_run_stop_btn.click()
        assert win.dp_run_stop_btn.text() == "Stop"
        assert not win._datapath_stopped
    finally:
        engine.stop()


def test_run_stop_acts_on_scope_tab_independently_of_data_path(qtbot):
    """Each page's own Run/Stop button sets only that page's flag -
    stopping Scope leaves Data Path's flag and button untouched (and
    vice versa), across page switches."""
    engine = make_demo_engine("targets/f411")
    bridge = EngineBridge(engine)
    win = MainWindow(engine, bridge)
    qtbot.addWidget(win)
    win.show()
    win.tabs.setCurrentIndex(1)             # switch to Scope
    assert win.tabs.currentIndex() == 1
    assert not win.scope_page.is_stopped()

    win.scope_page.run_stop_btn.click()
    assert win.scope_page.is_stopped()
    assert not win._datapath_stopped
    assert win.dp_run_stop_btn.text() == "Stop"
    assert not win.dp_run_stop_btn.isChecked()

    # switching back to Data Path: ITS OWN (still running) button is
    # untouched by Scope's stopped state.
    win.tabs.setCurrentIndex(0)
    assert win.tabs.currentIndex() == 0
    assert not win.dp_run_stop_btn.isChecked()
    assert win.dp_run_stop_btn.text() == "Stop"

    # and switching back to Scope shows it still stopped.
    win.tabs.setCurrentIndex(1)
    assert win.scope_page.is_stopped()
    assert win.scope_page.run_stop_btn.text() == "Run"

    win.scope_page.run_stop_btn.click()
    assert not win.scope_page.is_stopped()


def test_page_switch_buttons_mirror_current_page(qtbot):
    """The toolbar's Data Path/Scope buttons are the only page
    switch: clicking one switches the stack, and a programmatic
    switch mirrors back into their checked state."""
    engine = make_demo_engine("targets/f411")
    bridge = EngineBridge(engine)
    win = MainWindow(engine, bridge)
    qtbot.addWidget(win)
    win.show()
    assert win.datapath_page_btn.isChecked()
    assert not win.scope_page_btn.isChecked()

    win.scope_page_btn.click()
    assert win.tabs.currentIndex() == 1
    assert win.scope_page is not None
    assert win.scope_page_btn.isChecked()
    assert not win.datapath_page_btn.isChecked()

    win.datapath_page_btn.click()
    assert win.tabs.currentIndex() == 0

    # programmatic switch mirrors back into the buttons.
    win.tabs.setCurrentIndex(1)
    assert win.scope_page_btn.isChecked()
    win.tabs.setCurrentIndex(0)
    assert win.datapath_page_btn.isChecked()


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
