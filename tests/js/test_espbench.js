// node --test tests/js/test_espbench.js  (también lo corre pytest: tests/test_dashboard_js.py)
const test = require('node:test');
const assert = require('node:assert/strict');
const EB = require('../../remote/dashboard/espbench.js');

test('relTime: local sin zona', () => {
    const now = new Date(2026, 9, 5, 16, 0, 0).getTime();
    assert.equal(EB.relTime('2026-10-05T15:59:50', now), 'recién');
    assert.equal(EB.relTime('2026-10-05T15:55:00', now), 'hace 5 min');
    assert.equal(EB.relTime('2026-10-05T13:00:00', now), 'hace 3 h');
    assert.equal(EB.relTime('2026-10-03T16:00:00', now), 'hace 2 d');
    assert.equal(EB.relTime('2026-01-01T00:00:00', now), '2026-01-01');
    assert.equal(EB.relTime(null, now), null);
});

test('fmtBytes', () => {
    assert.equal(EB.fmtBytes(512), '512 B');
    assert.equal(EB.fmtBytes(2048), '2.0 KB');
    assert.equal(EB.fmtBytes(3 * 1024 * 1024), '3.0 MB');
});

test('healthBadges: sano no muestra nada', () => {
    assert.deepEqual(EB.healthBadges({boots: 1, panics: 0, boot_loop: false, last_reset: {reason: 'POWERON_RESET', abnormal: false}}), []);
    assert.deepEqual(EB.healthBadges(null), []);
    assert.equal(EB.healthLevel({boots: 1, panics: 0}), 'ok');
});

test('healthBadges: panic + reinicios', () => {
    const b = EB.healthBadges({boots: 3, panics: 1, boot_loop: false,
        last_panic: {kind: 'guru', detail: 'LoadProhibited', ts: '2026-10-05T16:00:00'},
        last_reset: {reason: 'SW_CPU_RESET', abnormal: false}});
    assert.deepEqual(b.map(x => x.cls), ['hb-panic', 'hb-info']);
    assert.equal(b[0].text, '⚠ 1 panic');
    assert.match(b[0].title, /Guru Meditation \(LoadProhibited\)/);
    assert.equal(b[1].text, '↻ 2');
    assert.equal(EB.healthLevel({panics: 1}), 'bad');
});

test('healthBadges: boot loop tapa reset y contador', () => {
    const b = EB.healthBadges({boots: 5, panics: 0, boot_loop: true,
        last_reset: {reason: 'TG1WDT_SYS_RESET', abnormal: true}});
    assert.deepEqual(b.map(x => x.cls), ['hb-loop']);
});

test('healthBadges: reset anormal sin panic = warn', () => {
    const h = {boots: 1, panics: 0, last_reset: {reason: 'RTCWDT_BROWN_OUT_RESET', abnormal: true}};
    assert.deepEqual(EB.healthBadges(h).map(x => x.cls), ['hb-warn']);
    assert.equal(EB.healthLevel(h), 'warn');
});

test('lineClass', () => {
    assert.equal(EB.lineClass('2026-10-05 16:00:00 | WARN  | protocol       | algo'), 'ln-tl ln-tl-warn');
    assert.equal(EB.lineClass('2026-10-05 16:00:00 | ERROR | flash          | x'), 'ln-tl ln-tl-error');
    assert.equal(EB.lineClass("Guru Meditation Error: Core  0 panic'ed (LoadProhibited)."), 'ln-panic');
    assert.equal(EB.lineClass('Backtrace: 0x400d1234:0x3ffb0000'), 'ln-panic');
    assert.equal(EB.lineClass('rst:0x8 (TG1WDT_SYS_RESET),boot:0x13'), 'ln-panic');
    assert.equal(EB.lineClass('rst:0x1 (POWERON_RESET),boot:0x13'), 'ln-reset');
    assert.equal(EB.lineClass('I (123) app: hola'), '');
    assert.equal(EB.lineClass('E (123) wifi: fallo'), 'ln-esp-e');
    assert.equal(EB.lineClass('W (123) wifi: ojo'), 'ln-esp-w');
    assert.ok(EB.isProblem('ln-tl ln-tl-error') && EB.isProblem('ln-esp-w') && EB.isProblem('ln-panic'));
    assert.ok(!EB.isProblem('ln-tl ln-tl-info') && !EB.isProblem(''));
});

