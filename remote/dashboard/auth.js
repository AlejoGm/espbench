/*
 * auth.js — token de la API en el dashboard (window.EBAuth).
 *
 * Si la Pi tiene /opt/esp/api_token, las escrituras piden
 * `Authorization: Bearer <token>`. EBAuth.fetch agrega el token guardado en
 * localStorage; ante un 401 muestra una barra con un input para pedirlo (una
 * vez: queda guardado), reintenta el pedido y devuelve esa respuesta. Las
 * lecturas no lo necesitan.
 */
(function () {
    'use strict';
    // A través de bench-master todos los benches comparten origen (localhost:8090):
    // un token por bench (por su /bench/<nombre>/). Directo, la clave de siempre.
    var BASE = EB.basePath(location.pathname);
    var KEY = 'eb.apiToken' + (BASE === '/' ? '' : ':' + BASE);
    var pending = null;

    function get() {
        try { return localStorage.getItem(KEY) || ''; } catch (e) { return ''; }
    }

    function set(token) {
        try { localStorage.setItem(KEY, token); } catch (e) {}
    }

    // Barra inline (no prompt()): resuelve con el token, o '' si se cancela.
    function ask() {
        if (pending) return pending;
        pending = new Promise(function (resolve) {
            var bar = document.createElement('form');
            bar.className = 'token-bar';
            bar.innerHTML = '<span>Esta Pi pide el token de la API para escribir.</span>' +
                '<input type="password" placeholder="token" autocomplete="off">' +
                '<button type="submit" class="action-btn">Guardar</button>' +
                '<button type="button" class="tool-btn">Cancelar</button>';
            document.body.appendChild(bar);
            var input = bar.querySelector('input');
            input.focus();
            function done(token) {
                bar.remove();
                pending = null;
                resolve(token);
            }
            bar.onsubmit = function (e) {
                e.preventDefault();
                var token = input.value.trim();
                if (!token) return;
                set(token);
                done(token);
            };
            bar.querySelector('button[type=button]').onclick = function () { done(''); };
        });
        return pending;
    }

    function authFetch(url, opts) {
        return fetch(url, EB.withToken(opts, get())).then(function (r) {
            if (r.status !== 401) return r;
            return ask().then(function (token) {
                return token ? fetch(url, EB.withToken(opts, token)) : r;
            });
        });
    }

    window.EBAuth = {fetch: authFetch, token: get};
})();
