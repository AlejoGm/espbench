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
    assert.match(t, /reservada por juan \(vence en 20 min, hasta 16:20:00\)/);
    assert.match(t, /¿Mandar igual\? Queda registrado como forzado\./);
    assert.match(EB.forceConfirmText(null, 'Resetear', "reservada por 'x' hasta y"), /^reservada por 'x'/);
});

test('searchMatch: texto libre o @usuario del lock', () => {
    assert.ok(EB.searchMatch('', 'lo que sea', null));
    assert.ok(EB.searchMatch('Board', 'board1 ttyusb0', null));
    assert.ok(!EB.searchMatch('xx', 'board1', 'xx'));       // @ para el lock
    assert.ok(EB.searchMatch('@ju', 'board1', 'Juan'));
    assert.ok(EB.searchMatch('@', 'board1', 'juan'));
    assert.ok(!EB.searchMatch('@', 'board1', null));
    assert.ok(!EB.searchMatch('@ana', 'board1 ana', 'juan'));
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

test('EventMarks: cada evento a la primera línea con hora ≥ la suya (− slack), una sola vez', () => {
    const m = new EB.EventMarks(500);
    assert.equal(m.add([ev('send', {text: 'status'}, '2026-10-06T16:02:05.000'),
                        ev('flash', {ok: true}, '2026-10-06T16:02:10.000')]), 2);
    assert.equal(m.add([ev('send', {text: 'status'}, '2026-10-06T16:02:05.000')]), 0);   // ya visto
    assert.deepEqual(m.take('2026-10-06T16:02:04.000'), []);
    assert.ok(m.dueBy('2026-10-06T16:02:04.600'));
    // el eco llegó 100 ms antes de que el api registrara el send: cae igual en su línea
    assert.deepEqual(m.take('2026-10-06T16:02:04.900').map(e => e.type), ['send']);
    assert.deepEqual(m.take('2026-10-06T16:02:05.100'), []);
    assert.ok(!m.dueBy('2026-10-06T16:02:09.000'));
    assert.deepEqual(m.take('2026-10-06T16:02:11.000').map(e => e.type), ['flash']);
    assert.deepEqual(m.take('sin hora'), []);
});

test('EventMarks: dropBefore descarta los anteriores a la vista; reset los vuelve a aceptar', () => {
    const m = new EB.EventMarks(0);
    m.add([ev('command', {command: 'reset'}, '2026-10-06T15:00:00.000'), ev('send', {}, '2026-10-06T16:00:00.000')]);
    m.dropBefore('2026-10-06T15:30:00.000');
    assert.deepEqual(m.take('2026-10-06T16:00:00.000').map(e => e.type), ['send']);
    m.reset();
    assert.equal(m.add([ev('send', {}, '2026-10-06T16:00:00.000')]), 1);
});