test('ansiLineToHtml: colores, escape y secuencias no-SGR', () => {
    assert.equal(EB.ansiLineToHtml('\x1b[0;32mI (1) x: <ok>\x1b[0m'),
                 '<span style="color:#3fb950;">I (1) x: &lt;ok&gt;</span>');
    assert.equal(EB.ansiLineToHtml('a\x1b[Kb'), 'ab');
});

test('LineBuffer: chunks partidos, CRLF partido y overwrite', () => {
    const lb = new EB.LineBuffer();
    assert.deepEqual(lb.push('hola mu'), []);
    assert.equal(lb.partialView(), 'hola mu');
    assert.deepEqual(lb.push('ndo\r'), []);
    assert.deepEqual(lb.push('\nsegunda\r\r\n'), ['hola mundo', 'segunda']);
    assert.deepEqual(lb.push('10%\r50%\r100%\n'), ['100%']);
    assert.deepEqual(lb.push('progreso 10%\r'), []);
    assert.equal(lb.partialView(), 'progreso 10%');
    assert.deepEqual(lb.push('progreso 90%\n'), ['progreso 90%']);
});

// ── Prefijo del DeviceLog ("YYYY-MM-DD HH:MM:SS.mmm <origen> ") ─────────

test('splitPrefix: serial, taglog, continuación y línea vieja', () => {
    assert.deepEqual(EB.splitPrefix('2026-10-05 16:02:03.123 > I (1) app: hola'),
        {date: '2026-10-05', time: '16:02:03.123', origin: '>', prefix: '2026-10-05 16:02:03.123 > ',
         body: 'I (1) app: hola'});
    assert.equal(EB.splitPrefix('2026-10-05 16:02:03.123 | INFO  | x | y').origin, '|');
    assert.equal(EB.splitPrefix('2026-10-05 16:02:03.123 ↪ resto').body, 'resto');
    const old = EB.splitPrefix('I (1) app: sin prefijo');
    assert.equal(old.time, null);
    assert.equal(old.body, 'I (1) app: sin prefijo');
    // taglog viejo: tiene hora pero no milisegundos ni origen → no es prefijo
    assert.equal(EB.splitPrefix('2026-10-05 16:00:00 | WARN  | protocol       | algo').origin, null);
});

test('lineClass con prefijo: las regex ancladas miran el cuerpo', () => {
    const P = '2026-10-05 16:02:03.123 ';
    assert.equal(EB.lineClass(P + '| WARN  | protocol       | algo'), 'ln-tl ln-tl-warn');
    assert.equal(EB.lineClass(P + '| ERROR | flash          | x'), 'ln-tl ln-tl-error');
    assert.equal(EB.lineClass(P + '| INFO  | device         | a -> b'), 'ln-tl ln-tl-info');
    assert.equal(EB.lineClass(P + '> rst:0x1 (POWERON_RESET),boot:0x13'), 'ln-reset');
    assert.equal(EB.lineClass(P + '> rst:0x8 (TG1WDT_SYS_RESET),boot:0x13'), 'ln-panic');
    assert.equal(EB.lineClass(P + '> Backtrace: 0x400d1234:0x3ffb0000'), 'ln-panic');
    assert.equal(EB.lineClass(P + '> E (123) wifi: fallo'), 'ln-esp-e');
    assert.equal(EB.lineClass(P + '↪ W (123) wifi: ojo'), 'ln-esp-w');
    assert.equal(EB.lineClass(P + '> I (123) app: hola'), '');
    // una línea serial que parece taglog no es taglog
    assert.equal(EB.lineClass(P + '> INFO  | x | y'), '');
});

test('overwrite conserva el prefijo', () => {
    assert.equal(EB.overwrite('2026-10-05 16:02:03.123 > 10%\r50%\r100%'), '2026-10-05 16:02:03.123 > 100%');
    assert.equal(EB.overwrite('10%\r100%'), '100%');
    assert.equal(EB.overwrite('2026-10-05 16:02:03.123 > sin cr'), '2026-10-05 16:02:03.123 > sin cr');
});

test('LineBuffer con líneas prefijadas', () => {
    const lb = new EB.LineBuffer();
    const out = lb.push('2026-10-05 16:02:03.123 > a 10%\r90%\n2026-10-05 16:02:03.200 | INFO  | x              | y\n');
    assert.deepEqual(out, ['2026-10-05 16:02:03.123 > 90%', '2026-10-05 16:02:03.200 | INFO  | x              | y']);
});

