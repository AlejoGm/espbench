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

    // output_<YYYYMMDD_HHMMSS_pid>.log → inicio de esa sesión (ISO local), o null. El id lo arma
    // device_log.make_session_id y el nombre lo valida history.SESSION_RE: tests/test_contract_parity.py.
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

    // acked (/api/devices `acked`): {panic, reset} = el último panic / reset anormal es de antes del ACK.
    function panicActive(h, acked) { return h.panics > 0 && !(acked && acked.panic); }
    function resetActive(h, acked) { return !!(h.last_reset && h.last_reset.abnormal) && !(acked && acked.reset); }

    // Lista de badges {cls, text, title}, de más grave a menos. Vacía = todo bien. Lo cubierto por el ACK no va.
    function healthBadges(h, acked) {
        var out = [];
        if (!h) return out;
        if (h.boot_loop) {
            out.push({cls: 'hb-loop', text: 'BOOT LOOP',
                      title: 'Reinicia en loop: ' + h.boots + ' arranques en poco tiempo'});
        }
        if (panicActive(h, acked)) {
            var p = h.last_panic || {};
            var what = (PANIC_KINDS[p.kind] || p.kind || 'panic') + (p.detail ? ' (' + p.detail + ')' : '');
            out.push({cls: 'hb-panic', text: '⚠ ' + h.panics + (h.panics === 1 ? ' panic' : ' panics'),
                      title: 'Último: ' + what + (p.ts ? ' — ' + p.ts.replace('T', ' ') : '')});
        }
        var r = h.last_reset;
        if (resetActive(h, acked) && !h.boot_loop) {
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
    function healthLevel(h, acked) {
        if (!h) return 'ok';
        if (h.boot_loop || panicActive(h, acked)) return 'bad';
        if (resetActive(h, acked)) return 'warn';
        return 'ok';
    }

    // ── Líneas del log ────────────────────────────────────────────────────

    var ANSI_RE = /\x1b\[[0-9;]*[A-Za-z]/g;

    function stripAnsi(s) { return s.replace(ANSI_RE, ''); }

    // Prefijo que pone DeviceLog a cada línea del archivo:
    // "2026-10-05 16:02:03.123 > " — origen: > serial, | taglog, ↪ continuación
    // de una línea serial que salió partida. Los logs viejos no lo tienen. Copia de
    // logrange._PREFIX_RE: tests/test_contract_parity.py.
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

    // Búsqueda de la home: texto libre (nombre, SN, firmware, nota...), "@usuario"
    // (solo el usuario del lock; "@" solo = cualquier placa con lock), "lock:reserva" /
    // "lock:flash" (los contadores del header) y propiedades "cat:valor" (el click en un
    // chip; "cat:" = con cualquier valor), combinables con texto: "chip:esp32-s3 lte".
    // lockKind: 'reserva' | 'flash' | ''. props: {cat: valor | [valores]}.
    function searchMatch(q, text, lockUser, lockKind, props) {
        q = (q || '').trim().toLowerCase();
        if (!q) return true;
        if (q === 'lock:reserva' || q === 'lock:flash') return lockKind === q.slice(5);
        if (q.charAt(0) === '@') return !!lockUser && lockUser.toLowerCase().indexOf(q.slice(1)) >= 0;
        var rest = [];
        var ok = q.split(/\s+/).every(function (tok) {
            var m = /^([a-z_]+):(.*)$/.exec(tok);
            // Solo una categoría de propiedades: "aa:bb:cc..." (una MAC) o "10:30" son texto.
            if (!m || PROP_CATEGORIES.indexOf(m[1]) < 0) { rest.push(tok); return true; }
            var have = propValues((props || {})[m[1]]);
            return m[2] ? have.indexOf(m[2]) >= 0 : have.length > 0;
        });
        return ok && (text || '').indexOf(rest.join(' ')) >= 0;
    }

    // ── Nota y propiedades de la placa (/api/devices: note*, props; /api/properties) ──

    // Las categorías son fijas, en el código del server (board_meta.CATEGORIES): copia a propósito
    // (searchMatch la necesita sin pedir el catálogo); tests/test_contract_parity.py compara las dos.
    var PROP_CATEGORIES = ['estado', 'uso', 'chip', 'conectividad'];
    // Sin catálogo (bench viejo, o todavía no llegó): los valores que excluyen de pick.
    var DEFAULT_EXCLUDE = {estado: ['no-tocar', 'roto']};

    function propValues(v) { return Array.isArray(v) ? v : (v ? [v] : []); }

    // La propiedad que excluye a la placa de pick (estado no-tocar / roto: exclude_pick), o null:
    // {cat, value, label} (label del catálogo si está; si no, el id con espacios: "no tocar").
    function avoidedBy(device, catalog) {
        var props = (device && device.props) || {};
        var excl = DEFAULT_EXCLUDE;
        if (catalog && catalog.length) {
            excl = {};
            catalog.forEach(function (c) {
                excl[c.id] = (c.values || []).filter(function (v) { return v.exclude_pick; }).map(function (v) { return v.id; });
            });
        }
        var cats = Object.keys(props);
        for (var i = 0; i < cats.length; i++) {
            var vals = propValues(props[cats[i]]);
            for (var j = 0; j < vals.length; j++) {
                if ((excl[cats[i]] || []).indexOf(vals[j]) < 0) continue;
                var f = findValue(catalog, cats[i], vals[j]);
                return {cat: cats[i], value: vals[j], label: f.value && f.value.label ? f.value.label : vals[j].replace(/-/g, ' ')};
            }
        }
        return null;
    }

    function avoided(device, catalog) { return !!avoidedBy(device, catalog); }

    // Nota para mostrar, o null: {text, by, ago, title}. note_at viene con la zona de la Pi.
    function noteInfo(d, now) {
        if (!d || !d.note) return null;
        var ago = d.note_at ? relTime(d.note_at, now) : null;
        var at = d.note_at ? d.note_at.replace('T', ' ').slice(0, 16) : '';
        return {text: d.note, by: d.note_by || '', ago: ago || '',
                title: 'Nota' + (d.note_by ? ' de ' + d.note_by : '') + (at ? ' · ' + at : '') + '\n' + d.note};
    }

    // Línea de la nota (card y header): "✎ texto — juan · hace 5 min". Todo escapado.
    function noteHtml(d, now) {
        var n = noteInfo(d, now);
        if (!n) return '';
        var meta = [n.by, n.ago].filter(Boolean).join(' · ');
        return '<span class="note-text">' + escapeHtml(n.text) + '</span>' +
               (meta ? ' <span class="note-meta">— ' + escapeHtml(meta) + '</span>' : '');
    }

    var NOTE_MAX = 200;

    // Validación de la nota antes de mandarla (la misma regla que board_meta.clean_note).
    function noteCheck(text) {
        var t = String(text == null ? '' : text).trim();
        if (t.length > NOTE_MAX) return {ok: false, text: t, error: 'más de ' + NOTE_MAX + ' caracteres'};
        if (/[\u0000-\u001f\u007f-\u009f]/.test(t)) return {ok: false, text: t, error: 'sin saltos de línea ni tabs'};
        return {ok: true, text: t, error: null};
    }

    // Unión de catálogos (bench-master: benches con valores distintos). Mismo formato que /api/properties.
    function mergeCatalogs(lists) {
        var out = [], idx = {};
        (lists || []).forEach(function (cats) {
            (cats || []).forEach(function (c) {
                if (!idx[c.id]) { idx[c.id] = {id: c.id, label: c.label, multi: c.multi, values: []}; out.push(idx[c.id]); }
                var have = idx[c.id].values.map(function (v) { return v.id; });
                (c.values || []).forEach(function (v) { if (have.indexOf(v.id) < 0) idx[c.id].values.push(v); });
            });
        });
        return out;
    }

    function findValue(catalog, cat, id) {
        var c = (catalog || []).filter(function (x) { return x.id === cat; })[0];
        var v = c ? (c.values || []).filter(function (x) { return x.id === id; })[0] : null;
        return {cat: c || null, value: v || null};
    }

    // Chips de las propiedades, en el orden de las categorías del catálogo (las que no
    // están, al final): [{cat, value, text, warn, filter, title, unknown}].
    function propChips(props, catalog) {
        props = props || {};
        var order = (catalog || []).map(function (c) { return c.id; });
        var cats = Object.keys(props).sort(function (a, b) {
            var ia = order.indexOf(a), ib = order.indexOf(b);
            return (ia < 0 ? 1e9 : ia) - (ib < 0 ? 1e9 : ib);
        });
        var out = [];
        cats.forEach(function (cat) {
            propValues(props[cat]).forEach(function (val) {
                var f = findValue(catalog, cat, val);
                var label = f.value ? f.value.label || val : val;
                out.push({cat: cat, value: val, label: label, text: (f.cat ? f.cat.label : cat) + ': ' + label,
                          warn: !!(f.value && (f.value.warn || f.value.exclude_pick)), filter: cat + ':' + val,
                          unknown: !!catalog && !f.value,
                          title: (f.value && f.value.desc ? f.value.desc + '\n' : '') + 'click: filtrar ' + cat + ':' + val});
            });
        });
        return out;
    }

    function propChipsHtml(props, catalog) {
        return propChips(props, catalog).map(function (p) {
            return '<span class="prop-chip' + (p.warn ? ' prop-warn' : '') + (p.unknown ? ' prop-unknown' : '') +
                   '" data-filter="' + escapeHtml(p.filter) + '" title="' + escapeHtml(p.title) + '">' +
                   escapeHtml(p.text) + '</span>';
        }).join('');
    }

    // Texto para el data-search de la card: "chip:esp32-s3 conectividad:lte ...".
    function propsSearchText(props) {
        return Object.keys(props || {}).map(function (cat) {
            return propValues(props[cat]).map(function (v) { return cat + ':' + v; }).join(' ');
        }).join(' ');
    }

    // Lo que necesita el editor: cada categoría con sus valores y cuáles tiene la placa
    // (un valor que la placa tiene y ya no está en el catálogo aparece igual, para poder quitarlo).
    function propsEditModel(props, catalog) {
        props = props || {};
        return (catalog || []).map(function (c) {
            var have = propValues(props[c.id]);
            var values = (c.values || []).map(function (v) {
                return {id: v.id, label: v.label || v.id, desc: v.desc || '', warn: !!(v.warn || v.exclude_pick),
                        checked: have.indexOf(v.id) >= 0};
            });
            have.forEach(function (id) {
                if (!values.some(function (v) { return v.id === id; })) {
                    values.push({id: id, label: id + ' (fuera del catálogo)', desc: '', warn: false, checked: true});
                }
            });
            return {id: c.id, label: c.label || c.id, multi: !!c.multi, values: values};
        });
    }

    // Cambios para PATCH {props: ...}: solo las categorías que cambiaron. selection: {cat: [ids]}.
    // Una categoría sin valores va como null (quitarla); las de un valor, como string.
    function propsPatch(before, selection, catalog) {
        var multi = {};
        (catalog || []).forEach(function (c) { multi[c.id] = !!c.multi; });
        var out = {}, n = 0;
        Object.keys(selection || {}).forEach(function (cat) {
            var now = selection[cat] || [];
            var was = propValues((before || {})[cat]);
            var same = now.length === was.length && now.every(function (v) { return was.indexOf(v) >= 0; });
            if (same) return;
            out[cat] = now.length ? (multi[cat] ? now.slice() : now[0]) : null;
            n++;
        });
        return n ? out : null;
    }

    // Slug de un valor nuevo (la regla de board_meta.VALUE_RE): null si no sirve.
    function propValueId(text) {
        var t = String(text || '').trim().toLowerCase().replace(/\s+/g, '-');
        return /^[a-z0-9][a-z0-9._-]*$/.test(t) && t.length <= 24 ? t : null;
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
        note:      {icon: '✎', label: 'nota'},
        props:     {icon: '◇', label: 'propiedades'},
        ack:       {icon: '✓', label: 'ACK'},
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

    // Durante un boot loop los panics no van como eventos: el end trae cuántos y el kind del primero y el último.
    function loopKinds(d) {
        var k = [d.first_panic, d.last_panic].filter(Boolean).map(function (x) { return PANIC_KINDS[x] || x; });
        if (k.length === 2 && k[0] === k[1]) k = [k[0]];
        return k.length ? ' (' + k.join(' … ') + ')' : '';
    }

    function eventDetail(ev) {
        var d = ev.detail || {};
        var forced = d.forced ? ' · forzado' + (forcedBy(d) ? ' por ' + forcedBy(d) : '') : '';
        switch (ev.type) {
        case 'boot': return (d.reason || '') + (d.abnormal ? ' ⚠' : '');
        case 'panic': return (PANIC_KINDS[d.kind] || d.kind || 'panic') + (d.reason ? ' (' + d.reason + ')' : '');
        case 'boot_loop': return (d.phase === 'end' ? 'fin' : 'inicio') + (d.boots ? ' · ' + d.boots + ' arranques' : '') +
                                 (d.panics ? ' · ' + d.panics + (d.panics === 1 ? ' panic' : ' panics') + loopKinds(d) : '');
        case 'fw': return [d.project, d.version].filter(Boolean).join(' ') + (d.idf ? ' · IDF ' + d.idf : '');
        case 'state': return (STATE_NAMES[d.from] || d.from || '?') + ' → ' + (STATE_NAMES[d.to] || d.to || '?');
        case 'flash': return d.ok ? '✓ ' + (d.status || 'ok') : '✗ ' + String(d.error || d.status || 'falló').replace(/_/g, ' ');
        case 'send': return '"' + (d.text || '') + '"' + (d.enter ? ' ⏎' : '') + forced;
        case 'command': return (COMMAND_NAMES[d.command] || d.command || '') + forced;
        case 'reserve': return d.expires ? 'hasta ' + d.expires.replace('T', ' ').slice(0, 16) : '';
        case 'release': return d.forced ? 'de ' + (d.user || '?') + ' · forzada' +
                                          (forcedBy(d) ? ' por ' + forcedBy(d) : '') : '';
        case 'session': return [d.tty, d.pid ? 'pid ' + d.pid : ''].filter(Boolean).join(' · ');
        case 'note': return d.text ? '"' + d.text + '"' : 'borrada';
        case 'props': return Object.keys(d.changes || {}).map(function (cat) {
            var to = propValues(d.changes[cat].to);
            return cat + (to.length ? '=' + to.join(',') : ' quitada');
        }).join(' · ');
        }
        return '';
    }

    // Cursor del log: c:<sesión>:<offset>. Copia de events._CURSOR_RE (Python):
    // tests/test_contract_parity.py corre los mismos casos en los dos lados.
    var CURSOR_RE = /^c:(\d{8}_\d{6}_\d+):(\d+)$/;

    // c:<sesión>:<offset> → [sesión, offset], o null.
    function parseCursor(cursor) {
        var m = CURSOR_RE.exec(cursor || '');
        return m ? [m[1], +m[2]] : null;
    }

    // c:<sesión>:<offset> → sesión, o null.
    function cursorSession(cursor) {
        var c = parseCursor(cursor);
        return c ? c[0] : null;
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
        function key(e) { return parseCursor(e.cursor) || ['', 0]; }
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
        var c = parseCursor(cursor);
        return c ? c[1] : null;
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

    // ── Cards (compartido con bench-master) ───────────────────────────────

    // Clase de la franja de color de la card según estado de la FSM y salud.
    // Una placa sana con estado no-tocar/roto (avoided) no es "ok": st-avoid (no se elige).
    function cardState(device, catalog) {
        if (device.status !== 'RUNNING') return 'st-down';
        if (device.state === 'flashing' || device.state === 'erasing' || device.state === 'discovering') return 'st-busy';
        if (device.state === 'unknown') return 'st-unknown';
        var lvl = healthLevel(device.health, device.acked);
        return lvl === 'bad' ? 'st-bad' : lvl === 'warn' ? 'st-warn' : avoided(device, catalog) ? 'st-avoid' : 'st-ok';
    }

    // Estado de la FSM (run/<tty>.json). "monitoring" es lo normal y no lleva badge.
    var STATE_BADGES = {
        discovering: ['badge-busy',    'INICIANDO'],
        flashing:    ['badge-busy',    'FLASHEANDO'],
        erasing:     ['badge-busy',    'BORRANDO'],
        unknown:     ['badge-unknown', 'SIN MAC'],
        disconnected:['badge-down',    'DESCONECTADO']
    };

    function stateBadgeHtml(device) {
        var b = STATE_BADGES[device.state];
        return b ? '<span class="badge ' + b[0] + '">' + b[1] + '</span>' : '';
    }

    // Filas de firmware de la card: versión de la app (el proyecto en el tooltip: suele repetir
    // el HW) y ESP-IDF, cada una en su fila. [{label, html, title}]
    function fwRows(device) {
        var rows = [];
        var app = device.fw_version || device.fw_project;
        if (app) rows.push({label: 'App', html: '<span class="fw-version">' + escapeHtml(app) + '</span>',
                            title: device.fw_project ? 'Proyecto: ' + device.fw_project : ''});
        if (device.fw_idf) rows.push({label: 'ESP-IDF', html: escapeHtml(device.fw_idf), title: ''});
        return rows;
    }

    function lastFlashHtml(device, now) {
        if (!device.last_flash_ts) return '<span class="dim">nunca</span>';
        var icon = device.last_flash_ok === true ? '<span class="ok-mark">✓</span> '
                 : device.last_flash_ok === false ? '<span class="fail-mark">✗</span> ' : '';
        var who = device.last_flash_user ? ' <span class="dim">·</span> ' + escapeHtml(device.last_flash_user) : '';
        return icon + escapeHtml(relTime(device.last_flash_ts, now) || device.last_flash_ts) + who;
    }

    // Contadores del header. Los devices sin MAC (st-unknown) cuentan en el total y nada más.
    // Locks: reservas vigentes (con `who` para el tooltip) y locks del flash, por separado.
    function summarize(devices, now, catalog) {
        var c = {total: devices.length, ok: 0, busy: 0, bad: 0, down: 0, avoid: 0, reserved: 0, flashLocked: 0, who: []};
        devices.forEach(function (d) {
            var st = cardState(d, catalog);
            if (st === 'st-down') c.down++;
            else if (st === 'st-busy') c.busy++;
            else if (st === 'st-bad' || st === 'st-warn') c.bad++;
            else if (st === 'st-avoid') c.avoid++;
            else if (st !== 'st-unknown') c.ok++;
        });
        devices.forEach(function (d) {
            var lock = lockInfo(d, now);
            if (!lock) return;
            if (lock.reservation) {
                c.reserved++;
                // hora fija, no "vence en": el tooltip no se refresca con el tick
                c.who.push(lock.user + ' → ' + (d.device_key || d.tty_name) + ' (hasta ' + lock.until.slice(11, 16) + ')');
            } else {
                c.flashLocked++;
            }
        });
        return c;
    }

    // ── URLs ──────────────────────────────────────────────────────────────

    // Directorio de la página: "/" servida directo por el bench, "/bench/<nombre>/"
    // a través de bench-master. Todas las URLs del dashboard son relativas a esto.
    function basePath(pathname) {
        return pathname.replace(/[^/]*$/, '') || '/';
    }

    function wsUrl(loc, rel) {
        var proto = loc.protocol === 'https:' ? 'wss:' : 'ws:';
        return proto + '//' + loc.host + basePath(loc.pathname) + rel;
    }

    // ── v2: estado, actividad y card de placa (bench y bench-master) ──────

    var SILENT_S = 300;      // monitoreando y sin imprimir nada hace 5 min: "sin log"

    function silentFor(d, nowMs) {
        if (!d || d.status !== 'RUNNING' || d.state !== 'monitoring' || !d.last_log_epoch) return null;
        var s = ((nowMs === undefined ? Date.now() : nowMs) / 1000) - d.last_log_epoch;
        return s >= SILENT_S ? s : null;
    }

    // Estado de la placa en una palabra: {cls: ok|bad|warn|flash|off|avoid, text}. El orden es la prioridad.
    // avoid: sana, pero con una propiedad que la excluye de pick (estado no-tocar / roto; catalog
    // = /api/properties, sin él los de siempre). No es "ok": no se elige.
    function boardStatus(d, nowMs, catalog) {
        if (d.status !== 'RUNNING') return {cls: 'off', text: d.state === 'disconnected' ? 'Desconectada' : 'Caída'};
        if (d.state === 'flashing') return {cls: 'flash', text: 'Flasheando'};
        if (d.state === 'erasing') return {cls: 'flash', text: 'Borrando'};
        if (d.state === 'discovering') return {cls: 'flash', text: 'Iniciando'};
        if (d.state === 'unknown') return {cls: 'warn', text: 'Sin MAC'};
        var h = d.health || {};
        if (h.boot_loop) return {cls: 'bad', text: 'Boot loop'};
        // Con un ACK, h.panics cuenta también los de antes: sin número.
        if (panicActive(h, d.acked)) return {cls: 'bad', text: h.panics === 1 || d.acked ? 'Panic' : h.panics + ' panics'};
        if (silentFor(d, nowMs) !== null) return {cls: 'warn', text: 'Sin log'};
        if (healthLevel(h, d.acked) === 'warn') return {cls: 'warn', text: 'Reset anormal'};
        var av = avoidedBy(d, catalog);
        if (av) return {cls: 'avoid', text: av.label.charAt(0).toUpperCase() + av.label.slice(1)};
        return {cls: 'ok', text: 'En línea'};
    }

    // Tiempo encendida desde el último boot visto: [número, unidad, número, unidad] o null.
    function uptimeParts(d, nowMs) {
        var r = d && d.health && d.health.last_reset;
        var t = r && parseLocal(r.ts);
        if (!t || d.state !== 'monitoring') return null;
        var s = Math.max(0, ((nowMs === undefined ? Date.now() : nowMs) - t.getTime()) / 1000);
        var m = Math.floor(s / 60), h = Math.floor(m / 60), days = Math.floor(h / 24);
        if (days > 0) return [String(days), 'd', String(h % 24), 'h'];
        if (h > 0) return [String(h), 'h', String(m % 60), 'min'];
        if (m > 0) return [String(m), 'min'];
        return [String(Math.floor(s)), 's'];
    }

    function agoText(epochS, nowMs) {
        if (!epochS) return '—';
        return fmtDur(((nowMs === undefined ? Date.now() : nowMs) / 1000) - epochS);
    }

    // Totales de una lista de buckets de /api/activity.
    function activityTotals(buckets) {
        var t = {boot: 0, panic: 0, flash: 0, boot_loop: 0, reserve: 0};
        (buckets || []).forEach(function (b) { for (var k in t) t[k] += b[k] || 0; });
        return t;
    }

    var BAR_H = {boot: 38, flash: 66, panic: 100};
    function barKind(b) {
        if (b.panic || b.boot_loop) return 'panic';
        if (b.flash) return 'flash';
        if (b.boot) return 'boot';
        return '';
    }

    // Una barra por hora (la más vieja a la izquierda), coloreada por lo peor que pasó en esa hora.
    // acked: las primeras `acked` horas terminaron antes del ACK (van atenuadas).
    function activityBarsHtml(buckets, acked) {
        var n = (buckets || []).length;
        return (buckets || []).map(function (b, i) {
            var k = barKind(b), ago = n - i;
            var parts = [];
            if (b.boot) parts.push(b.boot + (b.boot === 1 ? ' reinicio' : ' reinicios'));
            if (b.panic) parts.push(b.panic + (b.panic === 1 ? ' panic' : ' panics'));
            if (b.boot_loop) parts.push('boot loop');
            if (b.flash) parts.push(b.flash + (b.flash === 1 ? ' flash' : ' flashes'));
            if (b.reserve) parts.push('reservada');
            var title = 'Hace ' + ago + ' h' + (parts.length ? ': ' + parts.join(', ') : ': sin novedades') +
                        (i < (acked || 0) && parts.length ? ' (antes del ACK)' : '');
            return '<span class="' + (k + (b.reserve ? ' resv' : '') + (i < (acked || 0) ? ' acked' : '')).trim() + '" style="height:' +
                   (k ? BAR_H[k] : 8) + '%" title="' + escapeHtml(title) + '"></span>';
        }).join('');
    }

    // Chips de salud de la máquina del bench (GET /api/bench/health). [{icon, value, label, level}]
    function benchChips(h) {
        if (!h) return [];
        var out = [];
        if (h.temp_c != null) out.push({icon: 'temperature', value: String(h.temp_c).replace('.', ',') + ' °C', label: 'temperatura',
                                        level: h.temp_c >= 80 ? 'bad' : h.temp_c >= 70 ? 'warn' : ''});
        if (h.ram) out.push({icon: 'cpu', value: h.ram.used_pct + ' %', label: 'RAM de ' + (h.ram.total_mb >= 1024 ? Math.round(h.ram.total_mb / 1024) + ' GB' : h.ram.total_mb + ' MB'),
                             level: h.ram.used_pct >= 90 ? 'bad' : h.ram.used_pct >= 80 ? 'warn' : ''});
        if (h.disk) out.push({icon: 'database', value: h.disk.used_pct + ' %', label: 'disco',
                              level: h.disk.used_pct >= 95 ? 'bad' : h.disk.used_pct >= 85 ? 'warn' : ''});
        if (h.load) out.push({icon: 'activity', value: String(h.load['1m']).replace('.', ','), label: 'carga',
                              level: h.load['1m'] >= h.load.cpus ? 'warn' : ''});
        if (h.uptime_s != null) out.push({icon: 'clock', value: fmtDur(h.uptime_s), label: 'encendida', level: ''});
        return out;
    }

    // "2 h 13 min" para el chip de la card (antes era un número grande aparte).
    function uptimeText(parts) { return parts ? parts.join(' ').replace(/(\d) (\D)/g, '$1\u00a0$2') : null; }

    // ── Propiedades en la card: specs, etiquetas y estado ────────────────

    // Rol de cada categoría en la card: qué es la placa (spec: chip, conectividad), para qué está (tag: uso)
    // y cómo está (estado). Una categoría que el server agregue después va como tag.
    var PROP_ROLE = {chip: 'spec', conectividad: 'spec', uso: 'tag', estado: 'estado'};
    var CAT_ICON = {chip: 'cpu', conectividad: 'access-point', uso: 'tag', estado: 'flag'};
    var VALUE_ICON = {wifi: 'wifi', lte: 'antenna-bars-5', ble: 'bluetooth', 'nb-iot': 'antenna', ethernet: 'network',
                      lora: 'radio', zigbee: 'affiliate', thread: 'affiliate'};

    function propIcon(cat, value, warn) {
        if (cat === 'estado' && warn) return 'alert-triangle';
        if (cat === 'conectividad' && VALUE_ICON[value]) return VALUE_ICON[value];
        return CAT_ICON[cat] || 'point';
    }

    // Las propiedades de una placa por rol: {spec, tag, estado}, cada una con los campos de propChips
    // más role e icon. El orden es el del catálogo (categorías y valores: todas las cards iguales).
    function cardProps(props, catalog) {
        var out = {spec: [], tag: [], estado: []};
        var chips = propChips(props, catalog);
        function rank(p) {
            var f = findValue(catalog, p.cat, p.value);
            var i = f.cat ? (f.cat.values || []).indexOf(f.value) : -1;
            return i < 0 ? 1e6 : i;
        }
        chips = chips.map(function (p, i) { return [p, i]; }).sort(function (a, b) {
            return a[0].cat === b[0].cat ? (rank(a[0]) - rank(b[0])) || (a[1] - b[1]) : a[1] - b[1];
        }).map(function (x) { return x[0]; });
        chips.forEach(function (p) {
            var role = PROP_ROLE[p.cat] || 'tag';
            p.role = role;
            p.icon = propIcon(p.cat, p.value, p.warn);
            out[role].push(p);
        });
        return out;
    }

    function cardPropHtml(p) {
        var cls = 'prop-chip pc-' + p.role + (p.cat === 'chip' ? ' pc-chip' : '') + (p.warn ? ' prop-warn' : '') +
                  (p.unknown ? ' prop-unknown' : '');
        return '<span class="' + cls + '" data-filter="' + escapeHtml(p.filter) + '" title="' +
               escapeHtml(p.text + (p.unknown ? ' (fuera del catálogo)' : '') + '\n' + p.title) + '">' +
               '<i class="ti ti-' + p.icon + '" aria-hidden="true"></i>' + escapeHtml(p.label) + '</span>';
    }

    /*
     * Nota y propiedades de la card (solo placas con MAC). Una fila: el estado que excluye (no tocar / roto,
     * rojo lleno) primero, las specs (cuadradas: chip invertido, conectividad con su ícono) y después las
     * etiquetas (uso, redondas violetas) y el estado común (redondo ámbar). Sin separador: al partirse la
     * fila quedaba colgando; la forma ya distingue specs de etiquetas. Debajo, la nota destacada. Sin nada: en el master no se dibuja
     * nada; en el bench (edit), los botones para agregar ("+ chip, conectividad…", "Nota").
     * edit: botones data-act="props|note" y el host .meta-editor para EBMeta.
     */
    function boardMetaHtml(d, catalog, edit, now) {
        if (!d.mac) return '';
        var n = noteInfo(d, now);
        var cp = cardProps(d.props, catalog);
        var hard = cp.estado.filter(function (p) { return p.warn; });
        var soft = cp.estado.filter(function (p) { return !p.warn; });
        var tags = cp.tag.concat(soft);
        var any = cp.spec.length + tags.length + hard.length;
        if (!n && !any && !edit) return '';
        var row = '';
        if (any || edit) {
            row = '<div class="prop-row">' + hard.map(cardPropHtml).join('') + cp.spec.map(cardPropHtml).join('') +
                tags.map(cardPropHtml).join('') +
                (edit ? (any ? '<button class="meta-btn icon" data-act="props" title="Editar las propiedades" aria-label="Editar las propiedades"><i class="ti ti-adjustments-horizontal"></i></button>'
                             : '<button class="meta-btn" data-act="props" title="Propiedades: chip, conectividad, uso, estado"><i class="ti ti-plus"></i>chip, conectividad…</button>') +
                        (n ? '' : '<button class="meta-btn" data-act="note" title="Agregar una nota"><i class="ti ti-note"></i>Nota</button>') : '') +
                '</div>';
        }
        return '<div class="card-meta">' + row +
            (n ? '<div class="card-note" title="' + escapeHtml(n.title) + '"><span class="note-icon">✎</span><span class="note-body">' + noteHtml(d, now) + '</span>' +
                 (edit ? '<button class="note-edit" data-act="note" title="Editar la nota" aria-label="Editar la nota"><i class="ti ti-pencil"></i></button>' : '') + '</div>' : '') +
            (edit ? '<div class="meta-editor"></div>' : '') +
        '</div>';
    }

    // Etiqueta del bench de la card (bench-master): nombre y ubicación, aparte del modelo y el tty.
    function benchTagHtml(bench, location, online) {
        return '<span class="bench-tag' + (online === false ? ' off' : '') + '" title="Bench ' + escapeHtml(bench) +
               (location ? ' · ' + escapeHtml(location) : '') + (online === false ? ' (sin respuesta)' : '') + '">' +
               '<i class="ti ti-' + (online === false ? 'server-off' : 'server-2') + '" aria-hidden="true"></i><b>' + escapeHtml(bench) + '</b>' +
               (location ? '<span class="loc"><i class="ti ti-map-pin" aria-hidden="true"></i>' + escapeHtml(location) + '</span>' : '') +
               '</span>';
    }

    /*
     * Card de una placa. opts: {buckets, totals y acked (de /api/activity: lo de después del ACK y cuántas
     * horas quedaron antes), href (monitor), direct (link directo, bench-master),
     * bench (nombre, bench-master), location (del bench), benchTag (false: sin la etiqueta del bench, p. ej.
     * agrupando por bench), rename (bench: lápiz para renombrar), catalog (/api/properties: colorea los chips
     * y decide "no tocar"), meta (bench: botones para editar nota y propiedades, y ACK), now}.
     * Los botones llevan data-act="rename|copy|note|props|ack" para que la página les ponga el handler.
     */
    function boardCardHtml(d, opts) {
        opts = opts || {};
        var st = boardStatus(d, opts.now, opts.catalog);
        var title = d.device_key || d.tty_name;
        var meta = [d.hw_model, d.tty_name].filter(Boolean).join(' · ');
        var tot = opts.totals || (opts.buckets ? activityTotals(opts.buckets) : null);
        var boots = tot ? tot.boot : Math.max(0, ((d.health || {}).boots || 1) - 1);
        var panics = tot ? tot.panic : (d.health || {}).panics || 0;
        var silent = silentFor(d, opts.now) !== null;
        var lock = lockInfo(d, opts.now);
        var live = d.state === 'monitoring' && d.status === 'RUNNING';
        var up = uptimeText(uptimeParts(d, opts.now));
        var ackTitle = d.ack_at ? 'Desde el ACK' + (d.ack_by ? ' de ' + d.ack_by : '') + ', ' + relTime(d.ack_at, opts.now) : '';
        var h = d.health || {};
        // ACK: en el bench (meta), con un server que lo soporta ('ack_at' en el device) y algo para dar por visto.
        var canAck = opts.meta && d.mac && 'ack_at' in d &&
                     (panics > 0 || panicActive(h, d.acked) || resetActive(h, d.acked));
        var fwTitle = [d.fw_project ? 'Proyecto ' + d.fw_project : '', d.fw_idf ? 'ESP-IDF ' + d.fw_idf : ''].filter(Boolean).join(', ');
        return '<article class="board st-' + st.cls + '" data-tty="' + escapeHtml(d.tty_name) + '"' + (opts.bench ? ' data-bench="' + escapeHtml(opts.bench) + '"' : '') + '>' +
            (opts.bench && opts.benchTag !== false ? '<div class="b-ctx">' + benchTagHtml(opts.bench, opts.location, d.bench_online) + '</div>' : '') +
            '<div class="bh"><div class="bh-name"><div class="name">' + escapeHtml(title) +
                (opts.rename && d.mac ? ' <button class="icon-btn" data-act="rename" aria-label="Renombrar"><i class="ti ti-pencil"></i></button>' : '') +
                '</div><div class="meta">' + escapeHtml(meta) + '</div></div>' +
                '<span class="status ' + st.cls + '">' + escapeHtml(st.text) + '</span></div>' +
            boardMetaHtml(d, opts.catalog, opts.meta, opts.now) +
            '<div class="facts">' +
                (live && up ? '<span class="fact" title="Encendida desde el último arranque"><i class="ti ti-clock" aria-label="Encendida"></i><b>' + escapeHtml(up) + '</b></span>' : '') +
                '<span class="fact' + (silent ? ' warn' : '') + '">' + (silent ? '<i class="ti ti-volume-off"></i>' : (live ? '<span class="live"></span>' : '')) +
                    'Último log <b>' + escapeHtml(agoText(d.last_log_epoch, opts.now)) + '</b></span>' +
                '<span class="fact"' + (ackTitle ? ' title="' + escapeHtml(ackTitle) + '"' : '') + '>Reinicios <b>' + boots + '</b></span>' +
                '<span class="fact' + (panics ? ' bad' : '') + '"' + (ackTitle ? ' title="' + escapeHtml(ackTitle) + '"' : '') + '>Panics <b>' + panics + '</b></span>' +
                (canAck ? '<button class="fact fact-btn" data-act="ack" title="ACK: dar por vistos los panics y reinicios hasta ahora">' +
                          '<i class="ti ti-checks" aria-hidden="true"></i>ACK</button>' : '') +
            '</div>' +
            (opts.buckets ? '<div class="bars" aria-label="Actividad por hora, últimas ' + opts.buckets.length + ' h">' + activityBarsHtml(opts.buckets, opts.acked) + '</div>' +
                            '<div class="baxis"><span>hace ' + opts.buckets.length + ' h</span><span>ahora</span></div>' : '') +
            '<div class="bf"><span class="fw"' + (fwTitle ? ' title="' + escapeHtml(fwTitle) + '"' : '') + '>Firmware <b>' + escapeHtml(d.fw_version || '—') + '</b></span>' +
                (lock ? '<span class="lock" title="' + escapeHtml(lock.title) + '"><i class="ti ti-lock"></i>' + escapeHtml(lock.user) +
                        (lock.reservation ? ', ' + escapeHtml(lock.text.replace('vence en ', '')) : '') + '</span>' : '') +
                (opts.direct ? '<a class="icon-btn" href="' + escapeHtml(opts.direct) + '" title="Abrir directo en el bench"><i class="ti ti-external-link"></i></a>' : '') +
                '<button class="icon-btn" data-act="copy" title="Copiar config para deploy.py"><i class="ti ti-copy"></i></button>' +
                '<a class="btn" href="' + escapeHtml(opts.href || ('device.html?tty=' + encodeURIComponent(d.tty_name))) + '"><i class="ti ti-terminal-2"></i>Monitor</a>' +
            '</div>' +
        '</article>';
    }

    // ── Agrupar la grilla de placas ───────────────────────────────────────

    // Lo que ofrece el selector. bench-master (master: true) también agrupa por bench y por ubicación.
    var GROUP_PROPS = ['chip', 'conectividad', 'uso', 'estado'];
    function groupOptions(master) {
        var out = [['', 'Sin agrupar']];
        if (master) out.push(['bench', 'Bench'], ['location', 'Ubicación']);
        return out.concat(GROUP_PROPS.filter(function (c) { return PROP_CATEGORIES.indexOf(c) >= 0; })
                                     .map(function (c) { return [c, c.charAt(0).toUpperCase() + c.slice(1)]; }));
    }

    // La agrupación al cargar: la de la URL (?group=) gana a la recordada; una que no se ofrece = sin agrupar.
    function pickGroup(fromUrl, stored, master) {
        var ok = groupOptions(master).map(function (o) { return o[0]; });
        if (fromUrl !== null && fromUrl !== undefined && ok.indexOf(fromUrl) >= 0) return fromUrl;
        return ok.indexOf(stored) >= 0 ? stored : '';
    }

    // Libre: sana (En línea, no "no tocar") y sin lock; lo que `espbench pick` elegiría.
    function boardFree(d, catalog, nowMs) {
        return boardStatus(d, nowMs, catalog).cls === 'ok' && !lockInfo(d, nowMs);
    }
    function boardProblem(d, nowMs) {
        var c = boardStatus(d, nowMs).cls;
        return c === 'bad' || c === 'warn' || c === 'off';
    }

    var GROUP_EMPTY = {bench: 'Sin bench', location: 'Sin ubicación'};

    /*
     * Grupos para la grilla: [{key, label, empty, items, count, free, problems, benches, location}].
     * by: '' (un solo grupo, todo), 'bench' (d.bench), 'location' (d.bench_location, sin distinguir mayúsculas)
     * o una categoría de propiedades. Una placa con varios valores en una categoría multi (WiFi y LTE) va en
     * cada grupo: agrupar responde "¿qué placas tienen LTE?" y esa también tiene. Sin valor → "Sin <x>", al
     * final. Orden: el del catálogo para las propiedades (los valores fuera del catálogo después), alfabético
     * para bench y ubicación. benches: los benches del grupo; location: la de un grupo por bench.
     */
    function groupBoards(devices, by, catalog, nowMs) {
        devices = devices || [];
        var groups = {}, keys = [];
        function add(key, label, d) {
            var g = groups[key];
            if (!g) {
                g = groups[key] = {key: key, label: label, empty: key === '', items: [], count: 0, free: 0, problems: 0,
                                   benches: [], location: null};
                keys.push(key);
            }
            if (g.items.indexOf(d) >= 0) return;
            g.items.push(d);
            g.count++;
            if (boardFree(d, catalog, nowMs)) g.free++;
            if (boardProblem(d, nowMs)) g.problems++;
            if (d.bench && g.benches.indexOf(d.bench) < 0) g.benches.push(d.bench);
            if (!g.location && d.bench_location) g.location = d.bench_location;
        }
        if (!by) {
            devices.forEach(function (d) { add('*', '', d); });
            return keys.length ? [groups['*']] : [];
        }
        var cat = (catalog || []).filter(function (c) { return c.id === by; })[0];
        devices.forEach(function (d) {
            var vals;
            if (by === 'bench') vals = d.bench ? [[d.bench, d.bench]] : [];
            else if (by === 'location') {
                var loc = String(d.bench_location || '').trim();
                vals = loc ? [[loc.toLowerCase(), loc]] : [];
            } else {
                vals = propValues((d.props || {})[by]).map(function (v) {
                    var f = cat ? (cat.values || []).filter(function (x) { return x.id === v; })[0] : null;
                    return [v, f && f.label ? f.label : v];
                });
            }
            if (!vals.length) add('', GROUP_EMPTY[by] || 'Sin ' + by, d);
            vals.forEach(function (v) { add(v[0], v[1], d); });
        });
        var order = cat ? (cat.values || []).map(function (v) { return v.id; }) : [];
        keys.sort(function (a, b) {
            if (a === '' || b === '') return a === '' ? 1 : -1;
            var ia = order.indexOf(a), ib = order.indexOf(b);
            if (ia >= 0 || ib >= 0) return (ia < 0 ? 1e9 : ia) - (ib < 0 ? 1e9 : ib);
            return groups[a].label.toLowerCase().localeCompare(groups[b].label.toLowerCase());
        });
        return keys.map(function (k) { return groups[k]; });
    }

    // Encabezado de un grupo (ocupa toda la fila de la grilla): ícono, nombre, cantidad, libres y con problemas.
    function groupHeaderHtml(g, by) {
        var icon = by === 'bench' ? 'server-2' : by === 'location' ? 'map-pin' :
                   g.empty ? 'circle-dashed' : propIcon(by, g.key, false);
        var sub = by === 'bench' ? g.location : by === 'location' ? g.benches.join(', ') : '';
        return '<div class="group-h' + (g.empty ? ' empty' : '') + '" data-group="' + escapeHtml(g.key) + '">' +
            '<span class="gh-ic"><i class="ti ti-' + icon + '" aria-hidden="true"></i></span>' +
            '<h3>' + escapeHtml(g.label) + '</h3>' +
            (sub ? '<span class="gh-sub">' + (by === 'bench' ? '<i class="ti ti-map-pin" aria-hidden="true"></i>' : '') + escapeHtml(sub) + '</span>' : '') +
            '<span class="gh-stat">' + g.count + (g.count === 1 ? ' placa' : ' placas') + '</span>' +
            (g.free ? '<span class="gh-stat ok">' + g.free + (g.free === 1 ? ' libre' : ' libres') + '</span>' : '') +
            (g.problems ? '<span class="gh-stat bad">' + g.problems + ' con problemas</span>' : '') +
        '</div>';
    }

    // ── Ubicación del bench (/api/version location; PATCH api/bench = override manual) ──

    var LOCATION_MAX = 60;

    // Para mostrar: {label, source: auto|manual, stale, icon, title} o null. loc: el objeto del server
    // (geo.location()), o un texto suelto (benches 0.41/0.42: manual).
    function locationView(loc, now) {
        if (typeof loc === 'string') loc = {label: loc, source: 'manual'};
        if (!loc || !loc.label) return null;
        var manual = loc.source === 'manual';
        var when = loc.ts ? relTime(loc.ts, now) : null;
        var detail = [loc.city, loc.region, loc.country].filter(Boolean).join(', ');
        return {label: loc.label, source: manual ? 'manual' : 'auto', stale: !!loc.stale,
                icon: manual ? 'map-pin' : 'current-location',
                title: manual ? 'Ubicación fijada a mano (pisa la automática)' :
                       'Ubicación automática por la IP pública' + (detail && detail !== loc.label ? ': ' + detail : '') +
                       (loc.tz ? ' (' + loc.tz + ')' : '') +
                       (loc.stale ? '. No se pudo actualizar' + (when ? ': es de ' + when : '') : (when ? ', ' + when : ''))};
    }

    // La regla de benchinfo.set_location: hasta 60 caracteres, sin control ni formato Unicode (Cf).
    function locationCheck(text) {
        var t = String(text == null ? '' : text).trim();
        if (t.length > LOCATION_MAX) return {ok: false, text: t, error: 'más de ' + LOCATION_MAX + ' caracteres'};
        if (/[\u0000-\u001f\u007f-\u009f]/.test(t) || /\p{Cf}/u.test(t)) {
            return {ok: false, text: t, error: 'sin saltos de línea, tabs ni caracteres invisibles'};
        }
        return {ok: true, text: t, error: null};
    }

    // ── Monitor (device.html): a dónde vuelve y la nota y propiedades en el header ──

    /*
     * A dónde vuelve "← Placas" del monitor. Abierto por el proxy de bench-master
     * (/bench/<n>/device.html), al master (`/`); si no, al home del bench (el directorio de la página).
     * Si la página anterior (referrer, mismo origen) es ese home, se vuelve a esa URL: conserva su
     * ?q= y ?group=. Por el master, si se vino del dashboard del bench por el proxy (/bench/<n>/), a ese.
     * → {home, href, master: nombre del bench si es por el master, o null}. `home` es a donde filtran los chips.
     */
    function monitorNav(pathname, referrer, origin) {
        var m = /^(.*?)\/bench\/([^/]+)\/device\.html$/.exec(pathname || '');
        var home = m ? m[1] + '/' : basePath(pathname || '/');
        var out = {home: home, href: home, master: null};
        if (m) { try { out.master = decodeURIComponent(m[2]); } catch (e) { out.master = m[2]; } }
        var r = null;
        try { r = referrer ? new URL(referrer) : null; } catch (e) { r = null; }
        var benchHome = basePath(pathname || '/');
        var homes = [home, home + 'index.html'].concat(m ? [benchHome, benchHome + 'index.html'] : []);
        if (r && r.origin === origin && homes.indexOf(r.pathname) >= 0) out.href = r.pathname + r.search;
        return out;
    }

    // Home filtrada por una propiedad (click en un chip del monitor): el master o el bench, según nav.
    function navFilterHref(nav, filter) {
        return nav.home + '?q=' + encodeURIComponent(filter);
    }

    /*
     * ¿El bench guarda nota y propiedades? false si /api/properties dio 404 (catalogStatus) o si
     * el device no trae `props` ni `note` (un server anterior); null si todavía no se sabe.
     */
    function metaSupported(d, catalogStatus) {
        if (catalogStatus === 404) return false;
        if (d && d.mac && !('props' in d) && !('note' in d)) return false;
        return catalogStatus ? true : null;
    }

    var META_OFF_TITLE = 'Este bench no soporta notas ni propiedades: actualizalo';

    /*
     * Nota y propiedades en el header del monitor, compactas: los chips (mismo orden y roles que la
     * card), la nota resumida (texto completo en el tooltip; click: editarla) y los botones de editar.
     * Sin nada: un solo botón (+) que abre el menú. supported === false: un ícono deshabilitado.
     * Los botones llevan data-act="add|props|note" (la página abre el popover).
     */
    function monitorMetaHtml(d, catalog, supported, now) {
        if (!d || !d.mac) return '';
        if (supported === false) {
            return '<span class="meta-btn icon meta-off" role="img" title="' + META_OFF_TITLE + '" aria-label="' + META_OFF_TITLE + '">' +
                   '<i class="ti ti-tag-off" aria-hidden="true"></i></span>';
        }
        var n = noteInfo(d, now);
        var cp = cardProps(d.props, catalog);
        var hard = cp.estado.filter(function (p) { return p.warn; });
        var soft = cp.estado.filter(function (p) { return !p.warn; });
        var chips = hard.concat(cp.spec, cp.tag, soft);
        if (!n && !chips.length) {
            return '<button class="meta-btn icon" data-act="add" title="Agregar propiedades (chip, conectividad, uso, estado) o una nota" ' +
                   'aria-label="Agregar propiedades o una nota"><i class="ti ti-plus" aria-hidden="true"></i></button>';
        }
        return chips.map(cardPropHtml).join('') +
            (n ? '<button class="note-chip" data-act="note" title="' + escapeHtml(n.title + '\n(click: editar)') + '">' +
                 '<i class="ti ti-note" aria-hidden="true"></i><span class="note-chip-text">' + escapeHtml(n.text) + '</span></button>' : '') +
            '<button class="meta-btn icon" data-act="props" title="' + (chips.length ? 'Editar las propiedades' : 'Agregar propiedades') +
                '" aria-label="Propiedades"><i class="ti ti-adjustments-horizontal" aria-hidden="true"></i></button>' +
            (n ? '' : '<button class="meta-btn icon" data-act="note" title="Agregar una nota" aria-label="Agregar una nota">' +
                      '<i class="ti ti-note" aria-hidden="true"></i></button>');
    }

    // Gráfico de área (SVG) para una serie: curva suave, relleno, marcas en los índices de `marks`.
    function areaChartSvg(values, marks, w, h, opts) {
        opts = opts || {};
        var pad = opts.pad || 14, inset = opts.inset || 12;
        var max = Math.max(1, Math.max.apply(null, values.concat([0])) * 1.25);
        var n = values.length;
        var pts = values.map(function (v, i) {
            return [inset + (n > 1 ? i / (n - 1) : 0) * (w - 2 * inset), h - pad - v / max * (h - 2 * pad)];
        });
        var d = 'M' + pts[0][0].toFixed(1) + ',' + pts[0][1].toFixed(1);
        for (var i = 1; i < n; i++) {
            var p = pts[i - 1], c = pts[i], mx = ((p[0] + c[0]) / 2).toFixed(1);
            d += ' C' + mx + ',' + p[1].toFixed(1) + ' ' + mx + ',' + c[1].toFixed(1) + ' ' + c[0].toFixed(1) + ',' + c[1].toFixed(1);
        }
        var dots = (marks || []).map(function (i) {
            return '<circle class="mark" cx="' + pts[i][0].toFixed(1) + '" cy="' + pts[i][1].toFixed(1) + '" r="5"/>';
        }).join('');
        return {points: pts, svg: '<svg viewBox="0 0 ' + w + ' ' + h + '" preserveAspectRatio="none" role="img">' +
            '<defs><linearGradient id="area-fill" x1="0" x2="0" y1="0" y2="1"><stop offset="0" class="area-stop-a"/><stop offset="1" class="area-stop-b"/></linearGradient></defs>' +
            '<path class="area" d="' + d + ' L' + pts[n - 1][0].toFixed(1) + ',' + h + ' L' + pts[0][0].toFixed(1) + ',' + h + ' Z"/>' +
            '<path class="line" d="' + d + '"/>' + dots + '</svg>'};
    }

    return {
        fmtDur: fmtDur, secondsUntil: secondsUntil, expiresText: expiresText, lockInfo: lockInfo,
        forceConfirmText: forceConfirmText, searchMatch: searchMatch,
        propValues: propValues, PROP_CATEGORIES: PROP_CATEGORIES, avoided: avoided, avoidedBy: avoidedBy, noteInfo: noteInfo, noteHtml: noteHtml, noteCheck: noteCheck, NOTE_MAX: NOTE_MAX,
        mergeCatalogs: mergeCatalogs, propChips: propChips, propChipsHtml: propChipsHtml,
        propsSearchText: propsSearchText, propsEditModel: propsEditModel, propsPatch: propsPatch, propValueId: propValueId,
        EVENT_TYPES: EVENT_TYPES, EVENT_ORDER: EVENT_ORDER, eventDetail: eventDetail, eventView: eventView,
        eventCounts: eventCounts, eventContext: eventContext, cursorSession: cursorSession, parseCursor: parseCursor,
        safeType: safeType, mergeEvents: mergeEvents, backoffMs: backoffMs,
        sessionLabel: sessionLabel, sessionFile: sessionFile, expandRangeLine: expandRangeLine, findLine: findLine,
        cursorOffset: cursorOffset,
        lineMark: lineMark, tsMs: tsMs, EventMarks: EventMarks,
        errorText: errorText, errorCode: errorCode, withToken: withToken,
        escapeHtml: escapeHtml, parseLocal: parseLocal, relTime: relTime, fmtBytes: fmtBytes,
        isoLocal: isoLocal, sessionStart: sessionStart,
        healthBadges: healthBadges, healthLevel: healthLevel,
        splitPrefix: splitPrefix, stripAnsi: stripAnsi, lineClass: lineClass, isProblem: isProblem,
        ansiLineToHtml: ansiLineToHtml, overwrite: overwrite, LineBuffer: LineBuffer,
        cardState: cardState, stateBadgeHtml: stateBadgeHtml, fwRows: fwRows,
        lastFlashHtml: lastFlashHtml, summarize: summarize, basePath: basePath, wsUrl: wsUrl,
        SILENT_S: SILENT_S, silentFor: silentFor, boardStatus: boardStatus, uptimeParts: uptimeParts, uptimeText: uptimeText, agoText: agoText,
        activityTotals: activityTotals, activityBarsHtml: activityBarsHtml, benchChips: benchChips, boardCardHtml: boardCardHtml, boardMetaHtml: boardMetaHtml,
        cardProps: cardProps, propIcon: propIcon, benchTagHtml: benchTagHtml,
        groupOptions: groupOptions, pickGroup: pickGroup, groupBoards: groupBoards, groupHeaderHtml: groupHeaderHtml,
        boardFree: boardFree, locationCheck: locationCheck, locationView: locationView, LOCATION_MAX: LOCATION_MAX,
        monitorNav: monitorNav, navFilterHref: navFilterHref, metaSupported: metaSupported,
        monitorMetaHtml: monitorMetaHtml, META_OFF_TITLE: META_OFF_TITLE,
        areaChartSvg: areaChartSvg
    };
});
