import app as nm


def test_simple_notification(fixture_lines):
    msgs = list(nm.parse_notify_stream(fixture_lines('simple.txt')))
    assert len(msgs) == 1
    args, entry = msgs[0]
    assert args == ['notify-send', 0, 'dialog-information', 'Build finished', 'All 42 tests passed']
    assert entry is None


def test_multiline_body_and_quotes(fixture_lines):
    (args, _), = nm.parse_notify_stream(fixture_lines('multiline.txt'))
    assert args[0] == 'Firefox'
    assert args[3] == 'Reminder: "standup"'
    assert args[4] == 'First line\nsecond line with "quotes"\n\nlast line after a blank'


def test_portal_notification_uses_desktop_entry(fixture_lines, tmp_path, monkeypatch):
    (tmp_path / 'exampleapp_exampleapp.desktop').write_text(
        '[Desktop Entry]\nName=Example App\n\n[Desktop Action new]\nName=New window\n')
    monkeypatch.setattr(nm, 'SNAP_DESKTOP_DIR', str(tmp_path))
    monkeypatch.setattr(nm, '_desktop_names', {})

    msgs = list(nm.parse_notify_stream(fixture_lines('portal_snap.txt')))
    assert len(msgs) == 2
    (args1, entry1), (args2, entry2) = msgs
    assert entry1 == 'snap.exampleapp'
    assert nm.resolve_app_name(args1, entry1) == 'Example App'
    assert entry2 is None
    assert nm.resolve_app_name(args2, entry2) == 'Unknown'


def test_desktop_entry_falls_back_to_id(tmp_path, monkeypatch):
    monkeypatch.setattr(nm, 'SNAP_DESKTOP_DIR', str(tmp_path))
    monkeypatch.setattr(nm, 'DESKTOP_DIRS', [str(tmp_path)])
    monkeypatch.setattr(nm, '_desktop_names', {})
    assert nm.desktop_entry_name('snap.missing') == 'missing'
    assert nm.desktop_entry_name('org.example.Missing') == 'org.example.Missing'


def test_concatenated_stream_yields_all(fixture_lines):
    lines = fixture_lines('simple.txt') + fixture_lines('multiline.txt') + fixture_lines('portal_snap.txt')
    assert [a[3] for a, _ in nm.parse_notify_stream(lines)] == [
        'Build finished', 'Reminder: "standup"', 'New message in #general', 'No hint at all']


def test_save_notification_dedupes_within_2s(client):
    before = len(client.get('/notifications').get_json())
    nm.save_notification('Tester', 'dup', 'same body')
    nm.save_notification('Tester', 'dup', 'same body')
    rows = client.get('/notifications').get_json()
    assert len(rows) == before + 1
    assert rows[0]['app_name'] == 'Tester' and rows[0]['summary'] == 'dup'
