"""Birds radio player (static/radio-player.js) against a fake Spotify (tests/fake_spotify.js).

The player tells Spotify which track to play, one at a time; it must never use Spotify's own queue or its
"next", and the highlighted track in the list must always be the one playing.
"""
import pytest

SHOW = '/playlist/2021-03-04'


def wait(page, ms=400):
    page.wait_for_timeout(ms)


def start_from_show_track(page, server, row=3):
    """Open a show page and press the radio icon on one of its tracks; wait for the first track to play."""
    page.goto(server + SHOW)
    page.wait_for_function('window.FakeSpotify && document.documentElement.classList.contains("player-ready")')
    page.click(f'table.show-tracks tbody tr:nth-child({row}) a.radio-link')
    page.wait_for_function('FakeSpotify.track !== null')
    wait(page)


def live_ids(page):
    return page.eval_on_selector_all('#rp-list li', 'els => els.map(e => e.dataset.spotify)')


def playing_row(page):
    return page.eval_on_selector_all('#rp-list li', 'els => els.findIndex(e => e.classList.contains("is-playing"))')


def fake(page, expr):
    return page.evaluate(f'FakeSpotify.{expr}')


def plays(page):
    return fake(page, 'playsOf()')


def never_used_spotify_queue(page):
    calls = fake(page, 'calls')
    return not any(c['path'] == '/me/player/queue' or c['path'] == 'nextTrack' for c in calls)


def test_radio_link_plays_the_set_in_place(page, server):
    start_from_show_track(page, server)
    ids = live_ids(page)
    assert page.url.endswith(SHOW), 'stays on the page'
    assert len(ids) == 20
    assert plays(page) == [ids[0]]
    assert playing_row(page) == 0
    assert fake(page, 'repeat') == 'off', 'repeat is turned off'
    assert not page.errors


def test_track_end_plays_the_next_one(page, server):
    start_from_show_track(page, server)
    ids = live_ids(page)
    for n in range(1, 4):
        fake(page, 'finish()')
        wait(page)
        assert plays(page)[-1] == ids[n]
        assert playing_row(page) == n
    assert fake(page, 'autoplays') == 0, "Spotify's autoplay never gets to play"
    assert never_used_spotify_queue(page)


def test_next_button_plays_the_next_track_in_the_list(page, server):
    start_from_show_track(page, server)
    ids = live_ids(page)
    page.click('#rp-next')
    wait(page)
    assert plays(page)[-1] == ids[1] and playing_row(page) == 1
    page.click('#rp-next')
    wait(page)
    assert plays(page)[-1] == ids[2] and playing_row(page) == 2
    assert never_used_spotify_queue(page)


def test_changing_adventure_then_next_plays_the_new_list(page, server):
    """The reported bug: change what's coming, then ⏭."""
    start_from_show_track(page, server)
    page.click('#rp-next')
    wait(page)
    before = live_ids(page)
    page.click('#rp-toggle')
    page.fill('#rp-adventure', '90')  # fires input and change, as a real slider does
    page.wait_for_function('document.querySelectorAll("#rp-list li.fresh").length > 0')
    page.wait_for_load_state('networkidle')
    after = live_ids(page)
    assert after[:2] == before[:2], 'what has played and what is playing stay'
    assert after[2:] != before[2:], 'what comes next is re-sequenced'
    assert fake(page, 'track') == before[1], 'the current track keeps playing'
    page.click('#rp-next')
    wait(page)
    assert plays(page)[-1] == after[2]
    assert playing_row(page) == 2
    fake(page, 'finish()')
    wait(page)
    assert plays(page)[-1] == after[3] and playing_row(page) == 3
    assert never_used_spotify_queue(page)
    assert not page.errors


def test_new_set_then_next_plays_the_new_sets_second_track(page, server):
    start_from_show_track(page, server, row=3)
    page.click('#rp-next')
    wait(page)
    old = live_ids(page)
    page.click('table.show-tracks tbody tr:nth-child(10) a.radio-link')
    page.wait_for_function(f'FakeSpotify.track !== {old[1]!r}')
    wait(page)
    new = live_ids(page)
    assert new[0] != old[0]
    assert plays(page)[-1] == new[0] and playing_row(page) == 0
    page.click('#rp-next')
    wait(page)
    assert plays(page)[-1] == new[1] and playing_row(page) == 1
    assert never_used_spotify_queue(page)


