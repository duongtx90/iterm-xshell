from xshell_panels.sessions_window import cg_to_cocoa, dock_frame, screen_for


def test_cg_to_cocoa():
    # Màn chính cao 1080: cửa sổ ở y=30 (từ trên), cao 750 -> đáy cách đáy màn 300
    assert cg_to_cocoa((100, 30, 800, 750), 1080) == (100, 300, 800, 750)


def test_dock_room_on_left():
    frame, need = dock_frame((400, 100, 800, 600), (0, 0, 1920, 1055), 260)
    assert frame == (140, 100, 260, 600) and need == 0


def test_dock_needs_room():
    frame, need = dock_frame((50, 100, 800, 600), (0, 0, 1920, 1055), 260)
    assert frame[0] == 0 and need == 210


def test_dock_fullscreen_window():
    frame, need = dock_frame((0, 0, 2560, 1080), (0, 0, 2560, 1050), 260)
    assert frame == (0, 0, 260, 1050) and need == 260


def test_dock_second_screen_left_of_main():
    screens = [(0, 0, 1920, 1080), (-1440, 0, 1440, 900)]
    win = (-1200, 100, 900, 700)
    assert screen_for(win, screens) == screens[1]
    frame, need = dock_frame(win, (-1440, 0, 1440, 875), 240)
    assert frame == (-1440, 100, 240, 700) and need == 0


def test_dock_clamped_vertically():
    frame, _ = dock_frame((500, -50, 800, 1200), (0, 0, 1920, 1055), 260)
    assert frame[1] == 0 and frame[3] == 1055
