// Birds radio player. Loaded once in <head>; Turbo Drive swaps pages underneath without reloading, so this
// script's state and the #radio-player bar (data-turbo-permanent) survive moving around the site, and the
// music keeps playing. Uses Spotify's Web Playback SDK, which needs Spotify Premium.
//
// Spotify is handed one track at a time (play, then queue the next as each starts), so changing what's
// coming never touches what's playing. The set is kept here, not in the page; /radio draws it when shown.
(function () {
    const S = {
        player: null, deviceId: null, token: null, tokenAt: 0, sdkRequested: false,
        queue: [],      // [{id, html, ms}] in play order
        sent: -1,       // index of the last track Spotify knows about
        current: -1,    // index of what's playing
        stale: new Set(),  // tracks queued on Spotify that are no longer in the set; skipped if they come up
        active: false, extending: false, queueing: false, browsing: false,
        settings: {adventure: 30, n: 20},
    };
    const $ = id => document.getElementById(id);
    const uri = id => 'spotify:track:' + id;
    const here = () => location.pathname + location.search;
    const loginLink = () => `/spotify/login?next=${encodeURIComponent(here())}`;
    const status = html => { const el = $('rp-now'); if (el) el.innerHTML = html; };
    const esc = s => String(s).replace(/[&<>"]/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;'}[c]));

    // ---- Spotify ----
    async function getToken(fresh) {
        if (!fresh && S.token && Date.now() - S.tokenAt < 20 * 60 * 1000) return S.token;
        const resp = await fetch('/spotify/token');
        if (!resp.ok) {
            status(`Your Spotify login expired. <a href="${loginLink()}" data-turbo="false">Log in again</a>`);
            throw new Error('no token');
        }
        S.token = (await resp.json()).access_token;
        S.tokenAt = Date.now();
        return S.token;
    }
    async function api(method, path, body) {
        const resp = await fetch('https://api.spotify.com/v1' + path, {method,
            headers: {'Authorization': 'Bearer ' + await getToken(), 'Content-Type': 'application/json'},
            body: body ? JSON.stringify(body) : undefined});
        if (!resp.ok) throw Object.assign(new Error(`Spotify said ${resp.status}`), {status: resp.status});
    }
    // play on this page's device; Spotify answers 404 until the device has been made the active one
    async function playOnDevice(body) {
        const play = () => api('PUT', `/me/player/play?device_id=${S.deviceId}`, body);
        try {
            await play();
        } catch (e) {
            if (e.status !== 404) throw e;
            await api('PUT', '/me/player', {device_ids: [S.deviceId], play: false});
            await new Promise(r => setTimeout(r, 800));
            await play();
        }
    }

    // ---- the list on /radio, when it's the current page ----
    const rows = () => [...document.querySelectorAll('.radio-set li')];
    function fromPage() {
        return rows().map(li => {
            const copy = li.cloneNode(true);
            copy.classList.remove('is-playing', 'is-played', 'fresh');
            return {id: li.dataset.spotify, ms: +li.dataset.ms || 0, html: copy.outerHTML};
        });
    }
    function totals() {
        const total = $('radio-total');
        if (total) {
            const min = Math.round(S.queue.reduce((t, p) => t + p.ms, 0) / 60000);
            total.textContent = `${S.queue.length} tracks · about ${Math.floor(min / 60)}h ${String(min % 60).padStart(2, '0')}m`;
        }
        const save = document.querySelector('.radio-save input[name=tracks]');
        if (save) save.value = S.queue.map(p => p.id).join(',');
    }
    function mark() {
        rows().forEach((li, i) => {
            li.classList.toggle('is-playing', i === S.current);
            li.classList.toggle('is-played', i < S.current);
        });
        if (!S.browsing) follow();
    }
    // keep the playing track at the top of the list, unless the listener has scrolled away to look around
    function follow() {
        const scroller = $('radio-scroll'), li = rows()[Math.max(0, S.current)];
        if (scroller && li) scroller.scrollBy({top: li.getBoundingClientRect().top - scroller.getBoundingClientRect().top, behavior: 'smooth'});
    }
    function showLive(flashFrom) {
        const list = document.querySelector('.radio-set'), scroller = $('radio-scroll');
        if (!list) return;
        list.innerHTML = S.queue.map(p => p.html).join('');
        if (flashFrom != null) rows().slice(flashFrom).forEach(li => li.classList.add('fresh'));
        scroller.classList.add('is-live');
        $('radio-result').classList.add('is-live');  // the page's own "starting from" no longer describes what's playing
        for (const ev of ['wheel', 'touchmove', 'keydown']) {
            scroller.addEventListener(ev, () => { S.browsing = true; $('rp-sync').hidden = false; }, {passive: true});
        }
        totals();
        mark();
    }
    function replaceAfter(index, picks, flash) {
        S.queue = S.queue.slice(0, index + 1).concat(picks.map(p => ({id: p.spotify_id, html: p.html, ms: p.ms})));
        const list = document.querySelector('.radio-set');
        if (list) {
            rows().slice(index + 1).forEach(li => li.remove());
            const count = rows().length;
            list.insertAdjacentHTML('beforeend', picks.map(p => p.html).join(''));
            if (flash) rows().slice(count).forEach(li => li.classList.add('fresh'));
            mark();
        }
        totals();
    }

    // ---- playing ----
    function settings() {
        const form = $('radio-controls');
        if (form) S.settings = {adventure: +form.elements.adventure.value, n: +form.elements.n.value};
        return S.settings;
    }
    // Spotify's queue can't be edited, so a track queued for a set we've since changed is remembered and skipped
    function abandonQueued() {
        if (S.sent > S.current && S.queue[S.sent]) S.stale.add(S.queue[S.sent].id);
    }
    async function playFrom(i, queue) {
        abandonQueued();
        S.queue = queue;
        S.current = -1;
        await S.player.activateElement();
        try {
            await playOnDevice({uris: [uri(S.queue[i].id)]});
            S.sent = i;
            S.active = true;
            S.browsing = false;
            $('rp-next').hidden = false;
            $('rp-sync').hidden = true;
            document.documentElement.classList.add('radio-bar');
            const scroller = $('radio-scroll');
            if (scroller && !scroller.classList.contains('is-live')) showLive();
        } catch (e) { status(`Couldn't start playback (${esc(e.message)}).`); }
    }
    async function more(afterIndex, n, random) {
        const resp = await fetch('/radio/more', {method: 'POST', headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({after: S.queue[afterIndex].id, played: S.queue.slice(0, afterIndex + 1).map(p => p.id),
                                  n, random: !!random, seed: Math.floor(Math.random() * 1e6), adventure: settings().adventure})});
        if (!resp.ok) throw new Error(resp.status);
        return (await resp.json()).picks;
    }
    const endless = () => { const box = $('rp-endless'); return !box || box.checked; };
    async function keepGoing() {
        // running low: sequence more from the end of the set
        if (!S.extending && endless() && S.current >= S.queue.length - 3) {
            S.extending = true;
            try { replaceAfter(S.queue.length - 1, await more(S.queue.length - 1, 10)); } catch (e) {}
            S.extending = false;
        }
        // playing the last track Spotify knows: hand it the next one
        if (!S.queueing && S.current === S.sent && S.sent < S.queue.length - 1) {
            S.queueing = true;
            try { await api('POST', `/me/player/queue?device_id=${S.deviceId}&uri=${encodeURIComponent(uri(S.queue[S.sent + 1].id))}`); S.sent++; }
            catch (e) {}
            S.queueing = false;
        }
    }
    // controls changed while listening: what's playing and the track already handed to Spotify stay, the rest
    // is re-sequenced (random: from a random new start instead of from here)
    async function retune(random) {
        const keep = Math.max(S.current, S.sent);
        const n = Math.max(5, settings().n - keep - 1);
        try {
            replaceAfter(keep, await more(keep, n, random), true);
            keepGoing();
        } catch (e) { status(`Couldn't change what's next (${esc(e.message)}).`); }
    }

    function connect() {
        window.onSpotifyWebPlaybackSDKReady = () => {
            const p = S.player = new Spotify.Player({name: 'Birds radio', volume: 0.8,
                                                     getOAuthToken: cb => getToken(true).then(cb, () => {})});
            p.addListener('ready', ({device_id}) => {
                S.deviceId = device_id;
                $('rp-play').disabled = false;
                if (!S.active) status($('radio-controls') ? 'Ready. Press Play, or ▶ next to any track to start there.'
                                                          : 'Ready. <a href="/radio">Pick a set to play</a>.');
                document.documentElement.classList.add('player-ready');
            });
            p.addListener('not_ready', () => status('Spotify player went offline.'));
            p.addListener('account_error', () => {
                status('Playing here needs Spotify Premium. You can still save a set as a playlist.');
                $('rp-play').disabled = true;
            });
            p.addListener('authentication_error', () => { getToken(true).catch(() => {}); });
            p.addListener('initialization_error', ({message}) => status(`This browser can't play Spotify here (${esc(message)}).`));
            p.addListener('playback_error', ({message}) => status(`Playback problem: ${esc(message)}`));
            p.addListener('player_state_changed', state => {
                if (!state) return;
                const t = state.track_window.current_track;
                const id = (t.linked_from && t.linked_from.id) || t.id;
                status(`${state.paused ? 'Paused' : 'Now playing'}: <a href="/radio">${esc(t.name)}</a> · ${esc(t.artists.map(a => a.name).join(', '))}`);
                $('rp-play').textContent = state.paused ? '▶ Play' : '⏸ Pause';
                if (S.stale.has(id) && !state.paused) { S.stale.delete(id); p.nextTrack(); return; }
                const i = S.queue.findIndex(q => q.id === id);
                if (i >= 0 && i !== S.current) { S.current = i; mark(); }
                if (i >= 0) keepGoing();
            });
            p.connect();
        };
        const script = document.createElement('script');
        script.src = 'https://sdk.scdn.co/spotify-player.js';
        script.async = true;
        document.head.appendChild(script);
        S.sdkRequested = true;
    }

    // ---- /radio page controls (a new set each time when idle; rewrite what's next while listening) ----
    let latest = 0, timer = null;
    async function loadSet(url) {
        const mine = ++latest, result = $('radio-result'), form = $('radio-controls');
        result.classList.add('loading');
        try {
            const resp = await fetch(url);
            if (!resp.ok) throw new Error(resp.status);
            const doc = new DOMParser().parseFromString(await resp.text(), 'text/html');
            if (mine !== latest) return;  // a newer request is on its way
            const fresh = doc.getElementById('radio-result');
            result.replaceWith(fresh);
            for (const key of ['seed', 'artist', 'show', 'track']) {
                form.elements[key].value = fresh.dataset[key];
                form.elements[key].disabled = !fresh.dataset[key];
            }
            history.replaceState(history.state, '', resp.url);  // keep Turbo's state
        } catch (e) {
            if (mine === latest) location.href = url;
        } finally {
            if (mine === latest && $('radio-result')) $('radio-result').classList.remove('loading');
        }
    }
    function bindRadioPage() {
        const form = $('radio-controls');
        if (!form) return;
        $('radio-update').hidden = true;
        if (S.active) {
            // arriving from a "Birds radio" link while listening: that set comes up after the current track
            const params = new URLSearchParams(location.search);
            let flashFrom = null;
            if (lastVisit !== 'restore' && ['artist', 'show', 'track'].some(k => params.get(k))) {
                abandonQueued();
                const keep = S.current;
                S.queue = S.queue.slice(0, keep + 1).concat(fromPage());
                S.sent = keep;
                flashFrom = keep + 1;
                keepGoing();
            }
            S.browsing = false;
            showLive(flashFrom);
        } else {
            settings();
        }
        const fromControls = () => form.action + '?' + new URLSearchParams(new FormData(form));
        const changed = () => {
            settings();
            if (S.active) { history.replaceState(history.state, '', fromControls()); retune(); }
            else loadSet(fromControls());
        };
        form.addEventListener('input', () => { clearTimeout(timer); timer = setTimeout(changed, 250); });
        form.addEventListener('submit', e => { e.preventDefault(); changed(); });
    }

    // clicks anywhere: ▶ on a track, Reshuffle / Random start, and the bar's buttons
    document.addEventListener('click', e => {
        const play = e.target.closest('.play-from');
        if (play && S.player) {
            const li = play.closest('li');
            // mid-set, start from that track in what's playing; otherwise the page's set becomes what's playing
            const queue = S.active && $('radio-scroll') && $('radio-scroll').classList.contains('is-live') ? S.queue : fromPage();
            playFrom(rows().indexOf(li), queue);
            return;
        }
        const nav = e.target.closest('.radio-nav a');
        if (nav && !(e.metaKey || e.ctrlKey || e.shiftKey)) {
            e.preventDefault();
            if (S.active) { retune(nav.dataset.action === 'random'); return; }
            const url = new URL(nav.href);  // keep the controls' current settings
            url.searchParams.set('adventure', settings().adventure);
            url.searchParams.set('n', settings().n);
            loadSet(url);
            return;
        }
        if (!S.player) return;
        if (e.target.closest('#rp-play')) {
            if (S.active) S.player.togglePlay();
            else if (rows().length) playFrom(0, fromPage());
            else if (window.Turbo) Turbo.visit('/radio');
        } else if (e.target.closest('#rp-next')) {
            S.player.nextTrack();
        } else if (e.target.closest('#rp-sync')) {
            S.browsing = false;
            $('rp-sync').hidden = true;
            follow();
        }
    });
    document.addEventListener('change', e => {
        if (e.target.id !== 'rp-endless') return;
        try { localStorage.setItem('radio-endless', e.target.checked ? '1' : '0'); } catch (err) {}
        if (e.target.checked && S.active) keepGoing();
    });

    let lastVisit = null;  // Turbo's visit action: 'advance', 'replace' or 'restore' (back/forward)
    document.addEventListener('turbo:visit', e => { lastVisit = e.detail.action; });

    // every page view, the first one included
    document.addEventListener('turbo:load', () => {
        const bar = $('radio-player');
        document.documentElement.classList.toggle('radio-bar', !!bar && (S.active || !!$('radio-controls')));
        if (!S.browsing) { const sync = $('rp-sync'); if (sync) sync.hidden = true; }
        if (bar && !S.sdkRequested) {
            try { $('rp-endless').checked = localStorage.getItem('radio-endless') !== '0'; } catch (e) {}
            connect();
        }
        bindRadioPage();
    });
})();