def test_another_release_of_the_track_still_counts(page, server):
    """Spotify can play the same recording under another id; the list must keep up and carry on."""
    page.goto(server + SHOW)
    page.wait_for_function('window.FakeSpotify && document.documentElement.classList.contains("player-ready")')
    page.click('table.show-tracks tbody tr:nth-child(3) a.radio-link')
    page.wait_for_function('FakeSpotify.track !== null')
    ids = live_ids(page)
    name = page.eval_on_selector('#rp-list li:nth-child(2)', 'e => e.dataset.name')
    fake(page, f"names[{ids[1]!r}] = {(name + ' - 2011 Remaster')!r}")
    fake(page, f"relink[{ids[1]!r}] = 'OTHERRELEASE'")
    fake(page, 'finish()')
    wait(page)
    assert fake(page, 'track') == ids[1] and playing_row(page) == 1
    fake(page, 'finish()')
    wait(page)
    assert plays(page)[-1] == ids[2] and playing_row(page) == 2


def test_autoplay_is_overridden_at_the_end_of_a_track(page, server):
    """If Spotify's autoplay gets in first (a background tab, say), the set takes over again."""
    start_from_show_track(page, server)
    ids = live_ids(page)
    page.evaluate('() => { const f = FakeSpotify; f.position = f.duration - 500; f.emit(); f.play("AUTOPLAY_X"); }')
    wait(page)
    assert plays(page)[-1] == ids[1] and playing_row(page) == 1


def test_something_else_chosen_in_spotify_is_left_alone(page, server):
    start_from_show_track(page, server)
    n = len(plays(page))
    fake(page, 'external("SOMETHING_ELSE")')
    wait(page)
    assert len(plays(page)) == n, 'the radio does not fight over it'
    assert 'not from Birds radio' in page.inner_text('#rp-now')
    page.click('#rp-next')
    wait(page)
    assert plays(page)[-1] == live_ids(page)[1], '⏭ goes back to the set'


def test_end_of_track_timer_starts_the_next_track(page, server):
    """Just before a track ends, the player starts the next one itself."""
    page.goto(server + SHOW)
    page.wait_for_function('window.FakeSpotify && document.documentElement.classList.contains("player-ready")')
    fake(page, 'duration = 1500')
    page.click('table.show-tracks tbody tr:nth-child(3) a.radio-link')
    page.wait_for_function('FakeSpotify.track !== null')
    ids = live_ids(page)
    page.wait_for_function(f'FakeSpotify.track === {ids[1]!r}', timeout=5000)
    assert playing_row(page) == 1
    assert fake(page, 'autoplays') == 0


def test_the_set_keeps_growing(page, server):
    start_from_show_track(page, server)
    for _ in range(18):
        page.click('#rp-next')
        wait(page, 250)
    page.wait_for_function('document.querySelectorAll("#rp-list li").length > 20')
    assert playing_row(page) == 18


def test_playing_carries_on_across_pages(page, server):
    start_from_show_track(page, server)
    ids = live_ids(page)
    page.evaluate('window.marker = 1')
    for path in ['/genres', '/rankings/artists', '/genres/map', '/artist/doesnotexist']:
        page.evaluate(f'Turbo.visit({path!r})')
        wait(page, 700)
    assert page.evaluate('window.marker === 1'), 'no full page loads'
    assert page.locator('#radio-player').count() == 1
    fake(page, 'finish()')
    wait(page)
    assert plays(page)[-1] == ids[1] and playing_row(page) == 1
    assert not page.errors


def test_queue_toggle(page, server):
    """＋ adds the playing track to the chosen queue playlist; ✓ takes it back out."""
    page.add_init_script("localStorage.setItem('radio-queue', JSON.stringify({id: 'q1', name: 'Next show'}))")
    start_from_show_track(page, server)
    track = fake(page, 'track')
    page.click('#rp-add')
    wait(page)
    assert page.inner_text('#rp-add') == '✓'
    page.click('#rp-add')
    wait(page)
    assert page.inner_text('#rp-add') == '＋'
    calls = [(c['method'], c['path']) for c in fake(page, 'calls') if c['path'].startswith('/playlists/q1')]
    assert ('POST', '/playlists/q1/items') in calls and ('DELETE', '/playlists/q1/items') in calls
    assert track