test('sesiones: el nombre es el inicio (con pid); los viejos no', () => {
    assert.equal(EB.sessionStart('output_20261005_160203_812.log'), '2026-10-05T16:02:03');
    assert.equal(EB.sessionStart('output_20261005_160203.log'), null);   // log viejo: hora de rotación
    assert.equal(EB.sessionStart('output.log'), null);
    const epoch = new Date(2026, 9, 5, 16, 2, 3).getTime() / 1000;
    assert.equal(EB.isoLocal(epoch), '2026-10-05T16:02:03');
});

test('errorText / errorCode: detail string u objeto', () => {
    assert.equal(EB.errorText({detail: 'device ocupado'}, 409), 'device ocupado');
    assert.equal(EB.errorText({detail: {error: 'locked', message: "reservada por 'juan'"}}, 423), "reservada por 'juan'");
    assert.equal(EB.errorText({detail: {error: 'auth'}}, 401), 'auth');
    assert.equal(EB.errorText(null, 502), 'HTTP 502');
    assert.equal(EB.errorCode({detail: {error: 'locked'}}), 'locked');
    assert.equal(EB.errorCode({detail: 'texto'}), null);
});

test('withToken: agrega Authorization sin pisar headers ni mutar opts', () => {
    const opts = {method: 'POST', headers: {'Content-Type': 'application/json'}};
    const out = EB.withToken(opts, 's3cret');
    assert.deepEqual(out.headers, {'Content-Type': 'application/json', 'Authorization': 'Bearer s3cret'});
    assert.deepEqual(opts.headers, {'Content-Type': 'application/json'});
    assert.deepEqual(EB.withToken(undefined, ''), {});
});

// ── Reservas: "vence en", lock del flash, vencida = inexistente ─────────

const NOW = new Date(2026, 9, 6, 16, 0, 0).getTime();

test('fmtDur', () => {
    assert.equal(EB.fmtDur(45), '45 s');
    assert.equal(EB.fmtDur(20 * 60 + 10), '20 min');
    assert.equal(EB.fmtDur(2 * 3600 + 5 * 60), '2 h 5 min');
    assert.equal(EB.fmtDur(3 * 3600), '3 h');
    assert.equal(EB.fmtDur(3 * 86400 + 4 * 3600), '3 d 4 h');
    assert.equal(EB.fmtDur(-5), '0 s');
});

test('expiresText: cuenta regresiva y vencida = null', () => {
    assert.equal(EB.expiresText('2026-10-06T16:20:00', NOW), 'vence en 20 min');
    assert.equal(EB.expiresText('2026-10-06T16:00:30', NOW), 'vence en 30 s');
    assert.equal(EB.expiresText('2026-10-06T16:00:00', NOW), null);
    assert.equal(EB.expiresText('2026-10-06T15:59:00', NOW), null);
    assert.equal(EB.expiresText(null, NOW), null);
});

test('lockInfo: reserva, lock del flash, vencida y sin lock', () => {
    const r = EB.lockInfo({lock_user: 'juan', lock_expires: '2026-10-06T16:20:00'}, NOW);
    assert.equal(r.reservation, true);
    assert.equal(r.text, 'vence en 20 min');
    assert.equal(r.title, 'Reservada por juan hasta 2026-10-06 16:20:00');
    const f = EB.lockInfo({lock_user: 'ana', lock_expires: null}, NOW);
    assert.equal(f.reservation, false);
    assert.equal(f.text, 'sin vencimiento');
    assert.equal(EB.lockInfo({lock_user: 'juan', lock_expires: '2026-10-06T15:00:00'}, NOW), null);
    assert.equal(EB.lockInfo({lock_user: null}, NOW), null);
    assert.equal(EB.lockInfo(null, NOW), null);
});

test('forceConfirmText: quién y hasta cuándo', () => {
    const lock = EB.lockInfo({lock_user: 'juan', lock_expires: '2026-10-06T16:20:00'}, NOW);
    const t = EB.forceConfirmText(lock, 'Mandar');
    assert.match(t, /reservada por juan \(vence en 20 min, hasta 16:20:00\)/);   // sin zona = hora local
    assert.match(t, /¿Mandar igual\? Queda registrado como forzado\./);
    assert.match(EB.forceConfirmText(null, 'Resetear', "reservada por 'x' hasta y"), /^reservada por 'x'/);
    const flash = EB.lockInfo({lock_user: 'ana', lock_expires: null}, NOW);
    assert.match(EB.forceConfirmText(flash, 'Soltarlo'), /^La placa tiene el lock del flash de ana \(sin vencimiento\)/);
});

