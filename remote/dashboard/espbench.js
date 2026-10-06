/*
 * espbench.js — lógica del dashboard sin DOM: formato, salud del device,
 * clasificación de líneas del log, el buffer incremental de la terminal,
 * reservas ("vence en") y eventos (filas, contexto, marcas en el vivo).
 *
 * Se carga en el navegador como window.EB y en node con require() para los
 * tests (tests/js/test_espbench.js). Nada de acá toca el DOM.
 */
(function (root, factory) {
    if (typeof module === 'object' && module.exports) module.exports = factory();
    else root.EB = factory();
})(this, function () {
    'use strict';

    function escapeHtml(s) {
        return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
                        .replace(/"/g, '&quot;');
    }

    // ── Tiempo y tamaños ──────────────────────────────────────────────────

    // Los timestamps del server son hora local sin zona ("2026-10-05T16:00:00"),
    // salvo los que traen offset (lock_expires: "...-03:00" o "Z"), que se
    // respetan aunque el navegador esté en otra zona que la Pi.
    function parseLocal(iso) {
        if (!iso) return null;
        var m = /^(\d{4})-(\d\d)-(\d\d)[T ](\d\d):(\d\d):(\d\d)(?:\.\d+)?(Z|[+-]\d\d:?\d\d)?$/.exec(iso) ||
                /^(\d{4})-(\d\d)-(\d\d)[T ](\d\d):(\d\d):(\d\d)/.exec(iso);
        if (!m) return null;
        if (!m[7]) return new Date(+m[1], +m[2] - 1, +m[3], +m[4], +m[5], +m[6]);
        var off = 0;
        if (m[7] !== 'Z') {
            var z = m[7].replace(':', '');
            off = (z.charAt(0) === '-' ? -1 : 1) * (+z.slice(1, 3) * 60 + +z.slice(3, 5));
        }
        return new Date(Date.UTC(+m[1], +m[2] - 1, +m[3], +m[4], +m[5], +m[6]) - off * 60000);
    }

    function relTime(iso, now) {
        var d = parseLocal(iso);
        if (!d) return null;
        var s = Math.round(((now === undefined ? Date.now() : now) - d.getTime()) / 1000);
        if (s < 45) return 'recién';
        var m = Math.round(s / 60);
        if (m < 60) return 'hace ' + m + ' min';
        var h = Math.round(m / 60);
        if (h < 24) return 'hace ' + h + ' h';
        var days = Math.round(h / 24);
        if (days < 30) return 'hace ' + days + ' d';
        return iso.slice(0, 10);
    }

    function pad2(n) { return n < 10 ? '0' + n : '' + n; }

    // Epoch en segundos → "YYYY-MM-DDTHH:MM:SS" en hora local (como los del server).
    function isoLocal(epochSec) {
        var d = new Date(epochSec * 1000);
        return d.getFullYear() + '-' + pad2(d.getMonth() + 1) + '-' + pad2(d.getDate()) + 'T' +
               pad2(d.getHours()) + ':' + pad2(d.getMinutes()) + ':' + pad2(d.getSeconds());
    }

    // output_<YYYYMMDD_HHMMSS_pid>.log → inicio de esa sesión (ISO local), o null.
    // Los logs viejos (output_<ts>.log, sin pid) llevaban la hora de rotación: null.
    function sessionStart(name) {
        var m = /^output_(\d{4})(\d\d)(\d\d)_(\d\d)(\d\d)(\d\d)_\d+\.log$/.exec(name);
        return m ? m[1] + '-' + m[2] + '-' + m[3] + 'T' + m[4] + ':' + m[5] + ':' + m[6] : null;
    }

    function fmtBytes(n) {
        if (n < 1024) return n + ' B';
        if (n < 1024 * 1024) return (n / 1024).toFixed(1) + ' KB';
        return (n / 1024 / 1024).toFixed(1) + ' MB';
    }

    // ── Salud del device (SerialWatch → run/<tty>.json → /api/devices) ────

    var PANIC_KINDS = {
        guru: 'Guru Meditation', abort: 'abort()', brownout: 'brownout',
        task_wdt: 'task watchdog', stack_overflow: 'stack overflow', assert: 'assert'
    };

    // Lista de badges {cls, text, title}, de más grave a menos. Vacía = todo bien.
    function healthBadges(h) {
        var out = [];
        if (!h) return out;
        if (h.boot_loop) {
            out.push({cls: 'hb-loop', text: 'BOOT LOOP',
                      title: 'Reinicia en loop: ' + h.boots + ' arranques en poco tiempo'});
        }
        if (h.panics > 0) {
            var p = h.last_panic || {};
            var what = (PANIC_KINDS[p.kind] || p.kind || 'panic') + (p.detail ? ' (' + p.detail + ')' : '');
            out.push({cls: 'hb-panic', text: '⚠ ' + h.panics + (h.panics === 1 ? ' panic' : ' panics'),
                      title: 'Último: ' + what + (p.ts ? ' — ' + p.ts.replace('T', ' ') : '')});
        }
        var r = h.last_reset;
        if (r && r.abnormal && !h.boot_loop) {
            out.push({cls: 'hb-warn', text: '↯ ' + r.reason,
                      title: 'Último reset anormal' + (r.ts ? ' — ' + r.ts.replace('T', ' ') : '')});
        }
        if (h.boots > 1 && !h.boot_loop) {
            out.push({cls: 'hb-info', text: '↻ ' + (h.boots - 1),
                      title: (h.boots - 1) + ' reinicios desde ' + (h.since || '').replace('T', ' ')});
        }
        return out;
    }

    // Peor nivel de salud: 'bad' | 'warn' | 'ok'. Para el color de la card.
    function healthLevel(h) {
        if (!h) return 'ok';
        if (h.boot_loop || h.panics > 0) return 'bad';
        if (h.last_reset && h.last_reset.abnormal) return 'warn';
        return 'ok';
    }

    // ── Líneas del log ────────────────────────────────────────────────────

    var ANSI_RE = /\x1b\[[0-9;]*[A-Za-z]/g;

    function stripAnsi(s) { return s.replace(ANSI_RE, ''); }

    // Prefijo que pone DeviceLog a cada línea del archivo:
    // "2026-10-05 16:02:03.123 > " — origen: > serial, | taglog, ↪ continuación
    // de una línea serial que salió partida. Los logs viejos no lo tienen.
    var PREFIX_RE = /^(\d{4}-\d\d-\d\d) (\d\d:\d\d:\d\d\.\d{3}) ([>|\u21aa]) /;

    // {date, time, origin, prefix, body}. Sin prefijo: date/time/origin null, body = línea.
    function splitPrefix(line) {
        var m = PREFIX_RE.exec(line);
        if (!m) return {date: null, time: null, origin: null, prefix: '', body: line};
        return {date: m[1], time: m[2], origin: m[3], prefix: m[0], body: line.slice(m[0].length)};
    }

    // Taglog con prefijo: el cuerpo es "WARN  | protocol       | msg".
    var TAGLOG_BODY_RE = /^(INFO|WARN|ERROR|DEBUG)\s*\| /;
    // Taglog de un log viejo (taglog.format_line): "2026-10-05 16:00:00 | WARN  | protocol       | msg"
    var TAGLOG_RE = /^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d \| (INFO|WARN|ERROR|DEBUG)\s*\| /;
    var PANIC_RE = new RegExp([
        'Guru Meditation Error', 'abort\\(\\) was called', 'Brownout detector was triggered',
        'Task watchdog got triggered', 'stack overflow in task', 'assert failed:',
        '^Backtrace:', 'Rebooting\\.\\.\\.$', '^rst:0x[0-9a-f]+ \\((?:[A-Z0-9_]*(?:WDT|BROWN|PANIC)[A-Z0-9_]*)\\)'
    ].join('|'));
    var RESET_RE = /^rst:0x[0-9a-f]+ \(/;
    var ESP_LEVEL_RE = /^([EW]) \(\d+\) /;   // ESP_LOGE / ESP_LOGW

    // Clase CSS de una línea (ya sin ANSI, con o sin prefijo), o '' si no lleva.
    // Las regex de abajo están ancladas con ^: se aplican al cuerpo.
    function lineClass(plain) {
        var p = splitPrefix(plain);
        var body = p.body;
        var m = p.origin === '|' ? TAGLOG_BODY_RE.exec(body) : p.origin === null ? TAGLOG_RE.exec(body) : null;
        if (m) return 'ln-tl ln-tl-' + m[1].toLowerCase();
        if (PANIC_RE.test(body)) return 'ln-panic';
        if (RESET_RE.test(body)) return 'ln-reset';
        var e = ESP_LEVEL_RE.exec(body);
        if (e) return e[1] === 'E' ? 'ln-esp-e' : 'ln-esp-w';
        return '';
    }

    // Líneas que muestra el filtro "solo problemas".
    var PROBLEM_RE = /\b(ln-panic|ln-reset|ln-tl-warn|ln-tl-error|ln-esp-e|ln-esp-w)\b/;
    function isProblem(cls) { return PROBLEM_RE.test(cls); }

    var ANSI_COLORS = {
        30: '#555555', 31: '#f85149', 32: '#3fb950', 33: '#d29922',
        34: '#388bfd', 35: '#bc8cff', 36: '#39c5cf', 37: '#b1bac4',
        90: '#6e7681', 91: '#ff7b72', 92: '#56d364', 93: '#e3b341',
        94: '#79c0ff', 95: '#d2a8ff', 96: '#56d4dd', 97: '#f0f6fc'
    };

    // Una línea con colores SGR → HTML. ESP-IDF resetea el color al final de
    // cada línea, así que se convierte línea por línea sin arrastrar estado.
    function ansiLineToHtml(line) {
        var out = '', open = false;
        var parts = line.split(/\x1b\[([0-9;]*)m/);
        for (var i = 0; i < parts.length; i++) {
            if (i % 2 === 0) {
                out += escapeHtml(stripAnsi(parts[i]));
                continue;
            }
            if (open) { out += '</span>'; open = false; }
            var codes = parts[i] === '' ? [0] : parts[i].split(';').map(Number);
            var style = '';
            for (var j = 0; j < codes.length; j++) {
                var c = codes[j];
                if (c === 0) style = '';
                else if (c === 1) style += 'font-weight:600;';
                else if (ANSI_COLORS[c]) style += 'color:' + ANSI_COLORS[c] + ';';
            }
            if (style) { out += '<span style="' + style + '">'; open = true; }
        }
        if (open) out += '</span>';
        return out;
    }

    // \r suelto = sobrescribir la línea (barras de progreso): queda lo último.
    // Se aplica al cuerpo: el prefijo de la línea se conserva.
    function overwrite(line) {
        var p = splitPrefix(line);
        var i = p.body.lastIndexOf('\r');
        return i >= 0 ? p.prefix + p.body.slice(i + 1) : line;
    }

    /*
     * Buffer incremental de la terminal. push(texto) devuelve las líneas que se
     * completaron con ese chunk; `partial` es la línea en curso (sin \n todavía).
     * Antes se re-renderizaba el log entero en cada mensaje del WebSocket: O(n²)
     * y la página se colgaba con sesiones largas.
     */
    function LineBuffer() {
        this.partial = '';
        this._held = '';      // \r al final de un chunk: puede ser la mitad de un \r\n
    }

    LineBuffer.prototype.push = function (text) {
        var buf = this.partial + this._held + text;
        this._held = '';
        if (buf.charAt(buf.length - 1) === '\r') {
            var k = buf.length;
            while (k > 0 && buf.charAt(k - 1) === '\r') k--;
            this._held = buf.slice(k);
            buf = buf.slice(0, k);
        }
        buf = buf.replace(/\r+\n/g, '\n');
        var parts = buf.split('\n');
        this.partial = parts.pop();
        return parts.map(overwrite);
    };

    // Lo que se muestra de la línea en curso.
    LineBuffer.prototype.partialView = function () { return overwrite(this.partial); };

    // ── Errores del API ───────────────────────────────────────────────────

    // Texto de un error de FastAPI: {detail: "texto"} o {detail: {error, message}}.
    function errorText(body, status) {
        var d = body && body.detail;
        if (typeof d === 'string') return d;
        if (d && (d.message || d.error)) return d.message || d.error;
        if (body && body.error) return body.message || body.error;
        return 'HTTP ' + status;
    }

    // Código estable del error ({detail: {error}}), o null.
    function errorCode(body) {
        var d = body && body.detail;
        return d && typeof d === 'object' && d.error ? d.error : null;
    }

    // Opciones de fetch con `Authorization: Bearer <token>` (sin token, igual).
    function withToken(opts, token) {
        var out = Object.assign({}, opts || {});
        if (!token) return out;
        out.headers = Object.assign({}, out.headers || {}, {'Authorization': 'Bearer ' + token});
        return out;
    }

    // ── Reservas y locks (/api/devices: lock_user, lock_expires) ──────────

    // Duración corta: "45 s", "20 min", "2 h 5 min", "3 d 4 h".
    function fmtDur(sec) {
        sec = Math.max(0, Math.round(sec));
        if (sec < 60) return sec + ' s';
        var m = Math.floor(sec / 60);
        if (m < 60) return m + ' min';
        var h = Math.floor(m / 60);
        if (h < 24) return h + ' h' + (m % 60 ? ' ' + (m % 60) + ' min' : '');
        var d = Math.floor(h / 24);
        return d + ' d' + (h % 24 ? ' ' + (h % 24) + ' h' : '');
    }

    // Segundos hasta un ISO local del server (negativo si ya pasó), o null.
    function secondsUntil(iso, now) {
        var d = parseLocal(iso);
        if (!d) return null;
        return (d.getTime() - (now === undefined ? Date.now() : now)) / 1000;
    }

    // "vence en 20 min", o null si ya venció (vencida = inexistente, como en el server).
    function expiresText(iso, now) {
        var s = secondsUntil(iso, now);
        return s === null || s <= 0 ? null : 'vence en ' + fmtDur(s);
    }

    /*
     * Lock de un device para mostrar, o null si no hay (o la reserva ya venció:
     * el server la ignora, el dashboard también aunque todavía no haya repolleado).
     * reservation: tiene vencimiento (reserve); si no, es el lock del flash.
     */
    function lockInfo(d, now) {
        if (!d || !d.lock_user) return null;
        if (!d.lock_expires && !d.lock_expires_epoch) {
            return {user: d.lock_user, reservation: false, expires: null, until: null, text: 'sin vencimiento',
                    title: 'Lock del flash de ' + d.lock_user + ' (sin vencimiento; no bloquea la consola)'};
        }
        // epoch si está (no depende de zonas); si no, el ISO (con offset o local)
        var expires = d.lock_expires_epoch ? isoLocal(d.lock_expires_epoch) : d.lock_expires;
        var text = expiresText(expires, now);
        if (!text) return null;
        var until = isoLocal(parseLocal(expires).getTime() / 1000);   // hora del navegador
        return {user: d.lock_user, reservation: true, expires: expires, until: until, text: text,
                title: 'Reservada por ' + d.lock_user + ' hasta ' + until.replace('T', ' ')};
    }

    // Texto del confirm antes de forzar (escritura con 423, o "forzar" la reserva).
    function forceConfirmText(lock, action, fallback) {
        var who;
        if (!lock) who = fallback || 'La placa está reservada por otro usuario.';
        else if (lock.reservation) who = 'La placa está reservada por ' + lock.user + ' (' + lock.text +
                                         ', hasta ' + lock.until.slice(11, 19) + ').';
        else who = 'La placa tiene el lock del flash de ' + lock.user + ' (sin vencimiento).';
        return who + '\n¿' + action + ' igual? Queda registrado como forzado.';
    }

    // Búsqueda de la home: texto libre, "@usuario" (solo el usuario del lock;
    // "@" solo = cualquier placa con lock) o "lock:reserva" / "lock:flash" (los
    // contadores del header). lockKind: 'reserva' | 'flash' | ''.
    function searchMatch(q, text, lockUser, lockKind) {
        q = (q || '').trim().toLowerCase();
        if (!q) return true;
        if (q === 'lock:reserva' || q === 'lock:flash') return lockKind === q.slice(5);
        if (q.charAt(0) === '@') return !!lockUser && lockUser.toLowerCase().indexOf(q.slice(1)) >= 0;
        return (text || '').indexOf(q) >= 0;
    }

    // ── Eventos (/api/board/{key}/events) ─────────────────────────────────

    var EVENT_TYPES = {
        panic:     {icon: '⚠', label: 'panic'},
        boot_loop: {icon: '∞', label: 'boot loop'},
        boot:      {icon: '↻', label: 'boot'},
        flash:     {icon: '⚡', label: 'flash'},
        fw:        {icon: '◆', label: 'firmware'},
        send:      {icon: '›', label: 'send'},
        command:   {icon: '⌘', label: 'comando'},
        reserve:   {icon: '🔒', label: 'reserva'},
        release:   {icon: '🔓', label: 'libera'},
        state:     {icon: '⇄', label: 'estado'},
        session:   {icon: '●', label: 'sesión'}
    };
    var EVENT_ORDER = Object.keys(EVENT_TYPES);

    var STATE_NAMES = {monitoring: 'monitoreando', flashing: 'flasheando', erasing: 'borrando',
                       discovering: 'iniciando', unknown: 'sin MAC', disconnected: 'desconectado'};

    // "dash (10.0.0.3)": quién forzó (lock_user del pedido y host), o ''.
    function forcedBy(d) {
        if (!d.by_user && !d.by_host) return '';
        return d.by_user ? d.by_user + (d.by_host ? ' (' + d.by_host + ')' : '') : d.by_host;
    }

    var COMMAND_NAMES = {'restart-session': 'reiniciar sesión'};

    function eventDetail(ev) {
        var d = ev.detail || {};
        var forced = d.forced ? ' · forzado' + (forcedBy(d) ? ' por ' + forcedBy(d) : '') : '';
        switch (ev.type) {
        case 'boot': return (d.reason || '') + (d.abnormal ? ' ⚠' : '');
        case 'panic': return (PANIC_KINDS[d.kind] || d.kind || 'panic') + (d.reason ? ' (' + d.reason + ')' : '');
        case 'boot_loop': return (d.phase === 'end' ? 'fin' : 'inicio') + (d.boots ? ' · ' + d.boots + ' arranques' : '');
        case 'fw': return [d.project, d.version].filter(Boolean).join(' ') + (d.idf ? ' · IDF ' + d.idf : '');
        case 'state': return (STATE_NAMES[d.from] || d.from || '?') + ' → ' + (STATE_NAMES[d.to] || d.to || '?');
        case 'flash': return d.ok ? '✓ ' + (d.status || 'ok') : '✗ ' + String(d.error || d.status || 'falló').replace(/_/g, ' ');
        case 'send': return '"' + (d.text || '') + '"' + (d.enter ? ' ⏎' : '') + forced;
        case 'command': return (COMMAND_NAMES[d.command] || d.command || '') + forced;
        case 'reserve': return d.expires ? 'hasta ' + d.expires.replace('T', ' ').slice(0, 16) : '';
        case 'release': return d.forced ? 'de ' + (d.user || '?') + ' · forzada' +
                                          (forcedBy(d) ? ' por ' + forcedBy(d) : '') : '';
        case 'session': return [d.tty, d.pid ? 'pid ' + d.pid : ''].filter(Boolean).join(' · ');
        }
        return '';
    }

    // c:<sesión>:<offset> → sesión, o null.
    function cursorSession(cursor) {
        var m = /^c:(\d{8}_\d{6}_\d+):\d+$/.exec(cursor || '');
        return m ? m[1] : null;
    }

    // 20261005_160203_812 → "2026-10-05 16:02:03"
    function sessionLabel(sid) {
        var m = /^(\d{4})(\d\d)(\d\d)_(\d\d)(\d\d)(\d\d)/.exec(sid || '');
        return m ? m[1] + '-' + m[2] + '-' + m[3] + ' ' + m[4] + ':' + m[5] + ':' + m[6] : (sid || '');
    }

    // Archivo de la sesión de un cursor (para /api/device/{tty}/sessions/{name}).
    function sessionFile(sid, current) {
        return !sid || sid === current ? 'output.log' : 'output_' + sid + '.log';
    }

    // Lo que muestra una fila de la pestaña Eventos.
    function eventView(ev) {
        var t = EVENT_TYPES[ev.type] || {icon: '·', label: ev.type};
        var d = ev.detail || {};
        var ts = ev.ts || '';
        var detail = eventDetail(ev);
        var full = ev.type === 'panic' && d.line ? d.line : detail;
        return {
            icon: t.icon, label: t.label, cls: 'ev-' + safeType(ev.type).replace(/_/g, '-'),
            date: ts.slice(0, 10), time: ts.slice(11, 19), detail: detail,
            // release forzado: user es el dueño anterior; "quién" es el que forzó
            who: (ev.type === 'release' && d.forced ? d.by_user || d.by_host : d.user) || '',
            session: cursorSession(ev.cursor), cursor: ev.cursor || null,
            bad: ev.type === 'panic' || ev.type === 'boot_loop' || (ev.type === 'flash' && d.ok === false) ||
                 (ev.type === 'boot' && !!d.abnormal),
            type: safeType(ev.type),
            title: ts.replace('T', ' ') + ' · ' + t.label + (full ? ': ' + full : '') +
                   (ev.cursor ? '\n' + ev.cursor : '')
        };
    }

    // Tipo usable en una clase o un atributo (los tipos vienen del server).
    function safeType(type) { return String(type).replace(/[^a-z0-9_-]/gi, ''); }

    // [{type, n, icon, label}] en el orden de EVENT_TYPES, solo los que aparecen.
    // Recibe la lista de eventos o el {tipo: n} de /events?counts=1.
    function eventCounts(events) {
        var n = {};
        if (Array.isArray(events) || !events) (events || []).forEach(function (e) { n[e.type] = (n[e.type] || 0) + 1; });
        else Object.keys(events).forEach(function (k) { n[k] = events[k]; });
        return EVENT_ORDER.concat(Object.keys(n).filter(function (k) { return !EVENT_TYPES[k]; }))
            .filter(function (k) { return n[k]; })
            .map(function (k) {
                var t = EVENT_TYPES[k] || {icon: '·', label: k};
                return {type: k, n: n[k], icon: t.icon, label: t.label};
            });
    }

    // Une dos páginas de /events (la principal y la de tipos raros: boot_loop,
    // flash, fw), sin repetidos, ordenadas por (sesión, offset) como el server.
    function mergeEvents(a, b) {
        var seen = {}, out = [];
        (a || []).concat(b || []).forEach(function (e) {
            var k = e.type + '|' + e.cursor + '|' + e.ts;
            if (!seen[k]) { seen[k] = true; out.push(e); }
        });
        function key(e) {
            var m = /^c:(\d{8}_\d{6}_\d+):(\d+)$/.exec(e.cursor || '');
            return m ? [m[1], +m[2]] : ['', 0];
        }
        return out.sort(function (x, y) {
            var a1 = key(x), b1 = key(y);
            return a1[0] < b1[0] ? -1 : a1[0] > b1[0] ? 1 : a1[1] - b1[1];
        });
    }

    // Espera del próximo poll: `every` ms, o el doble por cada error seguido, hasta `max`.
    function backoffMs(every, fails, max) {
        return Math.min(max === undefined ? 30000 : max, every * Math.pow(2, Math.max(0, fails)));
    }

    /*
     * Parámetros de /api/board/{key}/log para ver un evento en contexto.
     * panic / boot_loop: del rst: anterior al siguiente (el arranque entero que
     * terminó en el crash). El resto: N líneas antes y después (un boot muestra
     * lo que pasó antes del reset; un send, lo previo y la respuesta). Sin
     * cursor (evento viejo sin cursor): null.
     */
    function eventContext(ev, before, after) {
        if (!ev || !ev.cursor) return null;
        var q = {around: ev.cursor, raw: '1', max_lines: '2000'};
        if (ev.type !== 'panic' && ev.type !== 'boot_loop') {
            q.before = String(before === undefined ? 40 : before);
            q.after = String(after === undefined ? 200 : after);
        }
        return q;
    }

    // Línea compacta de /log ("HH:MM:SS.mmm > x", o con fecha si es de otro
    // día) → línea con el prefijo del DeviceLog, para renderla como las del vivo.
    var COMPACT_RE = /^\d\d:\d\d:\d\d\.\d{3} [>|\u21aa] /;
    function expandRangeLine(line, date) {
        return date && COMPACT_RE.test(line) ? date + ' ' + line : line;
    }

    /*
     * Índice de la línea del evento dentro del contexto, o -1. `seq` es lo que
     * devuelve around=<cursor>&before=N&after=0: las N líneas previas y, al
     * final, la del evento. Se compara con la hora (ms) incluida y, entre líneas
     * idénticas (mismo texto en el mismo ms), gana la que tiene más líneas
     * previas iguales a las de `seq`; si empatan, la primera.
     */
    function findLine(lines, seq, date, seqDate) {
        if (!seq || !seq.length) return -1;
        if (typeof seq === 'string') seq = [seq];
        var want = seq.map(function (l) { return expandRangeLine(l, seqDate); });
        var have = lines.map(function (l) { return expandRangeLine(l, date); });
        var last = want.length - 1, best = -1, bestScore = -1;
        for (var i = 0; i < have.length; i++) {
            if (have[i] !== want[last]) continue;
            var k = 1;
            while (k <= last && i - k >= 0 && have[i - k] === want[last - k]) k++;
            if (k > bestScore) { best = i; bestScore = k; }
        }
        return best;
    }

    // Offset de un cursor c:<sesión>:<offset>, o null.
    function cursorOffset(cursor) {
        var m = /^c:\d{8}_\d{6}_\d+:(\d+)$/.exec(cursor || '');
        return m ? +m[1] : null;
    }

    // ── Marcas de eventos en el log en vivo ───────────────────────────────

    // Copia de serial_watch._PANIC_RES / _RESET_RE: tests/test_linemark_parity.py
    // corre los mismos casos en Python y en node y exige el mismo resultado.
    var MARK_PANIC_RE = new RegExp([
        "Guru Meditation Error: Core\\s+\\d+ panic'ed \\(([^)]*)\\)", 'abort\\(\\) was called',
        'Brownout detector was triggered', 'Task watchdog got triggered',
        '\\*\\*\\*ERROR\\*\\*\\* A stack overflow in task (\\S+)', 'assert failed:'
    ].join('|'));
    var MARK_BOOT_RE = /rst:0x[0-9a-fA-F]+ \(([A-Z0-9_]+)\)/;

    /*
     * Marca de una línea por su contenido: 'boot' (rst:) o 'panic' (el inicio de
     * un panic: la misma detección que serial_watch.line_kind, no el backtrace
     * ni el "Rebooting..."), o null. Solo líneas seriales (o sin prefijo).
     */
    function lineMark(line) {
        var p = splitPrefix(line);
        if (p.origin === '|') return null;
        var body = stripAnsi(p.body);
        var i = body.lastIndexOf('\r');
        if (i >= 0) body = body.slice(i + 1);
        if (MARK_BOOT_RE.test(body)) return 'boot';
        if (MARK_PANIC_RE.test(body)) return 'panic';
        return null;
    }

    // "2026-10-05T16:02:03.123" (o con espacio) → ms epoch local, o null.
    function tsMs(iso) {
        var m = /^(\d{4})-(\d\d)-(\d\d)[T ](\d\d):(\d\d):(\d\d)(?:\.(\d{1,3}))?/.exec(iso || '');
        if (!m) return null;
        return new Date(+m[1], +m[2] - 1, +m[3], +m[4], +m[5], +m[6], +((m[7] || '0') + '00').slice(0, 3)).getTime();
    }

    /*
     * Eventos del api (send, command, flash) → la línea del vivo donde marcarlos.
     * El WebSocket manda texto, sin offsets: se ubican por hora.
     * - Ventana: desde t − back (150 ms: el api registra el evento después de
     *   mandar las teclas y el eco puede tener hora anterior).
     * - send con texto: el eco, una línea en [t − back, t + 2 s] que lo contenga.
     *   Entre varias, la ÚLTIMA con hora ≤ t (el eco llega antes de que el api
     *   registre el evento; si el firmware imprimió el mismo texto un instante
     *   antes, queda antes del eco), o si no hay, la primera después de t. Sin
     *   ninguna, la primera línea de la ventana.
     * - El resto: la primera línea de la ventana.
     * - Si la candidata está a más de 5 s, no se marca (la placa estuvo callada:
     *   la marca caería en una línea que no tiene nada que ver).
     * Aproximado a propósito (ver remote/dashboard/CLAUDE.md).
     *
     * add(events) → cuántos nuevos. feed(handle, lineIso, lineText) → [{ev, handle}]
     * a marcar (handle puede ser el de una línea anterior: el fallback de un send);
     * hay que llamarla en orden de líneas. flush(nowMs) resuelve los send cuya
     * ventana de texto ya pasó aunque no haya llegado otra línea.
     */
    function EventMarks(opts) {
        opts = opts || {};
        this.back = opts.back === undefined ? 150 : opts.back;
        this.textWindow = opts.textWindow === undefined ? 2000 : opts.textWindow;
        this.maxGap = opts.maxGap === undefined ? 5000 : opts.maxGap;
        this.reset();
    }

    EventMarks.prototype.reset = function () {
        this._seen = {};
        this._pending = [];
    };

    EventMarks.prototype.add = function (events) {
        var added = 0;
        for (var i = 0; i < (events || []).length; i++) {
            var ev = events[i];
            var key = ev.type + '|' + ev.cursor + '|' + ev.ts;
            var t = tsMs(ev.ts);
            if (this._seen[key] || t === null) continue;
            this._seen[key] = true;
            var text = ev.type === 'send' && ev.detail && ev.detail.text ? String(ev.detail.text).toLowerCase() : null;
            this._pending.push({t: t, ev: ev, text: text, fallback: null, best: null});
            added++;
        }
        this._pending.sort(function (a, b) { return a.t - b.t; });
        return added;
    };

    EventMarks.prototype.pending = function () { return this._pending.length; };

    EventMarks.prototype.feed = function (handle, lineIso, lineText) {
        var lt = tsMs(lineIso);
        var out = [];
        if (lt === null) return out;
        var low = String(lineText || '').toLowerCase();
        var keep = [];
        for (var i = 0; i < this._pending.length; i++) {
            var p = this._pending[i];
            if (lt < p.t - this.back) { keep.push(p); continue; }
            if (p.fallback === null && lt > p.t + this.maxGap) continue;          // demasiado lejos: sin marca
            var hit = !!p.text && lt <= p.t + this.textWindow && low.indexOf(p.text) >= 0;
            if (!p.text) {
                out.push({ev: p.ev, handle: handle});
            } else if (hit && lt <= p.t) {
                p.best = handle;                     // por ahora el último eco antes de t
                if (p.fallback === null) p.fallback = handle;
                keep.push(p);
            } else if (p.best !== null && lt > p.t) {
                out.push({ev: p.ev, handle: p.best});
            } else if (hit) {
                out.push({ev: p.ev, handle: handle});
            } else if (lt > p.t + this.textWindow) {
                out.push({ev: p.ev, handle: p.fallback !== null ? p.fallback : handle});
            } else {
                if (p.fallback === null) p.fallback = handle;
                keep.push(p);
            }
        }
        this._pending = keep;
        return out;
    };

    EventMarks.prototype.flush = function (nowMs) {
        var out = [], keep = [];
        for (var i = 0; i < this._pending.length; i++) {
            var p = this._pending[i];
            if (p.best !== null && nowMs > p.t) out.push({ev: p.ev, handle: p.best});
            else if (p.fallback !== null && nowMs > p.t + this.textWindow) out.push({ev: p.ev, handle: p.fallback});
            else if (nowMs > p.t + 10 * 60 * 1000) continue;              // nunca llegó una línea: se olvida
            else keep.push(p);
        }
        this._pending = keep;
        return out;
    };

    return {
        fmtDur: fmtDur, secondsUntil: secondsUntil, expiresText: expiresText, lockInfo: lockInfo,
        forceConfirmText: forceConfirmText, searchMatch: searchMatch,
        EVENT_TYPES: EVENT_TYPES, EVENT_ORDER: EVENT_ORDER, eventDetail: eventDetail, eventView: eventView,
        eventCounts: eventCounts, eventContext: eventContext, cursorSession: cursorSession,
        safeType: safeType, mergeEvents: mergeEvents, backoffMs: backoffMs,
        sessionLabel: sessionLabel, sessionFile: sessionFile, expandRangeLine: expandRangeLine, findLine: findLine,
        cursorOffset: cursorOffset,
        lineMark: lineMark, tsMs: tsMs, EventMarks: EventMarks,
        errorText: errorText, errorCode: errorCode, withToken: withToken,
        escapeHtml: escapeHtml, parseLocal: parseLocal, relTime: relTime, fmtBytes: fmtBytes,
        isoLocal: isoLocal, sessionStart: sessionStart,
        healthBadges: healthBadges, healthLevel: healthLevel,
        splitPrefix: splitPrefix, stripAnsi: stripAnsi, lineClass: lineClass, isProblem: isProblem,
        ansiLineToHtml: ansiLineToHtml, overwrite: overwrite, LineBuffer: LineBuffer
    };
});
