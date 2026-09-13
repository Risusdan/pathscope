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


def test_freeze_holds_display(qtbot):
    engine = make_demo_engine("targets/f411")
    bridge = EngineBridge(engine)
    win = MainWindow(engine, bridge)
    qtbot.addWidget(win)
    win.show()
    engine.start()
    try:
        qtbot.waitUntil(lambda: win.last_update is not None, timeout=4000)
        win.freeze_act.setChecked(True)
        held = win.rate_label.text()
        qtbot.wait(300)
        assert win.rate_label.text() == held      # frozen display
        win.freeze_act.setChecked(False)
    finally:
        engine.stop()


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
    visible on both tabs. The toolbar's Scope action is a tab
    shortcut: its checked state must mirror the active tab in both
    directions - clicking it switches tabs, and switching tabs (as
    tested here via the same setChecked() entry point) updates it."""
    engine = make_demo_engine("targets/f411")
    bridge = EngineBridge(engine)
    win = MainWindow(engine, bridge)
    qtbot.addWidget(win)
    win.show()

    assert win.tabs.currentIndex() == 0
    assert win.inspector_dock.isVisible()
    assert win.log_dock.isVisible()
    assert not win.scope_act.isChecked()

    win.scope_act.setChecked(True)
    assert win.tabs.currentIndex() == 1
    assert not win.inspector_dock.isVisible()
    assert win.log_dock.isVisible()
    assert win.scope_act.isChecked()
    # lazy construction: the Scope tab's page is real now.
    assert win.scope_page is not None
    assert win.scope_page.isVisible()

    win.scope_act.setChecked(False)
    assert win.tabs.currentIndex() == 0
    assert win.inspector_dock.isVisible()
    assert win.log_dock.isVisible()
    assert not win.scope_act.isChecked()
    # the page persists across tabs away/back - not destroyed, just
    # hidden (its own hideEvent/showEvent, unchanged, handle pausing
    # its repaint timer - see test_scope_page.py's
    # test_repaint_timer_stops_when_hidden for that half).
    assert win.scope_page is not None
    assert not win.scope_page.isVisible()

    # a plain tab-bar click (not the toolbar action) must sync the
    # action's checked state the other way too.
    win.tabs.setCurrentIndex(1)
    assert win.scope_act.isChecked()
    win.tabs.setCurrentIndex(0)
    assert not win.scope_act.isChecked()
