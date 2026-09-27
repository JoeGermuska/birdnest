// Birds radio player. Loaded once in <head>; Turbo Drive swaps pages underneath without reloading, so this
// script's state and the #radio-player bar (data-turbo-permanent, with its drawer holding the live track list)
// survive moving around the site, and the music keeps playing. Uses Spotify's Web Playback SDK (Premium only).
//
// Spotify is handed one track at a time (play, then queue the next as each starts), so changing what's
// coming never touches what's playing. Any Birds radio link on the site starts that set here, in place.
(function () {
    // once per page load: if Turbo ever re-runs this script, a second copy would start a second player
    if (window.birdsRadioLoaded) return;
    window.birdsRadioLoaded = true;
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
            status(`Your Spotify login needs renewing. <a href="${loginLink()}" data-turbo="false">Log in again</a>`);
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
        const text = await resp.text();
        return text ? JSON.parse(text) : null;
    }
    // play on this page's device; Spotify answers 404 until the device has been made the active one
    async function playOnDevice(body) {
        const play = () => api('PUT', `/me/player/play?device_id=${S.deviceId}`, body);
        try {
            await play();
        } catch (e) {
            if (e.status !== 404) throw e;
            await api('PUT', '/me/player', {device_ids: [S.deviceId], play: false}).catch(() => {});
            await new Promise(r => setTimeout(r, 800));
            try {
                await play();
            } catch (e2) {
                if (e2.status !== 404) throw e2;
                // still unknown: the device went away (the browser suspended it, say); reconnect and try once more
                await reconnect();
                await api('PUT', '/me/player', {device_ids: [S.deviceId], play: false}).catch(() => {});
                await new Promise(r => setTimeout(r, 800));
                await play();
            }
        }
    }
    let readyWaiters = [];
    async function reconnect() {
        status('Reconnecting to Spotify…');
        S.ready = false;
        S.player.disconnect();
        const ready = new Promise(r => readyWaiters.push(r));
        await S.player.connect();
        await Promise.race([ready, new Promise((_, no) => setTimeout(() => no(new Error("Spotify didn't reconnect")), 10000))]);
    }

    // The set survives a reload (browsers, iOS especially, reload tabs they've put to sleep): keep it in the tab
    function save() {
        try {
            sessionStorage.setItem('radio-set', JSON.stringify({queue: S.queue, current: S.current, from: $('rp-from').textContent}));
        } catch (e) {}
    }
    function restore() {
        try {
            const saved = JSON.parse(sessionStorage.getItem('radio-set') || 'null');
            if (!saved || !saved.queue.length) return;
            S.queue = saved.queue;
            S.current = saved.current;
            $('rp-from').textContent = saved.from;
            draw();
            S.resume = Math.max(0, saved.current);
        } catch (e) {}
    }

    // ---- the live list, in the player's drawer ----
    const liveRows = () => [...document.querySelectorAll('#rp-list li')];
    function draw(flashFrom) {
        $('rp-list').innerHTML = S.queue.map(p => p.html).join('');
        if (flashFrom != null) liveRows().slice(flashFrom).forEach(li => li.classList.add('fresh'));
        mark();
        save();
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
        save();
    }
    function openDrawer(open) {
        $('rp-drawer').hidden = !open;
        $('rp-toggle').setAttribute('aria-expanded', String(open));
        $('rp-toggle').textContent = open ? 'Tracks ▾' : 'Tracks ▴';
        try { localStorage.setItem('radio-drawer', open ? '1' : '0'); } catch (e) {}
        if (open) { S.browsing = false; $('rp-sync').hidden = true; follow(); loadQueues(); }
    }

    // ---- ♥ and ＋: save what's playing to your Liked Songs, or add it to the playlist you use as your queue ----
    async function showLiked() {
        const like = $('rp-like'), id = S.trackId;
        like.hidden = false;
        try {
            const [liked] = await api('GET', `/me/tracks/contains?ids=${id}`);
            if (id === S.trackId) setLiked(liked);
        } catch (e) {}
    }
    function setLiked(liked) {
        $('rp-like').textContent = liked ? '♥' : '♡';
        $('rp-like').setAttribute('aria-pressed', String(liked));
        $('rp-like').title = liked ? 'In your Liked Songs (click to remove)' : 'Save to your Liked Songs';
    }
    async function toggleLike() {
        const liked = $('rp-like').getAttribute('aria-pressed') === 'true', id = S.trackId;
        setLiked(!liked);
        try { await api(liked ? 'DELETE' : 'PUT', `/me/tracks?ids=${id}`); }
        catch (e) { setLiked(liked); status(`Couldn't update your Liked Songs (${esc(e.message)}).`); }
    }
    const savedQueue = () => { try { return JSON.parse(localStorage.getItem('radio-queue') || 'null'); } catch (e) { return null; } };
    let queuesLoaded = false;
    // your playlists you can add to (your own, and collaborative ones), for choosing a queue
    async function loadQueues() {
        if (queuesLoaded || !S.ready) return;
        queuesLoaded = true;
        const select = $('rp-queue'), chosen = savedQueue();
        try {
            const me = await api('GET', '/me');
            const lists = [];
            for (let offset = 0; offset < 500; offset += 50) {
                const page = await api('GET', `/me/playlists?limit=50&offset=${offset}`);
                lists.push(...page.items.filter(pl => pl && (pl.owner.id === me.id || pl.collaborative)));
                if (!page.next) break;
            }
            select.innerHTML = '<option value="">Choose a playlist…</option>' + lists.map(pl =>
                `<option value="${esc(pl.id)}"${chosen && chosen.id === pl.id ? ' selected' : ''}>${esc(pl.name)}</option>`).join('');
        } catch (e) { queuesLoaded = false; }
    }
    async function addToQueue() {
        const q = savedQueue(), add = $('rp-add');
        if (!q) {  // no queue yet: show where to choose one
            openDrawer(true);
            $('rp-queue').focus();
            status('Choose the playlist you use as your queue, then press ＋ again.');
            return;
        }
        try {
            await api('POST', `/playlists/${q.id}/items`, {uris: [uri(S.trackId)]})
                .catch(e => { if (e.status === 404) return api('POST', `/playlists/${q.id}/tracks`, {uris: [uri(S.trackId)]}); throw e; });
            if (S.nowHtml) status(S.nowHtml);
            add.textContent = '✓';
            add.title = `Added to ${q.name}`;
            setTimeout(() => { add.textContent = '＋'; add.title = 'Add to your queue playlist'; }, 2500);
        } catch (e) { status(`Couldn't add to ${esc(q.name)} (${esc(e.message)}).`); }
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
            if (S.player) return;  // one player per page load, however often the SDK script runs
            const p = S.player = new Spotify.Player({name: 'Birds radio', volume: 0.8,
                                                     getOAuthToken: cb => getToken(true).then(cb, () => {})});
            p.addListener('ready', ({device_id}) => {
                S.deviceId = device_id;
                S.ready = true;
                S.frame = [...document.querySelectorAll('body > iframe')].find(isSpotifyFrame) || S.frame;
                if (!$('rp-drawer').hidden) loadQueues();
                readyWaiters.forEach(r => r());
                readyWaiters = [];
                $('rp-play').disabled = false;
                if (!S.active) status(S.resume != null ? 'Ready. Press ▶ to pick up where you left off.'
                                                       : 'Ready. Press ▶ on any track, or a radio link, to start.');
                document.documentElement.classList.add('player-ready');
            });
            p.addListener('not_ready', () => { S.ready = false; if (!readyWaiters.length) status('Spotify player went offline. Press ▶ to reconnect.'); });
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
                if (id !== S.trackId) {
                    S.trackId = id;
                    showLiked();
                    $('rp-add').hidden = false;
                    $('rp-add').title = savedQueue() ? `Add to ${savedQueue().name}` : 'Add to your queue playlist';
                }
                S.nowHtml = `${state.paused ? 'Paused' : 'Now playing'}: <strong>${esc(t.name)}</strong> · ${esc(t.artists.map(a => a.name).join(', '))}`;
                status(S.nowHtml);
                $('rp-play').textContent = state.paused ? '▶' : '⏸';
                $('rp-play').setAttribute('aria-label', state.paused ? 'Play' : 'Pause');
                if (S.stale.has(id) && !state.paused) { S.stale.delete(id); p.nextTrack(); return; }
                const i = S.queue.findIndex(q => q.id === id);
                if (i >= 0 && i !== S.current) { S.current = i; mark(); save(); }
                if (i >= 0) keepGoing();
            });
            p.connect();
        };
        const script = document.createElement('script');
        script.src = 'https://sdk.scdn.co/spotify-player.js';
        script.async = true;
        // Turbo would otherwise run it again when a cached page's <head> comes back, making a second player
        script.setAttribute('data-turbo-eval', 'false');
        document.head.appendChild(script);
        S.sdkRequested = true;
    }

    // The Web Playback SDK plays through an iframe it adds to <body>. Turbo normally swaps in a whole new <body>,
    // which would take the iframe (and the music) with it, and moving an iframe reloads it. So keep the current
    // <body> and swap everything in it except Spotify's iframes.
    const isSpotifyFrame = el => el.tagName === 'IFRAME' && /scdn\.co|spotify\.com/.test(el.src || '');
    document.addEventListener('turbo:before-render', e => {
        e.detail.render = (current, next) => {
            // the player that's here stays, with its set, whatever the new page brings: a page without one (an
            // error page, say) mustn't take it away, and a page with one mustn't add a second
            const bar = current.querySelector('#radio-player'), incoming = next.querySelector('#radio-player');
            if (bar && incoming && incoming !== bar) incoming.remove();
            [...current.childNodes].forEach(node => { if (!isSpotifyFrame(node) && node !== bar) node.remove(); });
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
        // links off the site open in a new tab, so leaving never stops the radio
        const out = e.target.closest('a[href]');
        if (out && !out.target && new URL(out.href, location.href).origin !== location.origin && /^https?:/.test(out.href)) {
            out.target = '_blank';
            out.rel = 'noopener';
        }
        if (e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
        // Turbo only follows HTML links; links inside charts (SVG <a>: the genre map, timeline ticks) would
        // load a whole new page and stop the radio, so send those through Turbo too
        const svgLink = e.target.closest('a');
        if (svgLink instanceof SVGAElement && window.Turbo) {
            const href = svgLink.getAttribute('href') || svgLink.getAttribute('xlink:href');
            const url = href && new URL(href, location.href);
            if (url && url.origin === location.origin && !svgLink.getAttribute('target')) {
                e.preventDefault();
                Turbo.visit(url.href);
                return;
            }
        }
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
            if (S.active && S.ready) S.player.togglePlay();
            else if (S.queue.length) {  // picking up after a reload, or after the player dropped out
                const at = S.active ? Math.max(0, S.current) : S.resume;
                (S.ready ? Promise.resolve() : reconnect()).then(() => playFrom(at || 0)).catch(e => status(esc(e.message)));
            }
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
        } else if (e.target.closest('#rp-like') && S.trackId) {
            toggleLike();
        } else if (e.target.closest('#rp-add') && S.trackId) {
            addToQueue();
        }
    });
    document.addEventListener('change', e => {
        if (e.target.id === 'rp-endless') {
            try { localStorage.setItem('radio-endless', e.target.checked ? '1' : '0'); } catch (err) {}
            if (e.target.checked && S.active) keepGoing();
        } else if (e.target.id === 'rp-adventure') {
            retune(false);
        } else if (e.target.id === 'rp-queue') {
            const opt = e.target.selectedOptions[0];
            try {
                if (opt.value) localStorage.setItem('radio-queue', JSON.stringify({id: opt.value, name: opt.textContent}));
                else localStorage.removeItem('radio-queue');
            } catch (err) {}
            $('rp-add').title = opt.value ? `Add to ${opt.textContent}` : 'Add to your queue playlist';
        }
    });

    // Belt and braces: Turbo's own permanent-element handling can put a copy of the bar back after the render
    // above (seen after error pages and back/forward), and cached pages bring copies of Spotify's frame. After
    // every render, keep the live bar and frame and drop any copies.
    document.addEventListener('turbo:render', () => {
        const bars = [...document.querySelectorAll('#radio-player')];
        if (!S.bar || !S.bar.isConnected) S.bar = bars[0];
        bars.forEach(el => { if (el !== S.bar) el.remove(); });
        const frames = [...document.querySelectorAll('body > iframe')].filter(isSpotifyFrame);
        if (!S.frame || !S.frame.isConnected) S.frame = frames[0];
        frames.forEach(el => { if (el !== S.frame) el.remove(); });
    });

    // going to another page on the site: fold the track list away so it doesn't cover what was asked for
    document.addEventListener('turbo:visit', () => { if ($('rp-drawer') && !$('rp-drawer').hidden) openDrawer(false); });

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
            S.bar = bar;
            const q = savedQueue();  // show the chosen queue before the full list of playlists is loaded
            if (q) $('rp-queue').insertAdjacentHTML('beforeend', `<option value="${esc(q.id)}" selected>${esc(q.name)}</option>`);
            restore();
            connect();
        }
        // reserve the bar's real height at the bottom of the page (it changes with the drawer and on phones)
        const shown = document.querySelector('.radio-player');
        if (shown && shown !== S.measured) {
            S.measured = shown;
            if (!S.sizer) S.sizer = new ResizeObserver(([entry]) =>
                document.documentElement.style.setProperty('--radio-bar-h', `${Math.ceil(entry.target.offsetHeight)}px`));
            S.sizer.disconnect();
            S.sizer.observe(shown);
        }
        bindRadioPage();
    });
})();