def test_show_radio_keeps_leaning_as_it_continues(page, server):
    """Radio from a show carries the show along when the set grows or is retuned."""
    page.goto(server + SHOW)
    page.wait_for_function('window.FakeSpotify && document.documentElement.classList.contains("player-ready")')
    bodies = []
    page.on('request', lambda r: bodies.append(r.post_data_json) if r.url.endswith('/radio/more') else None)
    page.click('a.radio-start-link')  # "Birds radio" for this show
    page.wait_for_function('FakeSpotify.track !== null')
    assert 'show of March 4, 2021' in page.eval_on_selector('#rp-from', 'e => e.textContent')
    page.click('#rp-toggle')
    page.click('#rp-reshuffle')
    page.wait_for_load_state('networkidle')
    assert bodies and bodies[-1]['lean'][0] == 'show'
    page.click('#rp-random')
    page.wait_for_load_state('networkidle')
    assert bodies[-1]['lean'] is None, 'a random new start drops the lean'


def open_list(page):
    page.click('#rp-toggle')
    page.wait_for_selector('#rp-list li', state='visible')


def take_out(page, row):
    """Take a track out of the live list with its × (shown on hover)."""
    page.hover(f'#rp-list li:nth-child({row + 1})')
    page.click(f'#rp-list li:nth-child({row + 1}) .remove-pick')
    wait(page)


def swipe(page, row, dx):
    """A touch swipe across a row of the live list: dx < 0 is to the left."""
    page.evaluate('''([row, dx]) => {
        const li = document.querySelectorAll('#rp-list li')[row], r = li.getBoundingClientRect();
        const x = r.left + r.width / 2, y = r.top + r.height / 2;
        const ev = (type, cx) => li.dispatchEvent(new PointerEvent(type,
            {bubbles: true, cancelable: true, pointerType: 'touch', isPrimary: true, pointerId: 7, clientX: cx, clientY: y}));
        ev('pointerdown', x);
        for (let i = 1; i <= 10; i++) ev('pointermove', x + dx * i / 10);
        ev('pointerup', x + dx);
    }''', [row, dx])
    wait(page, 600)


def test_taking_out_a_coming_track(page, server):
    start_from_show_track(page, server)
    open_list(page)
    ids = live_ids(page)
    take_out(page, 2)
    assert live_ids(page) == ids[:2] + ids[3:]
    assert playing_row(page) == 0 and fake(page, 'track') == ids[0], 'what is playing carries on'
    fake(page, 'finish()')
    wait(page)
    fake(page, 'finish()')
    wait(page)
    assert plays(page)[-2:] == [ids[1], ids[3]], 'the taken-out track is skipped'
    assert playing_row(page) == 2
    assert not page.errors


def test_taking_out_a_played_track_keeps_the_place(page, server):
    """Rows before the playing one shift up; the highlight and the end-of-track timer follow."""
    page.goto(server + SHOW)
    page.wait_for_function('window.FakeSpotify && document.documentElement.classList.contains("player-ready")')
    fake(page, 'duration = 2500')
    page.click('table.show-tracks tbody tr:nth-child(3) a.radio-link')
    page.wait_for_function('FakeSpotify.track !== null')
    open_list(page)
    ids = live_ids(page)
    page.click('#rp-next')
    wait(page, 300)
    take_out(page, 0)
    assert live_ids(page) == ids[1:]
    assert playing_row(page) == 0 and fake(page, 'track') == ids[1]
    page.wait_for_function(f'FakeSpotify.track === {ids[2]!r}', timeout=5000)
    assert playing_row(page) == 1
    assert not page.errors


def test_taking_out_the_playing_track_plays_the_next(page, server):
    start_from_show_track(page, server)
    open_list(page)
    ids = live_ids(page)
    take_out(page, 0)
    assert live_ids(page) == ids[1:]
    assert plays(page)[-1] == ids[1] and playing_row(page) == 0
    assert never_used_spotify_queue(page)


def test_taken_out_tracks_do_not_come_back(page, server):
    start_from_show_track(page, server)
    open_list(page)
    bodies = []
    page.on('request', lambda r: bodies.append(r.post_data_json) if r.url.endswith('/radio/more') else None)
    gone = live_ids(page)[4]
    take_out(page, 4)
    page.click('#rp-reshuffle')
    page.wait_for_load_state('networkidle')
    assert gone in bodies[-1]['played']
    assert gone not in live_ids(page)


