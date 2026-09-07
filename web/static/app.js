/* ============================================================
   faceproof — front end
   Two data sources, one set of event handlers.

   LIVE
     POST /api/upload            multipart, field "file"  -> {image_id, url}
     GET  /api/run?image=<id>    text/event-stream
          events: stage, face, candidates, match, chain, done, error
     POST /api/verify            {image_id} -> verdict + hashes
     POST /api/tamper            {image_id} -> verdict + hashes + mutation

   DEMO
     If any of the above is unreachable, a scripted timeline calls the
     exact same handlers with generated data and the page is badged
     DEMO DATA. Nothing else about the rendering changes.
   ============================================================ */
(function () {
  'use strict';

  /* Default only. The live API sends its effective threshold on the
     `candidates` event, which overwrites this - see on.candidates. */
  var THRESHOLD = 0.60;
  var API = {
    upload: '/api/upload',
    run: '/api/run',
    verify: '/api/verify',
    tamper: '/api/tamper'
  };

  var reduce = window.matchMedia('(prefers-reduced-motion: reduce)');
  var noMotion = function () { return reduce.matches; };

  /* ── tiny helpers ─────────────────────────────────────── */
  var $ = function (id) { return document.getElementById(id); };
  function esc(s) {
    return String(s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }
  function f4(n) { return Number(n).toFixed(4); }
  function clamp(n, a, b) { return Math.min(b, Math.max(a, n)); }
  function shorten(h, head, tail) {
    h = String(h || '');
    if (h.length <= head + tail + 1) return h;
    return h.slice(0, head) + '…' + h.slice(-tail);
  }
  function safeUrl(u) {
    // an empty string resolves to the current page, which is not a link
    if (u == null || String(u).trim() === '') return '';
    try {
      var p = new URL(String(u), location.href);
      return (p.protocol === 'http:' || p.protocol === 'https:') ? p.href : '';
    } catch (e) { return ''; }
  }

  /* ── state ────────────────────────────────────────────── */
  var S = {
    mode: null,            // 'live' | 'demo'
    hashSource: null,      // 'server' | 'demo'
    imageId: null,
    photoUrl: null,
    natural: { w: 0, h: 0 },
    face: null,
    candidates: [],
    match: null,
    chain: null,
    record: null,          // canonical record object
    chainHash: null,       // hash committed on-chain
    localHash: null,       // hash of the record as it exists locally
    mutation: null,
    tampered: false,
    running: false,
    started: 0,
    es: null,
    timers: [],
    tick: null,
    rafs: []
  };

  function later(fn, ms) { var t = setTimeout(fn, ms); S.timers.push(t); return t; }
  function clearTimers() { S.timers.forEach(clearTimeout); S.timers = []; S.rafs.forEach(cancelAnimationFrame); S.rafs = []; }

  /* ── log ──────────────────────────────────────────────── */
  var logBox = $('rail-log');
  function log(name, text, kind) {
    var p = document.createElement('p');
    if (kind) p.dataset.kind = kind;
    p.innerHTML = '<b>' + esc(name) + '</b><span>' + esc(text) + '</span>';
    logBox.appendChild(p);
    while (logBox.children.length > 60) logBox.removeChild(logBox.firstChild);
    logBox.scrollTop = logBox.scrollHeight;
  }

  /* ── alerts ───────────────────────────────────────────── */
  function alertShow(text) {
    $('alert-text').textContent = text;
    $('alert').hidden = false;
  }
  function alertHide() { $('alert').hidden = true; }
  $('alert-close').addEventListener('click', alertHide);

  /* ── mode badge ───────────────────────────────────────── */
  function setMode(mode, why) {
    S.mode = mode;
    var b = $('mode-badge');
    b.dataset.mode = mode;
    $('mode-text').textContent = mode === 'live' ? 'Live API' : 'Demo data';
    $('fact-mode').textContent = mode === 'live' ? 'faceproof API' : 'demo data (in browser)';
    log(mode === 'live' ? 'mode' : 'mode', mode === 'live' ? 'live API stream' : 'demo timeline' + (why ? ' — ' + why : ''));
  }

  /* ── stage status ─────────────────────────────────────── */
  var LABEL = { pending: 'Queued', running: 'Running', done: 'Done', failed: 'Failed', waiting: 'Waiting' };
  function setStage(n, status, label) {
    var map = { pending: 'waiting', running: 'running', done: 'done', ok: 'done', failed: 'failed', error: 'failed' };
    var st = map[status] || 'waiting';
    var item = document.querySelector('.rail-item[data-stage="' + n + '"]');
    var sec = $('stage-' + n);
    if (item) item.dataset.status = st;
    if (sec) sec.dataset.status = st;
    var txt = label || LABEL[status] || LABEL[st];
    var rs = $('rail-status-' + n); if (rs) rs.textContent = txt;
    var pl = $('stage-pill-' + n); if (pl) pl.textContent = txt;
    var reached = 0;
    for (var i = 1; i <= 4; i++) {
      var it = document.querySelector('.rail-item[data-stage="' + i + '"]');
      if (it && (it.dataset.status === 'done' || it.dataset.status === 'running')) reached = i;
    }
    $('fact-stage').textContent = reached + ' of 4';
  }

  /* ── elapsed clock ────────────────────────────────────── */
  function startClock() {
    S.started = performance.now();
    stopClock();
    S.tick = setInterval(function () {
      $('fact-elapsed').textContent = ((performance.now() - S.started) / 1000).toFixed(1) + ' s';
    }, 100);
  }
  function stopClock() { if (S.tick) { clearInterval(S.tick); S.tick = null; } }

  /* ── seeded noise, used only for demo data ────────────── */
  function rng(seed) {
    var s = (seed >>> 0) || 0x9e3779b9;
    return function () {
      s ^= s << 13; s >>>= 0;
      s ^= s >>> 17;
      s ^= s << 5; s >>>= 0;
      return s / 4294967296;
    };
  }
  function hexBytes(rand, n) {
    var o = '';
    for (var i = 0; i < n; i++) o += Math.floor(rand() * 256).toString(16).padStart(2, '0');
    return o;
  }
  /* Well-formed 32-byte digest for demo mode only. Live mode uses the
     keccak256 value the server puts in the `chain` event. */
  function demoDigest(str) {
    var h1 = 0x811c9dc5, h2 = 0x01000193, h3 = 0x9e3779b9, h4 = 0x85ebca6b, i, c;
    for (i = 0; i < str.length; i++) {
      c = str.charCodeAt(i);
      h1 = Math.imul(h1 ^ c, 16777619) >>> 0;
      h2 = (h2 + Math.imul(c, 2654435761)) >>> 0; h2 = ((h2 << 13) | (h2 >>> 19)) >>> 0;
      h3 = Math.imul(h3 ^ (c + i), 2246822519) >>> 0;
      h4 = (h4 + ((c << (i & 7)) >>> 0)) >>> 0; h4 = (h4 ^ (h4 >>> 15)) >>> 0;
    }
    var out = '';
    for (i = 0; i < 32; i++) {
      h1 ^= h1 << 13; h1 >>>= 0; h1 ^= h1 >>> 17; h1 ^= h1 << 5; h1 >>>= 0;
      h2 = (h2 + h1 + i) >>> 0;
      h3 = Math.imul(h3 ^ h2, 2654435761) >>> 0;
      h4 = (h4 + h3) >>> 0;
      out += (((h1 ^ h2 ^ h3 ^ h4) >>> ((i % 4) * 8)) & 0xff).toString(16).padStart(2, '0');
    }
    return '0x' + out;
  }

  /* ── procedural images, so demo mode needs no network ─── */
  function svgUri(svg) {
    return 'data:image/svg+xml;charset=utf-8,' + encodeURIComponent(svg);
  }
  function samplePhoto() {
    var r = rng(20260214), i, bars = '';
    for (i = 0; i < 26; i++) {
      bars += '<rect x="' + (i * 20) + '" y="' + Math.round(r() * 480) + '" width="12" height="' +
        Math.round(20 + r() * 160) + '" fill="#FEE101" opacity="' + (0.04 + r() * 0.1).toFixed(2) + '"/>';
    }
    return svgUri(
      '<svg xmlns="http://www.w3.org/2000/svg" width="520" height="640" viewBox="0 0 520 640">' +
      '<rect width="520" height="640" fill="#084D2A"/>' + bars +
      '<g opacity="0.9"><ellipse cx="260" cy="252" rx="104" ry="126" fill="#0B6839" stroke="#FFFBE8" stroke-opacity="0.35" stroke-width="2"/>' +
      '<path d="M96 640 C104 470 168 404 260 404 C352 404 416 470 424 640 Z" fill="#0B6839" stroke="#FFFBE8" stroke-opacity="0.28" stroke-width="2"/>' +
      '<circle cx="222" cy="238" r="9" fill="#FEE101"/><circle cx="298" cy="238" r="9" fill="#FEE101"/>' +
      '<path d="M228 306 q32 22 64 0" fill="none" stroke="#FFFBE8" stroke-opacity="0.5" stroke-width="4"/></g>' +
      '<text x="24" y="616" font-family="monospace" font-size="19" fill="#FEE101" opacity="0.75">sample subject / not a real person</text>' +
      '</svg>');
  }
  function thumbFor(seed, token) {
    var r = rng(seed), i, shapes = '';
    for (i = 0; i < 11; i++) {
      shapes += '<rect x="' + Math.round(r() * 300) + '" y="' + Math.round(r() * 220) +
        '" width="' + Math.round(18 + r() * 110) + '" height="' + Math.round(12 + r() * 80) +
        '" fill="#FEE101" opacity="' + (0.05 + r() * 0.18).toFixed(2) + '"/>';
    }
    return svgUri(
      '<svg xmlns="http://www.w3.org/2000/svg" width="320" height="240" viewBox="0 0 320 240">' +
      '<rect width="320" height="240" fill="#063A20"/>' + shapes +
      '<circle cx="160" cy="98" r="42" fill="#0B6839" stroke="#FFFBE8" stroke-opacity="0.3" stroke-width="2"/>' +
      '<path d="M92 240 C96 168 122 140 160 140 C198 140 224 168 228 240 Z" fill="#0B6839" stroke="#FFFBE8" stroke-opacity="0.24" stroke-width="2"/>' +
      '<text x="12" y="228" font-family="monospace" font-size="16" fill="#FEE101" opacity="0.8">' + esc(token || '') + '</text>' +
      '</svg>');
  }

  /* ── source badges ────────────────────────────────────── */
  var SRC = {
    'instagram.com': 'IG', 'x.com': 'X', 'twitter.com': 'X', 'facebook.com': 'FB',
    'linkedin.com': 'IN', 'tiktok.com': 'TT', 'reddit.com': 'RD',
    'youtube.com': 'YT', 'youtu.be': 'YT'
  };
  function srcToken(domain) {
    var d = String(domain || '').toLowerCase().replace(/^www\./, '');
    if (SRC[d]) return SRC[d];
    for (var k in SRC) if (d.endsWith('.' + k) || d === k) return SRC[k];
    return d.slice(0, 2).toUpperCase() || '??';
  }

  /* ═══════════════════════════════════════════════════════
     STAGE 1 — scan
     ═══════════════════════════════════════════════════════ */
  var frame = $('frame'), shot = $('shot'), bbox = $('bbox'), bpath = $('bbox-path');

  function showPhoto(url, alt) {
    S.photoUrl = url;
    $('drop').hidden = true;
    $('scan-out').hidden = false;
    shot.alt = alt || 'The photo being scanned for a face';
    $('frame-cap-text').textContent = 'Loading photo';
    return new Promise(function (res) {
      shot.onload = function () {
        S.natural = { w: shot.naturalWidth || 1, h: shot.naturalHeight || 1 };
        bbox.setAttribute('viewBox', '0 0 ' + S.natural.w + ' ' + S.natural.h);
        $('face-dim').textContent = S.natural.w + ' × ' + S.natural.h;
        res(true);
      };
      shot.onerror = function () {
        $('frame-cap-text').textContent = 'The photo could not be displayed';
        res(false);
      };
      shot.src = url;
    });
  }

  function normBox(b) {
    var W = S.natural.w, H = S.natural.h;
    if (!Array.isArray(b) || b.length < 4) return [W * 0.28, H * 0.16, W * 0.44, H * 0.44];
    var out = b.slice(0, 4).map(Number);
    // Accept either pixel coordinates or 0..1 fractions.
    if (out.every(function (v) { return v >= 0 && v <= 1; })) {
      out = [out[0] * W, out[1] * H, out[2] * W, out[3] * H];
    }
    return out;
  }

  function drawBox(box) {
    var x = box[0], y = box[1], w = box[2], h = box[3];
    var d = 'M' + x + ' ' + y + ' L' + (x + w) + ' ' + y + ' L' + (x + w) + ' ' + (y + h) +
      ' L' + x + ' ' + (y + h) + ' Z';
    bpath.setAttribute('d', d);
    var len = 0;
    try { len = bpath.getTotalLength(); } catch (e) { len = 2 * (w + h); }
    bpath.style.transition = 'none';
    bpath.style.strokeDasharray = len;
    bpath.style.strokeDashoffset = len;
    // force a reflow so the transition actually runs
    void bpath.getBoundingClientRect();
    bpath.style.transition = noMotion() ? 'none' : 'stroke-dashoffset .75s cubic-bezier(.2,.8,.2,1)';
    bpath.style.strokeDashoffset = 0;

    var t = Math.max(6, Math.min(w, h) * 0.22), ticks = '', c;
    var corners = [[x, y, 1, 1], [x + w, y, -1, 1], [x + w, y + h, -1, -1], [x, y + h, 1, -1]];
    for (var i = 0; i < 4; i++) {
      c = corners[i];
      ticks += '<path d="M' + c[0] + ' ' + (c[1] + c[3] * t) + ' L' + c[0] + ' ' + c[1] +
        ' L' + (c[0] + c[2] * t) + ' ' + c[1] + '"/>';
    }
    $('bbox-ticks').innerHTML = ticks;
  }

  function buildStrip(vec) {
    var strip = $('strip');
    strip.textContent = '';
    var maxAbs = 0, sum = 0, min = Infinity, max = -Infinity, i;
    for (i = 0; i < vec.length; i++) {
      var v = vec[i];
      if (Math.abs(v) > maxAbs) maxAbs = Math.abs(v);
      sum += v; if (v < min) min = v; if (v > max) max = v;
    }
    if (!maxAbs) maxAbs = 1;
    var norm = 0;
    for (i = 0; i < vec.length; i++) norm += vec[i] * vec[i];
    norm = Math.sqrt(norm);

    $('face-embdim').textContent = vec.length + ' floats';
    $('face-norm').textContent = f4(norm);
    $('embed-stats').innerHTML =
      [['min', f4(min)], ['max', f4(max)], ['mean', f4(sum / vec.length)],
       ['peak abs', f4(maxAbs)], ['bars drawn', String(vec.length)]]
        .map(function (r) { return '<div><dt>' + r[0] + '</dt><dd>' + r[1] + '</dd></div>'; }).join('');

    function bar(i) {
      var v = vec[i];
      var b = document.createElement('i');
      var mag = Math.abs(v) / maxAbs * 46;
      b.style.setProperty('--m', mag.toFixed(3));
      b.style.setProperty('--s', v < 0 ? '1' : '-1');
      b.style.setProperty('--o', v < 0 ? '0%' : '100%');
      if (v < 0) b.dataset.neg = '1';
      b.dataset.i = i;
      b.dataset.v = v.toFixed(6);
      return b;
    }
    var made = 0, complete = false;
    function push(n) {
      var end = Math.min(made + n, vec.length), f = document.createDocumentFragment();
      for (var j = made; j < end; j++) f.appendChild(bar(j));
      strip.appendChild(f);
      made = end;
      if (made >= vec.length) complete = true;
    }

    if (noMotion()) { push(vec.length); return; }

    // Stream the vector in, 16 dimensions per animation frame.
    (function step() {
      if (complete) return;
      push(16);
      if (!complete) S.rafs.push(requestAnimationFrame(step));
    })();

    // requestAnimationFrame is suspended while the tab is hidden, and all
    // 512 bars have to end up on screen. These two guards finish the job.
    later(function () { if (!complete) push(vec.length); }, 2000);
    document.addEventListener('visibilitychange', function seen() {
      if (complete) { document.removeEventListener('visibilitychange', seen); return; }
      if (!document.hidden) push(vec.length);
    });
  }

  $('strip').addEventListener('pointermove', function (e) {
    var t = e.target;
    if (t && t.dataset && t.dataset.i !== undefined) {
      $('strip-read').textContent = 'dim ' + t.dataset.i + '  =  ' +
        (Number(t.dataset.v) >= 0 ? '+' : '') + t.dataset.v;
      var hot = $('strip').querySelector('.is-hot');
      if (hot) hot.classList.remove('is-hot');
      t.classList.add('is-hot');
    }
  });
  $('strip-scroll').addEventListener('pointerleave', function () {
    $('strip-read').textContent = 'hover a bar to read its value';
    var hot = $('strip').querySelector('.is-hot');
    if (hot) hot.classList.remove('is-hot');
  });

  function setCrop(box) {
    var W = S.natural.w, H = S.natural.h;
    var x = clamp(box[0], 0, W), y = clamp(box[1], 0, H);
    var w = clamp(box[2], 1, W), h = clamp(box[3], 1, H);
    var el = $('crop');
    el.style.backgroundImage = 'url("' + S.photoUrl + '")';
    el.style.backgroundSize = (W / w * 100).toFixed(2) + '% ' + (H / h * 100).toFixed(2) + '%';
    var px = (W - w) > 0 ? (x / (W - w) * 100) : 50;
    var py = (H - h) > 0 ? (y / (H - h) * 100) : 50;
    el.style.backgroundPosition = clamp(px, 0, 100).toFixed(2) + '% ' + clamp(py, 0, 100).toFixed(2) + '%';
  }

  /* ═══════════════════════════════════════════════════════
     STAGE 3 — dials
     ═══════════════════════════════════════════════════════ */
  var R = 52, C = 2 * Math.PI * R;

  function dialCard(cand, i) {
    var art = document.createElement('article');
    art.className = 'dial-card';
    art.dataset.state = 'pending';
    art.style.setProperty('--i', i);
    var thrOff = -(THRESHOLD * C) + 1;
    art.innerHTML =
      '<div class="dial-holder">' +
        '<svg class="dial" viewBox="0 0 126 126" aria-hidden="true" focusable="false">' +
          '<circle class="d-track" cx="63" cy="63" r="' + R + '"/>' +
          '<circle class="d-arc" cx="63" cy="63" r="' + R + '" stroke-dasharray="' + C.toFixed(2) +
            '" stroke-dashoffset="' + C.toFixed(2) + '"/>' +
          '<circle class="d-thr" cx="63" cy="63" r="' + R + '" stroke-dasharray="2 ' + C.toFixed(2) +
            '" stroke-dashoffset="' + thrOff.toFixed(2) + '"/>' +
        '</svg>' +
        '<span class="dial-num">0.0000</span>' +
      '</div>' +
      '<span class="dial-sub">' + esc(cand.source_domain || 'unknown source') + '</span>' +
      '<p class="dial-name">' + esc(cand.title || cand.post_url || 'untitled result') + '</p>' +
      '<span class="verdict">Scoring</span>';
    return art;
  }

  function runDial(card, target) {
    var arc = card.querySelector('.d-arc');
    var num = card.querySelector('.dial-num');
    var verdict = card.querySelector('.verdict');
    var landed = false, dur = 950;
    function land() {
      if (landed) return;
      landed = true;
      arc.setAttribute('stroke-dashoffset', (C * (1 - clamp(target, 0, 1))).toFixed(2));
      num.textContent = f4(target);
      var ok = target >= THRESHOLD;
      card.dataset.state = ok ? 'verified' : 'rejected';
      verdict.textContent = ok ? 'Face verified' : 'Rejected';
    }
    if (noMotion()) { land(); return; }

    var start = performance.now();
    (function frame(now) {
      if (landed) return;
      var p = clamp((now - start) / dur, 0, 1);
      var e = 1 - Math.pow(1 - p, 3);
      var v = target * e;
      arc.setAttribute('stroke-dashoffset', (C * (1 - clamp(v, 0, 1))).toFixed(2));
      num.textContent = f4(v);
      if (p < 1) S.rafs.push(requestAnimationFrame(frame)); else land();
    })(start);

    // rAF stops while the tab is hidden; the dial still has to reach its score
    later(land, dur + 500);
  }

  /* ═══════════════════════════════════════════════════════
     STAGE 4 — record, hashes, seal
     ═══════════════════════════════════════════════════════ */
  function canonicalize(o) {
    var keys = Object.keys(o).sort();
    return '{' + keys.map(function (k) {
      return JSON.stringify(k) + ':' + JSON.stringify(o[k]);
    }).join(',') + '}';
  }
  function jsonHtml(o) {
    var keys = Object.keys(o).sort(), out = '{\n';
    keys.forEach(function (k, i) {
      var v = o[k], cls = typeof v === 'number' ? 'jn' : (typeof v === 'boolean' ? 'jb' : 'js');
      out += '  <span class="jk">"' + esc(k) + '"</span><span class="jp">:</span> ' +
        '<span class="' + cls + '">' + esc(JSON.stringify(v)) + '</span>' +
        (i < keys.length - 1 ? '<span class="jp">,</span>' : '') + '\n';
    });
    return out + '}';
  }
  function renderRecord() {
    $('json-pretty').querySelector('code').innerHTML = jsonHtml(S.record);
    var canon = canonicalize(S.record);
    var code = $('json-canon').querySelector('code');
    var m = S.mutation, marked = false;
    if (m && m.field && m.index != null && S.record[m.field] != null) {
      var valStr = JSON.stringify(S.record[m.field]);        // quoted
      var vAt = canon.indexOf(valStr);
      if (vAt >= 0) {
        var at = vAt + 1 + m.index * 2;                      // step past the quote
        code.innerHTML = esc(canon.slice(0, at)) +
          '<span class="diffbyte">' + esc(canon.slice(at, at + 2)) + '</span>' +
          esc(canon.slice(at + 2));
        marked = true;
      }
    }
    if (!marked) code.textContent = canon;
    return canon;
  }

  function typeHash(node, hash) {
    if (noMotion()) { node.textContent = hash; return; }
    node.textContent = '0x';
    var dur = 620, start = performance.now();
    (function frame(now) {
      var p = clamp((now - start) / dur, 0, 1);
      node.textContent = hash.slice(0, 2 + Math.round((hash.length - 2) * p));
      if (p < 1) S.rafs.push(requestAnimationFrame(frame));
    })(start);
    later(function () { node.textContent = hash; }, dur + 400);
  }

  function sealAnimation() {
    var sig = $('sigil');
    sig.classList.remove('s-draw', 's-spin', 's-lock');
    void sig.getBoundingClientRect();
    if (noMotion()) {
      sig.classList.add('s-draw', 's-spin', 's-lock');
      $('sigil-state').textContent = 'Sealed';
      return;
    }
    $('sigil-state').textContent = 'Writing';
    later(function () { sig.classList.add('s-draw'); }, 40);
    later(function () { sig.classList.add('s-spin'); $('sigil-state').textContent = 'Broadcasting'; }, 420);
    later(function () {
      sig.classList.add('s-lock');
      $('sigil-state').textContent = 'Sealed';
    }, 1180);
  }

  /* ── byte diff ────────────────────────────────────────── */
  function bytesOf(hex) {
    var h = String(hex || '').replace(/^0x/i, '');
    var out = [];
    for (var i = 0; i < h.length; i += 2) out.push(h.slice(i, i + 2));
    return out;
  }
  function renderDiff(chainHash, localHash) {
    var a = bytesOf(chainHash), b = bytesOf(localHash), n = Math.max(a.length, b.length), diffs = 0;
    var ha = '', hb = '';
    for (var i = 0; i < n; i++) {
      var x = a[i] || '··', y = b[i] || '··', bad = x !== y;
      if (bad) diffs++;
      ha += '<span class="' + (bad ? 'd' : '') + '">' + esc(x) + '</span>';
      hb += '<span class="' + (bad ? 'd' : '') + '">' + esc(y) + '</span>';
    }
    $('diff-chain').innerHTML = ha;
    $('diff-local').innerHTML = hb;
    $('diff-count').textContent = diffs + ' of ' + n + ' bytes changed.';
    $('diff').hidden = false;
  }

  /* ═══════════════════════════════════════════════════════
     EVENT HANDLERS — identical for live and demo
     ═══════════════════════════════════════════════════════ */
  var on = {
    stage: function (d) {
      setStage(d.n, d.status, d.label);
      log('stage', d.n + ' ' + (d.name || '') + ' — ' + (d.status || ''));
    },

    face: function (d) {
      log('face', 'confidence ' + f4(d.confidence || 0) + ', ' +
        ((d.embedding && d.embedding.length) || 0) + ' dims');
      var box = normBox(d.bbox);
      S.face = d;

      $('face-conf').textContent = f4(d.confidence == null ? 0 : d.confidence);
      $('face-box').textContent = box.map(function (v) { return Math.round(v); }).join(', ');
      $('frame-cap-text').textContent = 'Scanning';

      if (d.crop_url) {
        var cu = safeUrl(d.crop_url);
        if (cu) { $('crop').style.backgroundImage = 'url("' + cu + '")'; $('crop').style.backgroundSize = 'cover'; $('crop').style.backgroundPosition = 'center'; }
        else setCrop(box);
      } else {
        setCrop(box);
      }

      var t = noMotion() ? 0 : 1;
      frame.classList.remove('is-scanning');
      void frame.getBoundingClientRect();
      if (!noMotion()) frame.classList.add('is-scanning');

      later(function () {
        drawBox(box);
        $('frame-cap-text').textContent = 'One face found — confidence ' + f4(d.confidence == null ? 0 : d.confidence);
      }, 1400 * t);
      later(function () { bbox.classList.add('ticks-in'); }, 2050 * t);
      later(function () {
        $('embed-wrap').hidden = false;
        buildStrip(Array.isArray(d.embedding) && d.embedding.length ? d.embedding : demoEmbedding(7));
      }, 2250 * t);
    },

    candidates: function (d) {
      var items = (d && d.items) || [];
      S.candidates = items;
      /* Adopt the run's own threshold so the dials and the verified/rejected
         labels always agree with what the backend actually applied. */
      if (d && typeof d.threshold === 'number' && isFinite(d.threshold)) {
        THRESHOLD = d.threshold;
        if ($('fact-threshold')) $('fact-threshold').textContent = f4(THRESHOLD);
      }
      log('candidates', items.length + ' results');
      $('cards-empty').hidden = items.length > 0;
      var box = $('cards');
      box.textContent = '';
      items.forEach(function (c, i) {
        var art = document.createElement('article');
        art.className = 'card';
        art.style.setProperty('--i', Math.min(i, 11));   // cap the stagger
        var href = safeUrl(c.post_url);
        var img = safeUrl(c.image_url) || thumbFor(1000 + i * 37, srcToken(c.source_domain));
        art.innerHTML =
          '<a class="card-link" href="' + esc(href || '#') + '"' +
            (href ? ' target="_blank" rel="noopener noreferrer"' : ' aria-disabled="true"') + '>' +
            '<div class="card-thumb"><img src="' + esc(img) + '" alt="Thumbnail of the result on ' +
              esc(c.source_domain || 'an unknown site') + '" loading="lazy" referrerpolicy="no-referrer"></div>' +
            '<div class="card-meta">' +
              '<span class="badge"><b>' + esc(srcToken(c.source_domain)) + '</b>' +
                esc(String(c.source_domain || 'unknown').replace(/^www\./, '')) + '</span>' +
              '<h3 class="card-title">' + esc(c.title || 'Untitled result') + '</h3>' +
              '<span class="card-engine">found by ' + esc(c.engine || 'unknown engine') + '</span>' +
              (href ? '<span class="card-url">' + esc(href.replace(/^https?:\/\//, '')) + '</span>' : '') +
            '</div>' +
          '</a>';
        var im = art.querySelector('img');
        im.addEventListener('error', function () {
          im.src = thumbFor(1000 + i * 37, srcToken(c.source_domain));
          var w = document.createElement('p');
          w.className = 'card-warn';
          w.textContent = 'Thumbnail would not load. Placeholder shown.';
          art.querySelector('.card-meta').appendChild(w);
        });
        box.appendChild(art);
      });

      // Dials wait for stage 3: until the scores arrive there is nothing
      // truthful to put on them.
      $('dials').textContent = '';
      $('dials-empty').hidden = false;
      $('dials-empty').textContent = 'Waiting for similarity scores.';
      $('dials-note').hidden = true;
    },

    match: function (d) {
      S.match = d;
      log('match', (d.source_domain || '') + ' at ' + f4(d.face_similarity || 0));
      $('fact-sim').textContent = f4(d.face_similarity || 0);

      // One dial per candidate the pipeline actually scored. The API may
      // only report the winner, and inventing the other 30 numbers would
      // be a lie, so those are counted instead of drawn.
      var scored = [];
      S.candidates.forEach(function (c) {
        var isMatch = c.post_url && d.post_url && c.post_url === d.post_url;
        var s = isMatch ? Number(d.face_similarity)
          : (c.face_similarity != null ? Number(c.face_similarity) : null);
        if (s != null && isFinite(s)) scored.push({ c: c, score: clamp(s, 0, 1) });
      });
      if (!scored.some(function (r) { return r.c.post_url === d.post_url; })) {
        scored.unshift({ c: d, score: clamp(Number(d.face_similarity) || 0, 0, 1) });
      }
      scored.sort(function (a, b) { return b.score - a.score; });

      var dials = $('dials');
      dials.textContent = '';
      $('dials-empty').hidden = scored.length > 0;
      scored.forEach(function (r, i) {
        var card = dialCard(r.c, i);
        dials.appendChild(card);
        later(function () { runDial(card, r.score); }, noMotion() ? 0 : i * 170);
      });

      var total = S.candidates.length;
      var note = $('dials-note');
      note.hidden = false;
      note.textContent = total > scored.length
        ? 'Scored ' + scored.length + ' of ' + total + ' candidates. The rest were ruled out before the face comparison.'
        : 'Scored all ' + scored.length + ' candidate' + (scored.length === 1 ? '' : 's') + '.';

      // Exactly the fields the match event carries — no extras, no rounding,
      // so this record has the same shape as the one the server hashes.
      S.record = {
        face_similarity: Number(d.face_similarity),
        image_sha256: d.image_sha256 || '',
        image_url: d.image_url || '',
        post_url: d.post_url || '',
        source_domain: d.source_domain || '',
        title: d.title || '',
        verified_at: d.verified_at || new Date().toISOString()
      };
    },

    chain: function (d) {
      S.chain = d;
      log('chain', (d.network || 'chain') + ' block ' + (d.block_number == null ? '?' : d.block_number));
      $('seal-empty').hidden = true;
      $('seal-card').hidden = false;
      $('meta-network').textContent = d.network || 'unknown network';

      var canon = renderRecord();
      var ph = d.payload_hash;
      if (ph) { S.hashSource = 'server'; }
      else { ph = demoDigest(canon); S.hashSource = 'demo'; }
      S.chainHash = ph;
      S.localHash = ph;

      $('canon-note').textContent = S.hashSource === 'server'
        ? 'Serialised by this page from the match event. The payload hash below is the keccak256 value the API reported.'
        : 'Demo mode hashes this exact string to produce the payload hash below.';

      typeHash($('payload-hash'), ph);
      $('face-hash').textContent = d.face_hash || demoDigest('face:' + canon);

      $('r-network').textContent = d.network || '—';
      $('r-chain').textContent = d.chain_id == null ? '—' : String(d.chain_id);
      $('r-contract').textContent = d.contract_address || '—';
      $('r-tx').textContent = d.tx_hash || '—';
      $('r-block').textContent = d.block_number == null ? '—' : String(d.block_number);
      var ex = safeUrl(d.explorer_url);
      var a = $('explorer-link');
      if (ex) { a.href = ex; a.textContent = 'open the transaction on the explorer'; a.removeAttribute('aria-disabled'); }
      else { a.removeAttribute('href'); a.textContent = 'no explorer link in this receipt'; a.setAttribute('aria-disabled', 'true'); }

      $('receipt').classList.remove('is-idle');
      sealAnimation();

      $('chainpanel').dataset.state = 'sealed';
      $('chain-state').textContent = 'Sealed on ' + (d.network || 'chain');
      $('chain-note').textContent = 'Block ' + (d.block_number == null ? 'pending' : d.block_number) +
        ', transaction ' + shorten(d.tx_hash || '', 10, 8) + '.';
      $('btn-reverify').disabled = false;
      $('btn-tamper').disabled = false;
      $('chain-hint').textContent = 'Re-verify hashes the local record and compares it with the chain. Tamper test changes one byte first.';
    },

    done: function () {
      S.running = false;
      stopClock();
      log('done', 'pipeline finished');
      closeStream();
    },

    error: function (d) {
      S.running = false;
      stopClock();
      var msg = (d && d.message) || 'the run stopped without a reason';
      log('error', msg, 'bad');
      for (var i = 1; i <= 4; i++) {
        var it = document.querySelector('.rail-item[data-stage="' + i + '"]');
        if (it && it.dataset.status === 'running') setStage(i, 'failed');
      }
      alertShow('The run stopped: ' + msg + ' Fix it on the API side and run again, or use the sample photo to see the interface with demo data.');
      closeStream();
    }
  };


  /* ═══════════════════════════════════════════════════════
     LIVE MODE
     ═══════════════════════════════════════════════════════ */
  function closeStream() {
    if (S.es) { try { S.es.close(); } catch (e) {} S.es = null; }
  }

  function parse(e) {
    try { return JSON.parse(e.data); } catch (err) { return {}; }
  }

  function startLive(imageId) {
    var es;
    try {
      es = new EventSource(API.run + '?image=' + encodeURIComponent(imageId));
    } catch (e) {
      fallbackToDemo('the browser could not open the event stream');
      return;
    }
    S.es = es;
    var got = false, settled = false;

    ['stage', 'face', 'candidates', 'match', 'chain', 'done', 'error'].forEach(function (name) {
      es.addEventListener(name, function (e) {
        got = true; settled = true;
        if (S.mode !== 'live') setMode('live');
        on[name](parse(e));
      });
    });

    es.onerror = function () {
      closeStream();
      if (!got) {
        fallbackToDemo('the run stream did not respond');
      } else if (S.running) {
        on.error({ message: 'the connection to /api/run dropped mid-run.' });
      }
    };

    // no traffic at all inside 2.5s means there is no backend here
    later(function () {
      if (!settled && S.running) { closeStream(); fallbackToDemo('/api/run did not send anything'); }
    }, 2500);
  }

  /* ═══════════════════════════════════════════════════════
     DEMO MODE
     ═══════════════════════════════════════════════════════ */
  function demoEmbedding(seed) {
    var r = rng(seed || 424242), v = new Array(512), prev = 0, i;
    for (i = 0; i < 512; i++) {
      prev = prev * 0.78 + (r() * 2 - 1) * 0.22;                 // low-pass walk
      var wave = Math.sin(i / 512 * Math.PI * 6.5) * 0.34 +
                 Math.sin(i / 17.3 + 1.1) * 0.13 +
                 Math.cos(i / 63.1) * 0.2;
      v[i] = prev * 1.15 + wave * 0.55;
    }
    var n = 0;
    for (i = 0; i < 512; i++) n += v[i] * v[i];
    n = Math.sqrt(n) || 1;
    for (i = 0; i < 512; i++) v[i] = v[i] / n;
    return v;
  }

  function demoCandidates() {
    return [
      { post_url: 'https://instagram.com/p/C8xQ1mLtq2v', source_domain: 'instagram.com',
        title: 'Beach cleanup at Ashwem, morning crew', image_url: '', engine: 'google lens',
        face_similarity: 0.6042 },
      { post_url: 'https://x.com/status/1799455120388',  source_domain: 'x.com',
        title: 'Team photo from the Goa build sprint',  image_url: '', engine: 'bing visual search',
        face_similarity: 0.5133 },
      { post_url: 'https://linkedin.com/posts/activity-7203994411', source_domain: 'linkedin.com',
        title: 'Speaker line-up announcement',          image_url: '', engine: 'yandex images',
        face_similarity: 0.3120 },
      { post_url: 'https://reddit.com/r/goa/comments/1d9k2af', source_domain: 'reddit.com',
        title: 'Anyone recognise this photo from Anjuna?', image_url: '', engine: 'tineye',
        face_similarity: 0.1985 }
    ];
  }

  function runDemo() {
    var r = rng(0x600a2026);
    var cands = demoCandidates();
    var emb = demoEmbedding(0xfa3e);
    var tx = '0x' + hexBytes(r, 32);
    var contract = '0x' + hexBytes(r, 20);
    var sha = hexBytes(r, 32);
    var q = [];
    // Reduced motion keeps the stage sequence, just compressed: the
    // animations are already off, so the long choreography has no purpose.
    function at(ms, fn) { q.push(later(fn, noMotion() ? Math.round(ms * 0.3) : ms)); }

    at(60,   function () { on.stage({ n: 1, name: 'scan', status: 'running' }); });
    at(420,  function () {
      on.face({
        bbox: [S.natural.w * 0.27, S.natural.h * 0.13, S.natural.w * 0.46, S.natural.h * 0.42],
        confidence: 0.9871, embedding: emb, crop_url: ''
      });
    });
    at(2900, function () { on.stage({ n: 1, name: 'scan', status: 'done' }); on.stage({ n: 2, name: 'search', status: 'running' }); });
    at(3600, function () { on.candidates({ items: cands }); });
    at(4500, function () { on.stage({ n: 2, name: 'search', status: 'done' }); on.stage({ n: 3, name: 'verify', status: 'running' }); });
    at(4900, function () {
      on.match({
        post_url: cands[0].post_url, source_domain: cands[0].source_domain, title: cands[0].title,
        image_url: '', image_sha256: sha, face_similarity: 0.6042,
        verified_at: new Date().toISOString()
      });
    });
    at(6300, function () { on.stage({ n: 3, name: 'verify', status: 'done' }); on.stage({ n: 4, name: 'seal', status: 'running' }); });
    at(6900, function () {
      on.chain({
        network: 'base-sepolia', chain_id: 84532,
        contract_address: contract, tx_hash: tx, block_number: 12894431,
        explorer_url: 'https://sepolia.basescan.org/tx/' + tx,
        payload_hash: '', face_hash: ''
      });
    });
    at(8400, function () { on.stage({ n: 4, name: 'seal', status: 'done' }); on.done({}); });
  }

  function fallbackToDemo(why) {
    if (S.mode === 'demo') return;
    setMode('demo', why);
    alertShow('The faceproof API is not answering (' + why + '), so this run shows demo data generated in your browser. Start the API and run again for real results.');
    runDemo();
  }

  /* ═══════════════════════════════════════════════════════
     RUN CONTROL
     ═══════════════════════════════════════════════════════ */
  function resetRun() {
    clearTimers(); closeStream(); stopClock(); alertHide();
    S.mode = null; S.hashSource = null; S.imageId = null; S.face = null;
    S.candidates = []; S.match = null; S.chain = null; S.record = null;
    S.chainHash = null; S.localHash = null; S.mutation = null;
    S.tampered = false; S.running = false;

    document.body.classList.remove('is-tampered');
    $('mode-badge').dataset.mode = 'standby';
    $('mode-text').textContent = 'Standby';
    $('fact-mode').textContent = 'standby';
    $('fact-sim').textContent = '—';
    $('fact-elapsed').textContent = '—';
    $('meta-network').textContent = 'not sealed';
    for (var i = 1; i <= 4; i++) setStage(i, 'pending', 'Waiting');

    $('drop').hidden = false;
    $('scan-out').hidden = true;
    $('embed-wrap').hidden = true;
    frame.classList.remove('is-scanning');
    bbox.classList.remove('ticks-in');
    bpath.setAttribute('d', '');
    $('strip').textContent = '';
    $('embed-stats').textContent = '';
    ['face-conf', 'face-box', 'face-dim', 'face-embdim', 'face-norm'].forEach(function (id) { $(id).textContent = '—'; });
    $('crop').style.backgroundImage = '';

    $('cards').textContent = ''; $('cards-empty').hidden = false;
    $('dials').textContent = ''; $('dials-empty').hidden = false;
    $('seal-card').hidden = true; $('seal-empty').hidden = false;
    $('receipt').classList.add('is-idle');
    $('break-banner').hidden = true;
    $('sigil').classList.remove('s-draw', 's-spin', 's-lock');

    $('chainpanel').dataset.state = 'idle';
    $('chain-state').textContent = 'Not sealed yet';
    $('chain-note').textContent = 'Run the pipeline, then re-check the record against the chain at any time.';
    $('chain-hint').textContent = 'Both buttons unlock once stage 4 seals a record.';
    $('btn-reverify').disabled = true;
    $('btn-tamper').disabled = true;
    $('btn-restore').hidden = true;
    $('diff').hidden = true;
    $('mutation-note').textContent = '';
    logBox.textContent = '';
  }

  function beginRun() {
    S.running = true;
    startClock();
    setStage(1, 'running');
    log('run', 'started');
  }

  async function runWithFile(file) {
    if (!file) return;
    if (!/^image\//.test(file.type)) {
      alertShow('That file is not an image. Choose a JPEG, PNG or WebP.');
      return;
    }
    if (file.size > 25 * 1024 * 1024) {
      alertShow('That image is larger than 25 MB. Export a smaller copy and try again.');
      return;
    }
    resetRun();
    var localUrl = URL.createObjectURL(file);
    await showPhoto(localUrl, 'The uploaded photo, being scanned for a face');
    beginRun();

    var fd = new FormData();
    fd.append('file', file, file.name || 'upload');
    var res, data;
    try {
      res = await fetch(API.upload, { method: 'POST', body: fd });
      if (!res.ok) throw new Error('HTTP ' + res.status);
      data = await res.json();
    } catch (e) {
      fallbackToDemo('POST /api/upload failed');
      return;
    }
    S.imageId = data.image_id;
    if (!S.imageId) { fallbackToDemo('/api/upload returned no image_id'); return; }
    var served = safeUrl(data.url);
    if (served) await showPhoto(served, 'The uploaded photo, being scanned for a face');
    setMode('live');
    log('upload', 'image_id ' + S.imageId);
    startLive(S.imageId);
  }

  async function runWithSample() {
    resetRun();
    await showPhoto(samplePhoto(), 'A generated sample photo used for the demo run');
    beginRun();
    setMode('demo', 'sample photo, no upload attempted');
    log('sample', 'generated subject, demo timeline');
    runDemo();
  }

  /* ── drop zone wiring ─────────────────────────────────── */
  var drop = $('drop'), fileInput = $('file');
  $('btn-choose').addEventListener('click', function () { fileInput.click(); });
  $('btn-sample').addEventListener('click', runWithSample);
  fileInput.addEventListener('change', function () {
    if (fileInput.files && fileInput.files[0]) runWithFile(fileInput.files[0]);
    fileInput.value = '';
  });
  ['dragenter', 'dragover'].forEach(function (n) {
    drop.addEventListener(n, function (e) { e.preventDefault(); drop.classList.add('is-over'); });
  });
  ['dragleave', 'dragend'].forEach(function (n) {
    drop.addEventListener(n, function () { drop.classList.remove('is-over'); });
  });
  drop.addEventListener('drop', function (e) {
    e.preventDefault();
    drop.classList.remove('is-over');
    var f = e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files[0];
    if (f) runWithFile(f);
  });
  window.addEventListener('paste', function (e) {
    if (!e.clipboardData) return;
    var items = e.clipboardData.files;
    if (items && items[0] && /^image\//.test(items[0].type)) runWithFile(items[0]);
  });

  /* ── rail navigation ──────────────────────────────────── */
  document.querySelectorAll('[data-goto]').forEach(function (b) {
    b.addEventListener('click', function () {
      var t = $(b.dataset.goto);
      if (t) {
        t.scrollIntoView({ behavior: noMotion() ? 'auto' : 'smooth', block: 'start' });
        t.querySelector('.stage-name').setAttribute('tabindex', '-1');
        t.querySelector('.stage-name').focus({ preventScroll: true });
      }
    });
  });

  /* ── copy JSON ────────────────────────────────────────── */
  $('btn-copy').addEventListener('click', async function () {
    var btn = $('btn-copy');
    if (!S.record) return;
    try {
      await navigator.clipboard.writeText(canonicalize(S.record));
      btn.textContent = 'Copied';
    } catch (e) {
      btn.textContent = 'Copy blocked';
      alertShow('The browser blocked clipboard access. Select the JSON block and copy it by hand.');
    }
    later(function () { btn.textContent = 'Copy JSON'; }, 1600);
  });

  /* ═══════════════════════════════════════════════════════
     VERIFY / TAMPER / RESTORE
     ═══════════════════════════════════════════════════════ */
  /* The API answers verify with
       {pass, local_hash, onchain:{payload_hash, face_hash, ...}}
     and tamper with
       {pass, local_hash, onchain_hash, diff_index}
     Older/other field names are still accepted. */
  function readVerdict(d) {
    d = d || {};
    var ok = d.pass;
    if (ok == null) ok = d.verified;
    if (ok == null) ok = d.ok;
    if (ok == null) ok = d.match;
    var oc = d.onchain || {};
    return {
      ok: !!ok,
      chain: d.onchain_hash || oc.payload_hash || d.chain_hash || d.expected_payload_hash || null,
      local: d.local_hash || d.payload_hash || d.computed_payload_hash || null,
      block: d.block_number != null ? d.block_number : oc.block_number,
      diffIndex: d.diff_index != null ? d.diff_index : null,
      mutation: d.mutation || d.mutated || null
    };
  }

  function passState(block) {
    var p = $('chainpanel');
    p.dataset.state = 'pass';
    document.body.classList.remove('is-tampered');
    $('break-banner').hidden = true;
    $('chain-state').textContent = 'Record matches the chain';
    $('chain-note').textContent = 'The local record hashes to the value stored in block ' +
      (block == null ? (S.chain && S.chain.block_number) : block) + '.';
    $('diff').hidden = true;
    log('verify', 'pass');
  }

  function breakState(mutationText) {
    S.tampered = true;
    document.body.classList.add('is-tampered');
    $('chainpanel').dataset.state = 'broken';
    $('chain-state').textContent = 'Tamper detected';
    // The API mutates a copy and leaves the stored record alone; demo mode
    // really does mutate the record held in this browser.
    var live = S.mode === 'live';
    $('chain-note').textContent = live
      ? 'The test ran against a mutated copy. The stored record was not changed, so re-verify still passes.'
      : 'Stop trusting this copy of the record. Re-fetch it from the source, or restore it below.';
    $('btn-restore').textContent = live ? 'Clear the tamper test' : 'Restore the record';
    $('btn-restore').hidden = false;
    // a second mutation of the same byte would quietly undo the first
    $('btn-tamper').disabled = true;
    $('chain-hint').textContent = live
      ? 'Clear the test to run it again.'
      : 'Restore the record to run the tamper test again.';
    $('break-banner').hidden = false;
    $('break-sub').textContent = mutationText;
    // escape first: in live mode this string comes from the API
    $('mutation-note').innerHTML = esc(mutationText)
      .replace(/(0x[0-9a-f…]+|[0-9a-f]{2} → [0-9a-f]{2})/gi, '<b>$1</b>');
    renderDiff(S.chainHash, S.localHash);
    setStage(4, 'failed', 'Broken');
    var fl = $('flash');
    fl.classList.remove('on'); void fl.getBoundingClientRect(); fl.classList.add('on');
    var bb = $('break-banner');
    bb.style.animation = 'none'; void bb.getBoundingClientRect(); bb.style.animation = '';
    log('tamper', mutationText, 'bad');
  }

  async function post(url) {
    var res = await fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ image_id: S.imageId })
    });
    if (!res.ok) throw new Error('HTTP ' + res.status);
    return res.json();
  }

  $('btn-reverify').addEventListener('click', async function () {
    var btn = this;
    if (!S.record) return;
    btn.disabled = true;
    var before = btn.textContent;
    btn.textContent = 'Checking…';
    try {
      if (S.mode !== 'live') throw new Error('demo');
      var v = readVerdict(await post(API.verify));
      if (v.chain) S.chainHash = v.chain;
      if (v.local) S.localHash = v.local;
      if (v.ok) passState(v.block);
      else breakState('The API reports that the local record no longer hashes to the on-chain value.');
    } catch (e) {
      if (S.mode === 'live') {
        alertShow('POST /api/verify did not answer, so the record was not re-checked. Confirm the API is running and try again.');
        log('verify', 'endpoint unreachable', 'bad');
      } else {
        // demo: compare the hashes we hold locally
        S.localHash = demoDigest(canonicalize(S.record));
        if (S.localHash === S.chainHash) passState(S.chain && S.chain.block_number);
        else breakState('The local record hashes to ' + shorten(S.localHash, 12, 8) +
          ' but the chain holds ' + shorten(S.chainHash, 12, 8) + '.');
      }
    }
    btn.textContent = before;
    btn.disabled = false;
  });

  $('btn-tamper').addEventListener('click', async function () {
    var btn = this;
    if (!S.record) return;
    btn.disabled = true;
    var before = btn.textContent;
    btn.textContent = 'Mutating…';
    try {
      if (S.mode !== 'live') throw new Error('demo');
      var v = readVerdict(await post(API.tamper));
      if (v.chain) S.chainHash = v.chain;
      if (v.local) S.localHash = v.local;
      var m = v.mutation;
      var txt = m && m.field
        ? 'One byte of ' + m.field + ' was changed from ' + m.from + ' to ' + m.to + '. The payload hash no longer matches the chain.'
        : (v.diffIndex != null
            ? 'The API changed one byte of the local record. The payload now differs from the sealed copy at byte ' + v.diffIndex + '.'
            : 'One byte of the local record was changed. The payload hash no longer matches the chain.');
      if (m && m.field === 'image_sha256' && m.to) {
        S.mutation = { field: m.field, from: m.from, to: String(S.record.image_sha256 || '').replace(m.from, m.to) };
      }
      if (m && m.field && m.field in S.record && m.to) {
        S.record[m.field] = String(S.record[m.field]).replace(m.from, m.to);
        S.mutation = { field: m.field, to: m.to };
        renderRecord();
      }
      breakState(txt);
    } catch (e) {
      if (S.mode === 'live') {
        alertShow('POST /api/tamper did not answer, so the record was left alone. Nothing on screen was changed.');
        log('tamper', 'endpoint unreachable', 'bad');
      } else {
        localTamper();
      }
    }
    btn.textContent = before;
    btn.disabled = S.tampered;   // stays locked until the record is restored
  });

  function localTamper() {
    // Flip exactly one byte of image_sha256 in the local copy.
    var sha = String(S.record.image_sha256 || '');
    if (sha.length < 4) {
      sha = hexBytes(rng(1), 32);
      S.record.image_sha256 = sha;
    }
    var pos = 30;                                  // byte 15, chars 30-31
    var oldByte = sha.slice(pos, pos + 2);
    var val = parseInt(oldByte, 16);
    var newByte = ((val ^ 0x5a) & 0xff).toString(16).padStart(2, '0');
    S.record.image_sha256 = sha.slice(0, pos) + newByte + sha.slice(pos + 2);
    S.mutation = { field: 'image_sha256', from: oldByte, to: newByte, index: pos / 2 };
    S.localHash = demoDigest(canonicalize(S.record));
    renderRecord();
    breakState('image_sha256 byte ' + (pos / 2) + ' changed: ' + oldByte + ' → ' + newByte +
      '. Keccak256 of the record moved from ' + shorten(S.chainHash, 12, 6) + ' to ' + shorten(S.localHash, 12, 6) + '.');
  }

  $('btn-restore').addEventListener('click', function () {
    if (!S.match) return;
    S.record.image_sha256 = S.match.image_sha256 || S.record.image_sha256;
    if (S.mutation && S.mutation.from && S.mutation.to) {
      S.record[S.mutation.field] = String(S.record[S.mutation.field]).replace(S.mutation.to, S.mutation.from);
    }
    S.mutation = null;
    S.tampered = false;
    S.localHash = S.chainHash;
    document.body.classList.remove('is-tampered');
    renderRecord();
    $('break-banner').hidden = true;
    $('diff').hidden = true;
    $('btn-restore').hidden = true;
    $('btn-tamper').disabled = false;
    $('chain-hint').textContent = 'Re-verify hashes the local record and compares it with the chain. Tamper test changes one byte first.';
    $('mutation-note').textContent = '';
    setStage(4, 'done', 'Done');
    $('chainpanel').dataset.state = 'sealed';
    $('chain-state').textContent = 'Sealed on ' + ((S.chain && S.chain.network) || 'chain');
    $('chain-note').textContent = S.mode === 'live'
      ? 'Tamper test cleared. Re-verify to check the stored record against the chain.'
      : 'Local record restored. Re-verify to confirm it matches the chain again.';
    $('sigil').classList.add('s-draw', 's-spin', 's-lock');
    $('sigil-state').textContent = 'Sealed';
    log('restore', 'local record restored');
  });

  $('btn-reset').addEventListener('click', function () {
    resetRun();
    document.getElementById('stage-1').scrollIntoView({ behavior: noMotion() ? 'auto' : 'smooth', block: 'start' });
  });

  /* ── init ─────────────────────────────────────────────── */
  $('thr-text').textContent = f4(THRESHOLD);
  $('fact-threshold').textContent = f4(THRESHOLD);
  $('receipt').classList.add('is-idle');
  for (var k = 1; k <= 4; k++) setStage(k, 'pending', 'Waiting');
  log('ready', 'waiting for a photo');
})();