// Este archivo corre con TZ=UTC y con TZ de Argentina (tests/test_dashboard_js.py):
// lo que viene con offset o epoch no puede depender de la zona del navegador.
test('lockInfo/parseLocal: lock_expires con offset y epoch, en cualquier TZ', () => {
    const now = Date.UTC(2026, 9, 6, 19, 0, 0);                  // 16:00 en la Pi (-03:00)
    const r = EB.lockInfo({lock_user: 'juan', lock_expires: '2026-10-06T16:20:00-03:00'}, now);
    assert.equal(r.text, 'vence en 20 min');
    assert.equal(EB.parseLocal('2026-10-06T16:20:00-03:00').getTime(), Date.UTC(2026, 9, 6, 19, 20, 0));
    assert.equal(EB.parseLocal('2026-10-06T19:20:00Z').getTime(), Date.UTC(2026, 9, 6, 19, 20, 0));
    assert.equal(EB.parseLocal('2026-10-06T22:20:00+0300').getTime(), Date.UTC(2026, 9, 6, 19, 20, 0));
    // el epoch manda sobre el ISO (aunque el ISO venga sin zona, de una Pi vieja)
    const e = EB.lockInfo({lock_user: 'juan', lock_expires: '2026-10-06T16:20:00',
                           lock_expires_epoch: Date.UTC(2026, 9, 6, 19, 30, 0) / 1000}, now);
    assert.equal(e.text, 'vence en 30 min');
    assert.equal(EB.lockInfo({lock_user: 'juan', lock_expires: '2026-10-06T15:59:00-03:00'}, now), null);
    // until: la hora en el reloj del navegador
    assert.equal(EB.tsMs(r.until), Date.UTC(2026, 9, 6, 19, 20, 0));
});

test('searchMatch: texto libre o @usuario del lock', () => {
    assert.ok(EB.searchMatch('', 'lo que sea', null));
    assert.ok(EB.searchMatch('Board', 'board1 ttyusb0', null));
    assert.ok(!EB.searchMatch('xx', 'board1', 'xx'));       // @ para el lock
    assert.ok(EB.searchMatch('@ju', 'board1', 'Juan'));
    assert.ok(EB.searchMatch('@', 'board1', 'juan'));
    assert.ok(!EB.searchMatch('@', 'board1', null));
    assert.ok(!EB.searchMatch('@ana', 'board1 ana', 'juan'));
    // los contadores del header: reservas y locks del flash por separado
    assert.ok(EB.searchMatch('lock:reserva', 'b', 'juan', 'reserva'));
    assert.ok(!EB.searchMatch('lock:reserva', 'b', 'ana', 'flash'));
    assert.ok(EB.searchMatch('lock:flash', 'b', 'ana', 'flash'));
    assert.ok(!EB.searchMatch('lock:flash', 'b', null, ''));
});

// ── Eventos ───────────────────────────────────────────────────────────

const SID = '20261006_155000_812';
const ev = (type, detail, ts = '2026-10-06T16:02:03.123', off = 100) =>
    ({ts, type, cursor: `c:${SID}:${off}`, detail});

