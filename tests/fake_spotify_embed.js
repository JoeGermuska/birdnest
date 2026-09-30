// A stand-in for Spotify's Embed iFrame API (served in place of open.spotify.com/embed/iframe-api/v1), for the
// radio player's embed mode in tests/test_player.py: what listeners who aren't logged in to the site get.
// It behaves as the real one was seen to: loadUri() reloads the embed ('ready' again), playback_update comes about
// once a second with playingURI, and at the end of a track the position stops at the duration *without* pausing.
// Tests drive it through window.FakeEmbed and read back what the page asked it to do.
(function () {
    const F = window.FakeEmbed = {
        loads: [],            // ids the page loaded, in order
        plays: 0,             // play() calls
        loaded: null,         // id loaded into the embed
        track: null,          // id playing
        paused: true,
        position: 0,
        duration: 200000,     // ms; 29713 is what a logged-out listener's 30-second preview reports
        listeners: {},
        frame: null,
        emit(event, data) { (this.listeners[event] || []).forEach(fn => fn({data})); },
        update() {
            if (!this.track) return;
            this.emit('playback_update', {playingURI: 'spotify:track:' + this.track, isPaused: this.paused,
                                          isBuffering: false, duration: this.duration, position: this.position});
        },
        // the track plays out: a last update near the end, then one at the end, still "playing"
        finish() {
            this.position = this.duration - 700; this.update();
            this.position = this.duration; this.update();
        },
    };
    const controller = {
        addListener(event, fn) { (F.listeners[event] = F.listeners[event] || []).push(fn); },
        loadUri(uri) {
            F.loaded = uri.split(':').pop();
            F.loads.push(F.loaded);
            F.frame.src = 'about:blank#' + F.loaded;
            setTimeout(() => F.emit('ready', {}), 20);
        },
        play() {
            F.plays++;
            F.track = F.loaded; F.paused = false; F.position = 0;
            F.emit('playback_started', {playingURI: 'spotify:track:' + F.track});
            F.update();
        },
        resume() { F.paused = false; F.update(); },
        pause() { F.paused = true; F.update(); },
        togglePlay() { F.paused = !F.paused; F.update(); },
        seek(s) { F.position = s * 1000; F.update(); },
        destroy() { F.frame.remove(); },
    };
    const api = {
        createController(el, options, cb) {
            F.frame = document.createElement('iframe');
            F.frame.width = options.width; F.frame.height = options.height;
            el.replaceWith(F.frame);
            cb(controller);
            controller.loadUri(options.uri);
        },
    };
    setTimeout(() => window.onSpotifyIframeApiReady && window.onSpotifyIframeApiReady(api), 10);
})();
