/*
 * meta.js — editores de la nota y las propiedades de una placa (window.EBMeta),
 * compartidos por index.html (cards) y device.html (header). La lógica sin DOM
 * (chips, validación, qué cambió) está en espbench.js; acá solo se arma DOM.
 *
 * Escrituras por EBAuth.fetch: PATCH api/devices/{mac} {note | props, user} y
 * POST api/properties/{cat}/values (valor nuevo). `user` = el lock_user recordado
 * (eb.lockUser); sin él, el server pone el host del pedido.
 */
(function () {
    'use strict';
    var esc = EB.escapeHtml;
    var catalog = null;         // GET api/properties (null: todavía no, o bench sin propiedades)
    var catalogP = null;
    var open = 0;               // editores abiertos: las páginas no re-renderizan mientras tanto

    function loadCatalog(force) {
        if (catalogP && !force) return catalogP;
        catalogP = fetch('api/properties')
            .then(function (r) { return r.ok ? r.json() : null; })
            .then(function (d) { catalog = d ? d.categories || [] : null; return catalog; })
            .catch(function () { catalogP = null; return catalog; });
        return catalogP;
    }

    function user() {
        try { return localStorage.getItem('eb.lockUser') || ''; } catch (e) { return ''; }
    }

    function send(method, url, body) {
        return EBAuth.fetch(url, {method: method, headers: {'Content-Type': 'application/json'},
                                  body: JSON.stringify(body)})
            .then(function (r) {
                return r.json().catch(function () { return null; }).then(function (d) {
                    if (!r.ok) throw new Error(EB.errorText(d, r.status));
                    return d;
                });
            });
    }

    function patch(device, body) {
        var u = user();
        if (u) body.user = u;
        else body.via = 'dashboard';        // note_by = dashboard@<ip> (lo arma el server)
        return send('PATCH', 'api/devices/' + encodeURIComponent(device.mac), body);
    }

    function track(done) {
        open++;
        var closed = false;
        return function (changed) {
            if (closed) return;
            closed = true;
            open--;
            done(changed);
        };
    }

    // Nota: input en `host` (reemplaza lo que tenga). onDone(changed) al guardar o cancelar.
    function noteEditor(host, device, onDone) {
        var done = track(onDone || function () {});
        host.innerHTML =
            '<div class="meta-edit note-edit">' +
                '<input class="note-input" maxlength="' + EB.NOTE_MAX + '" placeholder="Nota: testeando, no tocar, dev ana…">' +
                '<button class="rename-ok" title="Guardar (Enter)">✓</button>' +
                '<button class="rename-cancel" title="Cancelar (Esc)">✗</button>' +
                (device.note ? '<button class="meta-btn note-clear" title="Borrar la nota">borrar</button>' : '') +
                '<span class="meta-err"></span>' +
            '</div>';
        var inp = host.querySelector('.note-input');
        var err = host.querySelector('.meta-err');
        inp.value = device.note || '';
        inp.focus();
        inp.select();
        function save(text) {
            var c = EB.noteCheck(text);
            if (!c.ok) { err.textContent = c.error; return; }
            if (c.text === (device.note || '')) { done(false); return; }
            err.textContent = '';
            patch(device, {note: c.text}).then(function () { done(true); })
                .catch(function (e) { err.textContent = e.message || 'Error de conexión'; });
        }
        host.querySelector('.rename-ok').onclick = function () { save(inp.value); };
        host.querySelector('.rename-cancel').onclick = function () { done(false); };
        var clear = host.querySelector('.note-clear');
        if (clear) clear.onclick = function () { save(''); };
        inp.onkeydown = function (e) {
            if (e.key === 'Enter') save(inp.value);
            if (e.key === 'Escape') done(false);
        };
    }

    // Propiedades: un panel en `host` con cada categoría (un valor: select; varios: checkboxes)
    // y "+ valor" para agregar uno nuevo al catálogo del bench.
    function propsEditor(host, device, onDone) {
        var done = track(onDone || function () {});
        var before = device.props || {};
        var selection = null;   // {cat: [ids]}: sobrevive a recargar el catálogo (valor nuevo)

        function current() {
            var sel = {};
            host.querySelectorAll('[data-cat]').forEach(function (el) {
                var cat = el.dataset.cat;
                sel[cat] = sel[cat] || [];
                if (el.tagName === 'SELECT') { if (el.value) sel[cat].push(el.value); }
                else if (el.checked) sel[cat].push(el.value);
            });
            return sel;
        }

        function render(cat) {
            if (!cat) {
                host.innerHTML = '<div class="meta-edit props-edit"><span class="meta-err">Este bench no tiene ' +
                    'propiedades (actualizalo)</span> <button class="rename-cancel">✗</button></div>';
                host.querySelector('.rename-cancel').onclick = function () { done(false); };
                return;
            }
            var props = before;
            if (selection) {
                props = {};
                Object.keys(selection).forEach(function (k) { props[k] = selection[k]; });
            }
            var model = EB.propsEditModel(props, cat);
            host.innerHTML = '<div class="meta-edit props-edit">' + model.map(function (c) {
                var field;
                if (c.multi) {
                    field = c.values.map(function (v) {
                        return '<label class="prop-opt' + (v.warn ? ' prop-warn' : '') + '" title="' + esc(v.desc) + '">' +
                               '<input type="checkbox" data-cat="' + esc(c.id) + '" value="' + esc(v.id) + '"' +
                               (v.checked ? ' checked' : '') + '> ' + esc(v.label) + '</label>';
                    }).join('');
                } else {
                    field = '<select data-cat="' + esc(c.id) + '"><option value="">—</option>' + c.values.map(function (v) {
                        return '<option value="' + esc(v.id) + '"' + (v.checked ? ' selected' : '') + '>' +
                               esc(v.label) + (v.warn ? ' ⚠' : '') + '</option>';
                    }).join('') + '</select>';
                }
                return '<div class="prop-edit-row"><span class="prop-edit-label">' + esc(c.label) + '</span>' +
                       '<span class="prop-edit-field">' + field +
                       '<button class="meta-btn prop-new" data-new="' + esc(c.id) + '" title="Agregar un valor a ' +
                       esc(c.label) + ' (queda en el catálogo de este bench)">+ valor</button></span></div>';
            }).join('') +
            '<div class="prop-edit-actions"><button class="action-btn props-save">Guardar</button>' +
            '<button class="tool-btn props-cancel">Cancelar</button><span class="meta-err"></span></div></div>';
            var err = host.querySelector('.props-edit > .prop-edit-actions .meta-err');
            host.querySelector('.props-cancel').onclick = function () { done(false); };
            host.querySelector('.props-save').onclick = function () {
                var body = EB.propsPatch(before, current(), cat);
                if (!body) { done(false); return; }
                patch(device, {props: body}).then(function () { done(true); })
                    .catch(function (e) { err.textContent = e.message || 'Error de conexión'; });
            };
            host.querySelectorAll('.prop-new').forEach(function (b) {
                b.onclick = function () { newValue(b, b.dataset.new); };
            });
        }

        // Valor nuevo inline: id (slug) + opcional advertencia en `estado`.
        function newValue(btn, catId) {
            var row = btn.closest('.prop-edit-row');
            if (row.querySelector('.prop-new-form')) return;
            var f = document.createElement('span');
            f.className = 'prop-new-form';
            f.innerHTML = '<input placeholder="valor nuevo (ej. nb-iot)" maxlength="24">' +
                (catId === 'estado' ? '<label class="prop-opt prop-warn" title="Advertencia: pick y ls --free no ' +
                    'eligen una placa con este estado"><input type="checkbox"> advertencia</label>' : '') +
                '<button class="rename-ok" title="Agregar">✓</button><button class="rename-cancel">✗</button>' +
                '<span class="meta-err"></span>';
            row.appendChild(f);
            var inp = f.querySelector('input');
            var err = f.querySelector('.meta-err');
            inp.focus();
            function add() {
                var id = EB.propValueId(inp.value);
                if (!id) { err.textContent = 'minúsculas, números, . _ - (hasta 24)'; return; }
                var warn = catId === 'estado' && f.querySelector('input[type=checkbox]').checked;
                var keep = current();
                send('POST', 'api/properties/' + encodeURIComponent(catId) + '/values',
                     {id: id, label: inp.value.trim(), warn: warn, exclude_pick: warn})
                    .then(function () {
                        keep[catId] = (keep[catId] || []);
                        var multi = (catalog || []).some(function (c) { return c.id === catId && c.multi; });
                        keep[catId] = multi ? keep[catId].concat([id]) : [id];
                        selection = keep;
                        return loadCatalog(true);
                    })
                    .then(render)
                    .catch(function (e) { err.textContent = e.message || 'Error de conexión'; });
            }
            f.querySelector('.rename-ok').onclick = add;
            f.querySelector('.rename-cancel').onclick = function () { f.remove(); };
            inp.onkeydown = function (e) {
                if (e.key === 'Enter') add();
                if (e.key === 'Escape') f.remove();
            };
        }

        loadCatalog().then(render);
    }

    window.EBMeta = {
        loadCatalog: loadCatalog, catalog: function () { return catalog; },
        noteEditor: noteEditor, propsEditor: propsEditor,
        editing: function () { return open > 0; }
    };
})();