test('eventView: icono, hora, detalle corto y quién', () => {
    const p = EB.eventView(ev('panic', {kind: 'guru', reason: 'LoadProhibited', line: "Guru Meditation Error: Core 1 panic'ed"}));
    assert.equal(p.icon, '⚠');
    assert.equal(p.cls, 'ev-panic');
    assert.equal(p.time, '16:02:03');
    assert.equal(p.date, '2026-10-06');
    assert.equal(p.detail, 'Guru Meditation (LoadProhibited)');
    assert.equal(p.session, SID);
    assert.match(p.title, /Guru Meditation Error/);
    const s = EB.eventView(ev('send', {text: 'status', enter: true, user: 'alejo', forced: true}));
    assert.equal(s.detail, '"status" ⏎ · forzado');
    assert.equal(s.who, 'alejo');
    assert.equal(EB.eventView(ev('boot_loop', {phase: 'start', boots: 3})).cls, 'ev-boot-loop');
    // los panics del loop van en el end (no como eventos sueltos)
    assert.equal(EB.eventDetail(ev('boot_loop', {phase: 'end', boots: 40, panics: 38, first_panic: 'guru',
                                                 last_panic: 'abort'})),
                 'fin · 40 arranques · 38 panics (Guru Meditation … abort())');
    assert.equal(EB.eventDetail(ev('boot_loop', {phase: 'end', boots: 6, panics: 1, first_panic: 'guru',
                                                 last_panic: 'guru'})), 'fin · 6 arranques · 1 panic (Guru Meditation)');
    assert.equal(EB.eventDetail(ev('boot_loop', {phase: 'end', boots: 6, panics: 0})), 'fin · 6 arranques');
    assert.ok(p.bad && !s.bad);
    assert.ok(EB.eventView(ev('flash', {ok: false})).bad && !EB.eventView(ev('flash', {ok: true})).bad);
    assert.ok(EB.eventView(ev('boot', {abnormal: true})).bad && !EB.eventView(ev('boot', {})).bad);
    assert.equal(EB.eventDetail(ev('boot', {reason: 'TG1WDT_SYS_RESET', abnormal: true})), 'TG1WDT_SYS_RESET ⚠');
    assert.equal(EB.eventDetail(ev('state', {from: 'monitoring', to: 'flashing'})), 'monitoreando → flasheando');
    assert.equal(EB.eventDetail(ev('flash', {ok: false, error: 'esptool_failed'})), '✗ esptool failed');
    assert.equal(EB.eventDetail(ev('flash', {ok: true, status: 'exitoso'})), '✓ exitoso');
    assert.equal(EB.eventDetail(ev('fw', {project: 'simfw', version: '1.0.0', idf: 'v5.3'})), 'simfw 1.0.0 · IDF v5.3');
    assert.equal(EB.eventDetail(ev('reserve', {user: 'juan', expires: '2026-10-06T16:30:00'})), 'hasta 2026-10-06 16:30');
    assert.equal(EB.eventDetail(ev('release', {user: 'juan'})), '');
    // release forzado: el dueño anterior en el detalle y quién forzó como "quién"
    const rel = EB.eventView(ev('release', {user: 'juan', forced: true, by_user: 'dash', by_host: '10.0.0.9'}));
    assert.equal(rel.detail, 'de juan · forzada por dash (10.0.0.9)');
    assert.equal(rel.who, 'dash');
    assert.equal(EB.eventView(ev('release', {user: 'juan', forced: true, by_host: '10.0.0.9'})).who, '10.0.0.9');
    assert.equal(EB.eventDetail(ev('send', {text: 'x', forced: true, by_host: '10.0.0.3'})), '"x" · forzado por 10.0.0.3');
    assert.equal(EB.eventDetail(ev('command', {command: 'restart-session', user: 'dash'})), 'reiniciar sesión');
    assert.equal(EB.eventView({ts: 'x', type: 'nuevo', cursor: null}).icon, '·');   // tipo desconocido
});

test('eventCounts: agrupado por tipo, en orden de gravedad', () => {
    const c = EB.eventCounts([ev('boot', {}), ev('send', {}), ev('panic', {}), ev('boot', {}), ev('raro', {})]);
    assert.deepEqual(c.map(x => [x.type, x.n]), [['panic', 1], ['boot', 2], ['send', 1], ['raro', 1]]);
    assert.deepEqual(EB.eventCounts([]), []);
});

test('eventContext: panic de rst: a rst:, el resto con líneas antes/después', () => {
    assert.deepEqual(EB.eventContext(ev('panic', {})), {around: `c:${SID}:100`, raw: '1', max_lines: '2000'});
    assert.deepEqual(EB.eventContext(ev('send', {})),
        {around: `c:${SID}:100`, raw: '1', max_lines: '2000', before: '40', after: '200'});
    assert.equal(EB.eventContext({type: 'boot', cursor: null}), null);
});

test('cursores y sesiones: otra sesión va a su archivo rotado', () => {
    assert.equal(EB.cursorSession(`c:${SID}:48213`), SID);
    assert.equal(EB.cursorSession('now'), null);
    assert.equal(EB.sessionLabel(SID), '2026-10-06 15:50:00');
    assert.equal(EB.sessionFile(SID, SID), 'output.log');
    assert.equal(EB.sessionFile(SID, '20261006_170000_900'), `output_${SID}.log`);
});

