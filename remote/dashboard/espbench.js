/*
 * espbench.js — lógica del dashboard sin DOM: formato, salud del device,
 * clasificación de líneas del log y el buffer incremental de la terminal.
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

    // Los timestamps del server son hora local sin zona ("2026-10-05T16:00:00").
    function parseLocal(iso) {
        if (!iso) return null;
        var m = /^(\d{4})-(\d\d)-(\d\d)[T ](\d\d):(\d\d):(\d\d)/.exec(iso);
        if (!m) return null;
        return new Date(+m[1], +m[2] - 1, +m[3], +m[4], +m[5], +m[6]);
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

    return {
        escapeHtml: escapeHtml, parseLocal: parseLocal, relTime: relTime, fmtBytes: fmtBytes,
        healthBadges: healthBadges, healthLevel: healthLevel,
        splitPrefix: splitPrefix, stripAnsi: stripAnsi, lineClass: lineClass, isProblem: isProblem,
        ansiLineToHtml: ansiLineToHtml, overwrite: overwrite, LineBuffer: LineBuffer
    };
});