def test_swipe_left_takes_a_track_out(page, server):
    start_from_show_track(page, server)
    open_list(page)
    ids = live_ids(page)
    swipe(page, 2, -30)  # too short: nothing happens
    assert live_ids(page) == ids
    swipe(page, 2, -300)
    assert live_ids(page) == ids[:2] + ids[3:]
    assert fake(page, 'track') == ids[0]
    assert not page.locator('.swipe-hint').count()
    assert not page.errors


def test_swipe_right_starts_radio_from_that_track(page, server):
    start_from_show_track(page, server)
    open_list(page)
    ids = live_ids(page)
    swipe(page, 3, 300)
    page.wait_for_function(f'FakeSpotify.track === {ids[3]!r}')
    wait(page)
    assert live_ids(page)[0] == ids[3] and playing_row(page) == 0
    assert len(live_ids(page)) == 20, 'a new set'


# ---- embed mode: listeners who aren't logged in play through Spotify's embed (tests/fake_spotify_embed.js) ----

def embed(page, expr):
    return page.evaluate(f'FakeEmbed.{expr}')


def start_embed_from_show_track(page, server, row=3):
    page.goto(server + SHOW)
    page.wait_for_function('window.FakeEmbed && document.documentElement.classList.contains("player-ready")')
    page.click(f'table.show-tracks tbody tr:nth-child({row}) a.radio-link')
    page.wait_for_function('FakeEmbed.track !== null')
    wait(page)


def no_web_api(page):
    """Embed mode stays off the Web API and the Web Playback SDK altogether."""
    return not [u for u in page.outside if 'api.spotify.com' in u or 'sdk.scdn.co' in u] \
        and not page.evaluate('!!window.FakeSpotify')


def test_embed_radio_link_plays_the_set_in_place(guest_page, server):
    page = guest_page
    start_embed_from_show_track(page, server)
    ids = live_ids(page)
    assert page.url.endswith(SHOW)
    assert len(ids) == 20
    assert embed(page, 'loads') == [ids[0]] and embed(page, 'track') == ids[0]
    assert playing_row(page) == 0
    assert page.locator('#rp-embed-box iframe').is_visible(), 'the embed is in the bar, where it can be seen'
    assert page.locator('#rp-like, #rp-add, #rp-queue').count() == 0, 'no ♥, ＋ or queue: they need the Web API'
    assert no_web_api(page)
    assert not page.errors


def test_embed_track_end_plays_the_next_one(guest_page, server):
    """The embed doesn't pause at the end of a track; it just stops at the end."""
    page = guest_page
    start_embed_from_show_track(page, server)
    ids = live_ids(page)
    for n in range(1, 4):
        embed(page, 'finish()')
        wait(page)
        assert embed(page, 'loads')[-1] == ids[n] and embed(page, 'track') == ids[n]
        assert playing_row(page) == n
    assert len(embed(page, 'loads')) == 4, 'one load per track, no double skips'


def test_embed_end_of_track_timer_starts_the_next_track(guest_page, server):
    page = guest_page
    page.goto(server + SHOW)
    page.wait_for_function('window.FakeEmbed && document.documentElement.classList.contains("player-ready")')
    embed(page, 'duration = 1500')
    page.click('table.show-tracks tbody tr:nth-child(3) a.radio-link')
    page.wait_for_function('FakeEmbed.track !== null')
    ids = live_ids(page)
    page.wait_for_function(f'FakeEmbed.track === {ids[1]!r}', timeout=5000)
    assert playing_row(page) == 1


def test_embed_next_and_pause(guest_page, server):
    page = guest_page
    start_embed_from_show_track(page, server)
    ids = live_ids(page)
    page.click('#rp-next')
    wait(page)
    assert embed(page, 'track') == ids[1] and playing_row(page) == 1
    page.click('#rp-play')
    wait(page)
    assert embed(page, 'paused') and page.inner_text('#rp-play') == '▶'
    page.click('#rp-play')
    wait(page)
    assert not embed(page, 'paused') and embed(page, 'track') == ids[1]


