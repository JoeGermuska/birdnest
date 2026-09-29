// A stand-in for Spotify in the browser, for tests/test_player.py: the Web API (by wrapping fetch for
// api.spotify.com) and the Web Playback SDK (window.Spotify.Player, served in place of sdk.scdn.co's script).
// Tests drive it through window.FakeSpotify: end the current track, let "autoplay" step in, play another
// release of a track under a different id, and so on, and read back the API calls the page made.
(function () {
    const F = window.FakeSpotify = {
        calls: [],            // [{method, path, body}] every Web API call the page made
        track: null,          // id of what's playing
        paused: true,
        position: 0,
        duration: 200000,     // ms; tests can shorten it to exercise the end-of-track timer
        repeat: 'off',
        relink: {},           // requested id -> id Spotify reports instead (another release of the recording)
        names: {},            // id -> track name (defaults to "Track <id>")
        autoplay: true,       // after a track ends with nothing to follow, Spotify plays something of its own
        autoplays: 0,
        listeners: {},
        emit() {
            const l = this.listeners.player_state_changed;
            if (!l || !this.track) return;
            const shown = this.relink[this.track] || this.track;
            l({paused: this.paused, position: this.position, duration: this.duration,
               repeat_mode: this.repeat === 'off' ? 0 : 1,
               track_window: {current_track: {id: shown, name: this.names[this.track] || ('Track ' + this.track),
                                              artists: [{name: 'An Artist'}], linked_from: null}}});
        },
        play(id) {
            this.track = id; this.paused = false; this.position = 0;
            this.emit();
        },
        // the current track plays out: a last event near the end, then Spotify stops at 0 (and, with autoplay on
        // and nothing else asked for, soon plays something of its own)
        finish() {
            const ended = this.track;
            this.position = this.duration - 500; this.emit();
            this.paused = true; this.position = 0; this.emit();
            if (this.autoplay) setTimeout(() => { if (this.track === ended && this.paused) this.play('AUTOPLAY' + (++this.autoplays)); }, 150);
        },
        // someone plays something else in their Spotify app mid-track
        external(id) { this.position = 30000; this.emit(); this.play(id); },
        playsOf() { return this.calls.filter(c => c.method === 'PUT' && c.path === '/me/player/play').map(c => c.body.uris[0].split(':').pop()); },
    };

    const json = (status, body) => new Response(body === undefined ? null : JSON.stringify(body),
                                                  {status, headers: {'Content-Type': 'application/json'}});
    const realFetch = window.fetch.bind(window);
    window.fetch = async (input, init = {}) => {
        const url = String(input && input.url || input);
        if (!url.startsWith('https://api.spotify.com/v1')) return realFetch(input, init);
        const u = new URL(url), path = u.pathname.replace('/v1', ''), method = (init.method || 'GET').toUpperCase();
        const body = init.body ? JSON.parse(init.body) : null;
        F.calls.push({method, path, body, query: u.search});
        if (path === '/me/player/play') { setTimeout(() => F.play(body.uris[0].split(':').pop()), 30); return json(204); }
        if (path === '/me/player/repeat') { F.repeat = u.searchParams.get('state'); return json(204); }
        if (path === '/me/tracks/contains') return json(200, [false]);
        if (path === '/me') return json(200, {id: 'listener'});
        if (path === '/me/playlists') return json(200, {items: [], next: null});
        if (path.endsWith('/items') && method === 'GET') return json(200, {items: [], next: null});
        return json(204);
    };

    window.Spotify = {Player: class {
        constructor(options) { this.options = options; }
        addListener(event, fn) { F.listeners[event] = fn; }
        connect() { setTimeout(() => F.listeners.ready && F.listeners.ready({device_id: 'test-device'}), 20); return Promise.resolve(true); }
        disconnect() {}
        activateElement() { return Promise.resolve(); }
        getCurrentState() { return Promise.resolve(null); }
        togglePlay() { F.paused = !F.paused; F.emit(); return Promise.resolve(); }
        pause() { F.paused = true; F.emit(); return Promise.resolve(); }
        nextTrack() { F.calls.push({method: 'SDK', path: 'nextTrack'}); return Promise.resolve(); }
    }};
    setTimeout(() => window.onSpotifyWebPlaybackSDKReady && window.onSpotifyWebPlaybackSDKReady(), 10);
})();