test('expandRangeLine / findLine: la línea del evento dentro del contexto', () => {
    assert.equal(EB.expandRangeLine('16:02:03.123 > hola', '2026-10-06'), '2026-10-06 16:02:03.123 > hola');
    assert.equal(EB.expandRangeLine('2026-10-05 23:59:59.000 > ayer', '2026-10-06'), '2026-10-05 23:59:59.000 > ayer');
    assert.equal(EB.expandRangeLine('… 3 líneas omitidas …', '2026-10-06'), '… 3 líneas omitidas …');
    const lines = ['16:02:03.000 > rst:0x1 (POWERON_RESET)', '16:02:03.123 > esp> ', '16:02:03.200 ↪ status'];
    assert.equal(EB.findLine(lines, '16:02:03.200 ↪ status', '2026-10-06', '2026-10-06'), 2);
    // el contexto cruzó la medianoche: la línea del evento viene con fecha en uno y sin ella en el otro
    assert.equal(EB.findLine(['23:59:59.000 > a', '2026-10-07 00:00:01.000 > b'], '00:00:01.000 > b',
                             '2026-10-06', '2026-10-07'), 1);
    assert.equal(EB.findLine(lines, null, '2026-10-06', '2026-10-06'), -1);
    assert.equal(EB.findLine(lines, '16:09:00.000 > otra', '2026-10-06', '2026-10-06'), -1);
});

test('lineMark: boot y el inicio del panic, no el backtrace ni taglog', () => {
    const P = '2026-10-06 16:02:03.123 ';
    assert.equal(EB.lineMark(P + '> rst:0x1 (POWERON_RESET),boot:0x13 (SPI_FAST_FLASH_BOOT)'), 'boot');
    assert.equal(EB.lineMark(P + "> Guru Meditation Error: Core  1 panic'ed (LoadProhibited)."), 'panic');
    assert.equal(EB.lineMark(P + '↪ abort() was called at PC 0x400d1234'), 'panic');
    assert.equal(EB.lineMark(P + '> \x1b[0;31massert failed: x\x1b[0m'), 'panic');
    assert.equal(EB.lineMark(P + '> Backtrace: 0x400d1234:0x3ffb0000'), null);
    assert.equal(EB.lineMark(P + '> Rebooting...'), null);
    assert.equal(EB.lineMark(P + '| INFO  | device         | rst:0x1 (POWERON_RESET) visto'), null);
    assert.equal(EB.lineMark('rst:0xc (SW_CPU_RESET),boot:0x13'), 'boot');    // log viejo sin prefijo
    assert.equal(EB.lineMark(P + '> I (1) app: hola'), null);
});

test('tsMs: milisegundos de la hora del log y de los eventos', () => {
    assert.equal(EB.tsMs('2026-10-06T16:02:03.123') - EB.tsMs('2026-10-06 16:02:03.000'), 123);
    assert.equal(EB.tsMs('2026-10-06T16:02:03.5') - EB.tsMs('2026-10-06T16:02:03'), 500);
    assert.equal(EB.tsMs('basura'), null);
});

const L = (h) => '2026-10-06T' + h;
const feedAll = (m, lines) => lines.flatMap(([h, text], i) => m.feed(i, L(h), text).map(x => [x.ev.cursor, x.handle]));

test('EventMarks: send al eco (la línea con su texto), aunque llegue antes del evento', () => {
    const m = new EB.EventMarks();
    assert.equal(m.add([{type: 'send', cursor: 's1', ts: L('10:00:01.000'), detail: {text: 'status'}}]), 1);
    assert.equal(m.add([{type: 'send', cursor: 's1', ts: L('10:00:01.000'), detail: {text: 'status'}}]), 0);  // ya visto
    assert.deepEqual(feedAll(m, [['10:00:00.700', 'tick'], ['10:00:00.900', 'I (5) otra'],
                                 ['10:00:00.950', 'esp> status'], ['10:00:01.010', 'OK']]), [['s1', 2]]);
});

test('EventMarks: dos sends iguales a 300 ms van cada uno a su eco', () => {
    const m = new EB.EventMarks();
    m.add([{type: 'send', cursor: 's1', ts: L('10:00:01.000'), detail: {text: 'status'}},
           {type: 'send', cursor: 's2', ts: L('10:00:01.300'), detail: {text: 'status'}}]);
    assert.deepEqual(feedAll(m, [['10:00:00.990', 'status'], ['10:00:01.050', 'OK'],
                                 ['10:00:01.290', 'status'], ['10:00:01.350', 'OK']]), [['s1', 0], ['s2', 2]]);
});

