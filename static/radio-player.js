// Birds radio player. Loaded once in <head>; Turbo Drive swaps pages underneath without reloading, so this
// script's state and the #radio-player bar (data-turbo-permanent, with its drawer holding the live track list)
// survive moving around the site, and the music keeps playing. Uses Spotify's Web Playback SDK (Premium only).
//
// Spotify is handed one track at a time (play, then queue the next as each starts), so changing what's
// coming never touches what's playing. Any Birds radio link on the site starts that set here, in place.
(function () {
    const S = {
        player: null, deviceId: null, token: null, tokenAt: 0, sdkRequested: false, ready: false,
        queue: [],      // [{id, html, ms}] in play order
        sent: -1,       // index of the last track Spotify knows about
        current: -1,    // index of what's playing
        stale: new Set(),  // tracks queued on Spotify that are no longer in the set; skipped if they come up
        active: false, extending: false, queueing: false, browsing: false,
    };
    const $ = id => document.getElementById(id);
    const uri = id => 'spotify:track:' + id;
    const here = () => location.pathname + location.search;
    const loginLink = () => `/spotify/login?next=${encodeURIComponent(here())}`;
    const status = html => { const el = $('rp-now'); if (el) el.innerHTML = html; };
    const esc = s => String(s).replace(/[&<>"]/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;'}[c]));
    const adventure = () => $('rp-adventure') ? +$('rp-adventure').value : 30;

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

    // ---- the live list, in the player's drawer ----
    const liveRows = () => [...document.querySelectorAll('#rp-list li')];
    function draw(flashFrom) {
        $('rp-list').innerHTML = S.queue.map(p => p.html).join('');
        if (flashFrom != null) liveRows().slice(flashFrom).forEach(li => li.classList.add('fresh'));
        mark();
    }
    function mark() {
        liveRows().forEach((li, i) => {
            li.classList.toggle('is-playing', i === S.current);
            li.classList.toggle('is-played', i < S.current);
        });
        if (!S.browsing) follow();
    }
    // keep the playing track at the top of the list, unless the listener has scrolled away to look around
    function follow() {
        const scroller = $('rp-scroll'), li = liveRows()[Math.max(0, S.current)];
        if (scroller && li && !$('rp-drawer').hidden) {
            scroller.scrollBy({top: li.getBoundingClientRect().top - scroller.getBoundingClientRect().top, behavior: 'smooth'});
        }
    }
    function replaceAfter(index, picks, flash) {
        S.queue = S.queue.slice(0, index + 1).concat(picks.map(p => ({id: p.spotify_id, html: p.html, ms: p.ms})));
        liveRows().slice(index + 1).forEach(li => li.remove());
        const count = liveRows().length;
        $('rp-list').insertAdjacentHTML('beforeend', picks.map(p => p.html).join(''));
        if (flash) liveRows().slice(count).forEach(li => li.classList.add('fresh'));
        mark();
    }
    function openDrawer(open) {
        $('rp-drawer').hidden = !open;
        $('rp-toggle').setAttribute('aria-expanded', String(open));
        $('rp-toggle').textContent = open ? 'Tracks ▾' : 'Tracks ▴';
        try { localStorage.setItem('radio-drawer', open ? '1' : '0'); } catch (e) {}
        if (open) { S.browsing = false; $('rp-sync').hidden = true; follow(); }
    }

    // ---- playing ----
    // Spotify's queue can't be edited, so a track queued for a set we've since changed is remembered and skipped
    function abandonQueued() {
        if (S.sent > S.current && S.queue[S.sent]) S.stale.add(S.queue[S.sent].id);
    }
    async function playFrom(i, queue, label) {
        if (!S.ready) { status('The Spotify player is still connecting. Try again in a moment.'); return; }
        abandonQueued();
        if (queue) {
            S.queue = queue;
            if (label) $('rp-from').textContent = label;
            draw();
        }
        S.current = -1;
        await S.player.activateElement();
        try {
            await playOnDevice({uris: [uri(S.queue[i].id)]});
            S.sent = i;
            S.active = true;
            S.browsing = false;
            $('rp-next').hidden = false;
            document.documentElement.classList.add('radio-bar');
        } catch (e) { status(`Couldn't start playback (${esc(e.message)}).`); }
    }
    // start the set a /radio URL describes (?artist= / ?show= / ?track=), right here
    async function startRadio(url) {
        status('Starting Birds radio…');
        document.documentElement.classList.add('radio-bar');
        try {
            const params = new URL(url, location.href).searchParams;
            params.set('adventure', adventure());
            const resp = await fetch('/radio/set?' + params);
            if (!resp.ok) throw new Error(resp.status);
            const set = await resp.json();
            await playFrom(0, set.picks.map(p => ({id: p.spotify_id, html: p.html, ms: p.ms})), `From ${set.label}`);
        } catch (e) { status(`Couldn't start that set (${esc(e.message)}).`); }
    }
    async function more(afterIndex, n, random) {
        const resp = await fetch('/radio/more', {method: 'POST', headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({after: S.queue[afterIndex].id, played: S.queue.slice(0, afterIndex + 1).map(p => p.id),
                                  n, random: !!random, seed: Math.floor(Math.random() * 1e6), adventure: adventure()})});
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
    // what's playing and the track already handed to Spotify stay; the rest is re-sequenced
    // (random: from a random new start instead of from here)
    async function retune(random) {
        if (!S.active) return;
        const keep = Math.max(S.current, S.sent);
        try {
            replaceAfter(keep, await more(keep, 15, random), true);
            if (random) $('rp-from').textContent = 'From a random new start';
            keepGoing();
        } catch (e) { status(`Couldn't change what's next (${esc(e.message)}).`); }
    }

    function connect() {
        window.onSpotifyWebPlaybackSDKReady = () => {
            const p = S.player = new Spotify.Player({name: 'Birds radio', volume: 0.8,
                                                     getOAuthToken: cb => getToken(true).then(cb, () => {})});
            p.addListener('ready', ({device_id}) => {
                S.deviceId = device_id;
                S.ready = true;
                $('rp-play').disabled = false;
                if (!S.active) status('Ready. Press ▶ on any track, or a radio link, to start.');
                document.documentElement.classList.add('player-ready');
            });
            p.addListener('not_ready', () => { S.ready = false; status('Spotify player went offline.'); });
            p.addListener('account_error', () => {
                status('Playing here needs Spotify Premium. You can still save a set as a playlist on the radio page.');
                $('rp-play').disabled = true;
            });
            p.addListener('authentication_error', () => { getToken(true).catch(() => {}); });
            p.addListener('initialization_error', ({message}) => status(`This browser can't play Spotify here (${esc(message)}).`));
            p.addListener('playback_error', ({message}) => status(`Playback problem: ${esc(message)}`));
            p.addListener('player_state_changed', state => {
                if (!state) return;
                const t = state.track_window.current_track;
                const id = (t.linked_from && t.linked_from.id) || t.id;
                status(`${state.paused ? 'Paused' : 'Now playing'}: <strong>${esc(t.name)}</strong> · ${esc(t.artists.map(a => a.name).join(', '))}`);
                $('rp-play').textContent = state.paused ? '▶' : '⏸';
                $('rp-play').setAttribute('aria-label', state.paused ? 'Play' : 'Pause');
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

    // The Web Playback SDK plays through an iframe it adds to <body>. Turbo normally swaps in a whole new <body>,
    // which would take the iframe (and the music) with it, and moving an iframe reloads it. So keep the current
    // <body> and swap everything in it except Spotify's iframes.
    const isSpotifyFrame = el => el.tagName === 'IFRAME' && /scdn\.co|spotify\.com/.test(el.src || '');
    document.addEventListener('turbo:before-render', e => {
        e.detail.render = (current, next) => {
            [...current.childNodes].forEach(node => { if (!isSpotifyFrame(node)) node.remove(); });
            for (const a of [...current.attributes]) current.removeAttribute(a.name);
            for (const a of [...next.attributes]) current.setAttribute(a.name, a.value);
            // pages restored from Turbo's cache carry copies of the frame; the live one is already here
            current.prepend(...[...next.childNodes].filter(node => !isSpotifyFrame(node)));
        };
    });

    // ---- the /radio page: a proposed set to preview, play or save (it doesn't drive the live list) ----
    const pageRows = () => [...document.querySelectorAll('#radio-list li')];
    const pageSet = () => pageRows().map(li => ({id: li.dataset.spotify, ms: +li.dataset.ms || 0, html: li.outerHTML}));
    const pageLabel = () => $('radio-result') ? $('radio-result').dataset.label : null;
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
            if (mine === latest && !S.active) location.href = url;
        } finally {
            if (mine === latest && $('radio-result')) $('radio-result').classList.remove('loading');
        }
    }
    function bindRadioPage() {
        const form = $('radio-controls');
        if (!form) return;
        $('radio-update').hidden = true;
        const fromControls = () => form.action + '?' + new URLSearchParams(new FormData(form));
        form.addEventListener('input', () => { clearTimeout(timer); timer = setTimeout(() => loadSet(fromControls()), 250); });
        form.addEventListener('submit', e => { e.preventDefault(); loadSet(fromControls()); });
    }

    // a link to /radio?artist=… / ?show=… / ?track=…: with the player ready, play it here instead of going there
    function radioLink(a) {
        if (!a || !S.ready || a.closest('.radio-nav')) return null;
        const url = new URL(a.href, location.href);
        if (url.origin !== location.origin || url.pathname !== '/radio') return null;
        return ['artist', 'show', 'track'].some(k => url.searchParams.get(k)) ? url : null;
    }

    document.addEventListener('click', e => {
        if (e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
        const link = radioLink(e.target.closest('a'));
        if (link) { e.preventDefault(); startRadio(link); return; }
        const play = e.target.closest('.play-from');
        if (play && S.player) {
            const li = play.closest('li');
            if (li.closest('#rp-list')) playFrom(liveRows().indexOf(li));
            else playFrom(pageRows().indexOf(li), pageSet(), pageLabel());
            return;
        }
        const nav = e.target.closest('.radio-nav a');
        if (nav && $('radio-controls')) {  // Reshuffle / Random start on the radio page: a new proposed set
            e.preventDefault();
            const url = new URL(nav.href), form = $('radio-controls');
            url.searchParams.set('adventure', form.elements.adventure.value);
            url.searchParams.set('n', form.elements.n.value);
            loadSet(url);
            return;
        }
        if (!S.player) return;
        if (e.target.closest('#radio-play-set')) {
            playFrom(0, pageSet(), pageLabel());
        } else if (e.target.closest('#rp-play')) {
            if (S.active) S.player.togglePlay();
            else if (pageRows().length) playFrom(0, pageSet(), pageLabel());
            else startRadio('/radio');
        } else if (e.target.closest('#rp-next')) {
            S.player.nextTrack();
        } else if (e.target.closest('#rp-toggle')) {
            openDrawer($('rp-drawer').hidden);
        } else if (e.target.closest('#rp-sync')) {
            S.browsing = false;
            $('rp-sync').hidden = true;
            follow();
        } else if (e.target.closest('#rp-reshuffle')) {
            retune(false);
        } else if (e.target.closest('#rp-random')) {
            retune(true);
        }
    });
    document.addEventListener('change', e => {
        if (e.target.id === 'rp-endless') {
            try { localStorage.setItem('radio-endless', e.target.checked ? '1' : '0'); } catch (err) {}
            if (e.target.checked && S.active) keepGoing();
        } else if (e.target.id === 'rp-adventure') {
            retune(false);
        }
    });

    // every page view, the first one included
    document.addEventListener('turbo:load', () => {
        const bar = $('radio-player');
        // the bar is always there when Spotify is set up: the player, or a prompt to log in
        document.documentElement.classList.toggle('radio-bar', !!document.querySelector('.radio-player'));
        if (bar && !S.sdkRequested) {
            try {
                $('rp-endless').checked = localStorage.getItem('radio-endless') !== '0';
                if (localStorage.getItem('radio-drawer') === '1') openDrawer(true);
            } catch (e) {}
            // only real input counts as scrolling away; the list's own following doesn't
            for (const ev of ['wheel', 'touchmove', 'keydown']) {
                $('rp-scroll').addEventListener(ev, () => { if (S.active) { S.browsing = true; $('rp-sync').hidden = false; } }, {passive: true});
            }
            connect();
        }
        bindRadioPage();
    });
})();