def test_embed_stale_updates_from_the_last_track_are_ignored(guest_page, server):
    """An update from the track before, arriving late, mustn't move the list back or skip ahead."""
    page = guest_page
    start_embed_from_show_track(page, server)
    ids = live_ids(page)
    page.click('#rp-next')
    wait(page)
    page.evaluate(f'''() => FakeEmbed.emit('playback_update', {{playingURI: 'spotify:track:{ids[0]}', isPaused: false,
                     isBuffering: false, duration: FakeEmbed.duration, position: FakeEmbed.duration}})''')
    wait(page)
    assert playing_row(page) == 1 and embed(page, 'track') == ids[1]
    assert len(embed(page, 'loads')) == 2


def test_embed_says_when_it_plays_previews(guest_page, server):
    page = guest_page
    page.goto(server + SHOW)
    page.wait_for_function('window.FakeEmbed && document.documentElement.classList.contains("player-ready")')
    embed(page, 'duration = 29713')
    page.click('table.show-tracks tbody tr:nth-child(3) a.radio-link')
    page.wait_for_function('FakeEmbed.track !== null')
    wait(page)
    assert 'preview' in page.inner_text('#rp-now').lower()


def test_embed_carries_on_across_pages(guest_page, server):
    page = guest_page
    start_embed_from_show_track(page, server)
    ids = live_ids(page)
    page.evaluate('FakeEmbed.frame.dataset.marker = "1"')
    for path in ['/genres', '/rankings/artists', '/artist/doesnotexist', SHOW]:
        page.evaluate(f'Turbo.visit({path!r})')
        wait(page, 700)
    assert page.locator('#radio-player').count() == 1
    assert page.evaluate('FakeEmbed.frame.isConnected && FakeEmbed.frame.dataset.marker === "1"'), 'the same embed, never reloaded'
    embed(page, 'finish()')
    wait(page)
    assert embed(page, 'track') == ids[1] and playing_row(page) == 1
    assert not page.errors


def test_embed_radio_page_play_this_set(guest_page, server):
    page = guest_page
    page.goto(server + '/radio?seed=5')
    page.wait_for_function('window.FakeEmbed && document.documentElement.classList.contains("player-ready")')
    ids = page.eval_on_selector_all('#radio-list li', 'els => els.map(e => e.dataset.spotify)')
    page.click('#radio-play-set')
    page.wait_for_function('FakeEmbed.track !== null')
    assert embed(page, 'track') == ids[0]
    assert live_ids(page) == ids


def test_logged_in_listeners_keep_the_full_player(page, server):
    start_from_show_track(page, server)
    assert page.locator('#rp-embed-box').count() == 0
    assert not page.evaluate('!!window.FakeEmbed')


def embed_shown(page):
    return page.evaluate("(() => { const b = document.getElementById('rp-embed-box'); return !b.hidden && b.offsetHeight > 40; })()")


def test_embed_folds_away_when_paused_and_can_be_hidden(guest_page, server):
    page = guest_page
    page.goto(server + SHOW)
    page.wait_for_function('window.FakeEmbed && document.documentElement.classList.contains("player-ready")')
    assert not embed_shown(page), 'nothing playing yet: no embed'
    assert not page.locator('#rp-embed-toggle').is_visible()
    page.click('table.show-tracks tbody tr:nth-child(3) a.radio-link')
    page.wait_for_function('FakeEmbed.track !== null')
    wait(page)
    assert embed_shown(page)
    page.click('#rp-play')  # pause
    wait(page)
    assert not embed_shown(page), 'paused: folded away'
    page.click('#rp-play')
    wait(page)
    assert embed_shown(page)
    page.click('#rp-embed-toggle')  # hide it while it plays
    wait(page)
    assert not embed_shown(page)
    assert page.evaluate('FakeEmbed.frame.isConnected'), 'hidden, not removed: the music carries on'
    embed(page, 'finish()')
    wait(page)
    assert not embed_shown(page), 'stays hidden for the next track'
    page.click('#rp-embed-toggle')
    wait(page)
    assert embed_shown(page)
    assert not page.errors


def test_embed_mode_explains_the_login(guest_page, server):
    page = guest_page
    page.goto(server + SHOW)
    page.click('#rp-toggle')
    note = page.inner_text('#rp-drawer')
    assert 'five' in note and 'Log in with Spotify' in note
    assert not page.locator('#rp-scroll').is_visible(), 'no set yet: no empty list box'