test('EventMarks: sin eco, el send va a la primera línea de la ventana (al pasar 2 s o con flush)', () => {
    const m = new EB.EventMarks();
    m.add([{type: 'send', cursor: 's1', ts: L('10:00:01.000'), detail: {text: 'nada'}}]);
    assert.deepEqual(feedAll(m, [['10:00:01.100', 'a'], ['10:00:01.500', 'b']]), []);
    assert.deepEqual(m.feed(9, L('10:00:03.200'), 'c').map(x => x.handle), [0]);
    const f = new EB.EventMarks();
    f.add([{type: 'send', cursor: 's1', ts: L('10:00:01.000'), detail: {text: 'nada'}}]);
    f.feed('a', L('10:00:01.100'), 'a');
    assert.deepEqual(f.flush(EB.tsMs(L('10:00:02.000'))), []);
    assert.deepEqual(f.flush(EB.tsMs(L('10:00:03.100'))).map(x => x.handle), ['a']);
});

test('EventMarks: command/flash a la primera línea desde t − 150 ms, nunca a una de antes', () => {
    const m = new EB.EventMarks();
    m.add([{type: 'command', cursor: 'c1', ts: L('10:00:01.000')}, {type: 'flash', cursor: 'f1', ts: L('10:00:01.300')}]);
    assert.deepEqual(feedAll(m, [['10:00:00.700', 'x'], ['10:00:01.000', 'rst'], ['10:00:01.300', 'y']]),
                     [['c1', 1], ['f1', 2]]);
});

test('EventMarks: evento sin línea después en 5 s no se marca; sin hora tampoco', () => {
    const m = new EB.EventMarks();
    m.add([{type: 'command', cursor: 'c3', ts: L('10:00:00.000')}]);
    assert.deepEqual(m.feed(0, L('10:10:00.000'), 'tick'), []);
    assert.equal(m.pending(), 0);
    const n = new EB.EventMarks();
    n.add([{type: 'command', cursor: 'c4', ts: L('10:00:00.000')}]);
    assert.deepEqual(n.feed(0, 'sin hora', 'x'), []);
    assert.equal(n.pending(), 1);                                    // sigue esperando su línea
});

test('EventMarks: el firmware imprimió el mismo texto justo antes del eco: gana el eco (el último antes de t)', () => {
    const m = new EB.EventMarks();
    m.add([{type: 'send', cursor: 's1', ts: L('10:00:00.070'), detail: {text: 'dup'}}]);
    assert.deepEqual(feedAll(m, [['10:00:00.061', 'dup'], ['10:00:00.061', 'dup'], ['10:00:00.067', 'dup'],
                                 ['10:00:00.126', "error: comando desconocido 'dup'"]]), [['s1', 2]]);
    // sin línea después de t: flush lo resuelve con el eco
    const f = new EB.EventMarks();
    f.add([{type: 'send', cursor: 's1', ts: L('10:00:00.070'), detail: {text: 'dup'}}]);
    f.feed('eco', L('10:00:00.067'), 'dup');
    assert.deepEqual(f.flush(EB.tsMs(L('10:00:00.500'))).map(x => x.handle), ['eco']);
});

test('findLine: entre líneas idénticas en el mismo ms gana la de las previas iguales', () => {
    const D = '2026-10-06';
    const ctx = ['10:00:00.050 > esp> ', '10:00:00.100 > dup', '10:00:00.100 > dup', '10:00:00.100 > dup',
                 '10:00:00.200 > x'];
    assert.equal(EB.findLine(ctx, ['10:00:00.050 > esp> ', '10:00:00.100 > dup', '10:00:00.100 > dup',
                                   '10:00:00.100 > dup'], D, D), 3);
    assert.equal(EB.findLine(ctx, ['10:00:00.050 > esp> ', '10:00:00.100 > dup'], D, D), 1);
    assert.equal(EB.findLine(ctx, '10:00:00.200 > x', D, D), 4);       // compat: una sola línea
    assert.equal(EB.findLine(ctx, [], D, D), -1);
    assert.equal(EB.cursorOffset('c:20261006_155000_812:48213'), 48213);
    assert.equal(EB.cursorOffset('now'), null);
});

test('eventCounts desde /events?counts=1; safeType', () => {
    assert.deepEqual(EB.eventCounts({panic: 150, boot_loop: 1, send: 2}).map(x => [x.type, x.n]),
                     [['panic', 150], ['boot_loop', 1], ['send', 2]]);
    assert.equal(EB.safeType('x" onmouseover="alert(1)'), 'xonmouseoveralert1');
    assert.equal(EB.eventView({ts: '', type: '<b>', cursor: null}).cls, 'ev-b');
});

test('mergeEvents: un boot_loop viejo entra en su lugar, sin repetidos', () => {
    const e = (type, sid, off) => ({type, cursor: `c:${sid}:${off}`, ts: 't' + off});
    const S1 = '20261006_100000_1', S2 = '20261006_110000_2';
    const main = [e('panic', S2, 500), e('panic', S2, 900)];
    const rare = [e('boot_loop', S2, 100), e('flash', S1, 50), e('panic', S2, 900)];
    assert.deepEqual(EB.mergeEvents(main, rare).map(x => x.type + x.cursor.slice(-3)),
                     ['flash:50', 'boot_loop100', 'panic500', 'panic900']);
});

test('backoffMs: duplica por error hasta el tope', () => {
    assert.equal(EB.backoffMs(4000, 0), 4000);
    assert.equal(EB.backoffMs(4000, 2), 16000);
    assert.equal(EB.backoffMs(4000, 5), 30000);
});

test('cardState: caído, ocupado, sin MAC, salud', () => {
    assert.equal(EB.cardState({status: 'DOWN', state: 'monitoring'}), 'st-down');
    assert.equal(EB.cardState({status: 'RUNNING', state: 'flashing'}), 'st-busy');
    assert.equal(EB.cardState({status: 'RUNNING', state: 'discovering'}), 'st-busy');
    assert.equal(EB.cardState({status: 'RUNNING', state: 'unknown'}), 'st-unknown');
    assert.equal(EB.cardState({status: 'RUNNING', state: 'monitoring', health: {boots: 1, panics: 0}}), 'st-ok');
    assert.equal(EB.cardState({status: 'RUNNING', state: 'monitoring', health: {boots: 3, panics: 0, boot_loop: true}}), 'st-bad');
});

test('summarize: sin MAC solo cuenta en el total; reservas y locks del flash aparte', () => {
    const c = EB.summarize([
        {status: 'RUNNING', state: 'monitoring', lock_user: 'ana'},                       // lock del flash
        {status: 'RUNNING', state: 'flashing', device_key: 'b1', lock_user: 'juan',
         lock_expires: '2026-10-06T16:20:00'},                                             // reserva vigente
        {status: 'DOWN', lock_user: 'x', lock_expires: '2026-10-06T15:00:00'},            // reserva vencida
        {status: 'RUNNING', state: 'unknown'},
    ], NOW);
    assert.deepEqual(c, {total: 4, ok: 1, busy: 1, bad: 0, down: 1, reserved: 1, flashLocked: 1,
                         who: ['juan → b1 (hasta 16:20)']});
});

test('stateBadgeHtml / fwRows / lastFlashHtml', () => {
    assert.equal(EB.stateBadgeHtml({state: 'monitoring'}), '');
    assert.match(EB.stateBadgeHtml({state: 'erasing'}), /BORRANDO/);
    assert.deepEqual(EB.fwRows({}), []);
    const rows = EB.fwRows({fw_project: 'SFY1-56_1', fw_version: '56.1', fw_idf: 'v5.3.2'});
    assert.deepEqual(rows.map(r => r.label), ['App', 'ESP-IDF']);
    assert.match(rows[0].html, />56\.1</);
    assert.equal(rows[0].title, 'Proyecto: SFY1-56_1');
    assert.equal(rows[1].html, 'v5.3.2');
    assert.match(EB.fwRows({fw_project: 'a<b'})[0].html, /a&lt;b/);       // sin versión: el proyecto
    assert.match(EB.lastFlashHtml({}), /nunca/);
    const now = new Date(2026, 9, 5, 16, 0, 0).getTime();
    assert.match(EB.lastFlashHtml({last_flash_ts: '2026-10-05T15:55:00', last_flash_ok: false, last_flash_user: 'ana'}, now),
                 /✗.*hace 5 min.*ana/);
});

test('basePath / wsUrl: directo y a través de bench-master', () => {
    assert.equal(EB.basePath('/'), '/');
    assert.equal(EB.basePath('/device.html'), '/');
    assert.equal(EB.basePath('/bench/sensipi02/device.html'), '/bench/sensipi02/');
    assert.equal(EB.basePath('/bench/sensipi02/'), '/bench/sensipi02/');
    assert.equal(EB.wsUrl({protocol: 'http:', host: 'pi:8080', pathname: '/device.html'}, 'ws/device/esp-slot1'),
                 'ws://pi:8080/ws/device/esp-slot1');
    assert.equal(EB.wsUrl({protocol: 'https:', host: 'localhost:8090', pathname: '/bench/b1/device.html'}, 'ws/device/x'),
                 'wss://localhost:8090/bench/b1/ws/device/x');
});
