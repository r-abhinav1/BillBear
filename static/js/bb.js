/* Small helpers the room pages share: copy to the clipboard, the phone's share
   sheet, and remembering the person's name on this device. */
(function () {
    // Puts `text` on the clipboard and shows `doneLabel` on the button for a moment.
    function copyText(text, btn, doneLabel) {
        function done(ok) {
            if (!btn) return;
            var original = btn.dataset.label || btn.textContent;
            btn.dataset.label = original;
            btn.textContent = ok ? (doneLabel || 'Copied') : 'Could not copy';
            setTimeout(function () { btn.textContent = original; }, 2000);
        }
        function fallback() {
            var ta = document.createElement('textarea');
            ta.value = text;
            ta.style.cssText = 'position:fixed;top:0;left:0;opacity:0';
            document.body.appendChild(ta);
            ta.select();
            var ok = false;
            try { ok = document.execCommand('copy'); } catch (_e) {}
            document.body.removeChild(ta);
            done(ok);
        }
        if (navigator.clipboard && window.isSecureContext) {
            navigator.clipboard.writeText(text).then(function () { done(true); }).catch(fallback);
        } else {
            fallback();
        }
    }

    // Phones open their share sheet (WhatsApp and the rest). Desktops mostly can't.
    function canShare() {
        return typeof navigator.share === 'function';
    }

    // Resolves 'shared', 'cancelled' (the sheet was closed) or 'failed'.
    function share(data) {
        if (!canShare()) return Promise.resolve('failed');
        return navigator.share(data).then(
            function () { return 'shared'; },
            function (e) { return e && e.name === 'AbortError' ? 'cancelled' : 'failed'; }
        );
    }

    // The name typed last on this device, so friends who split often don't retype it.
    var NAME_KEY = 'bb_name';
    function savedName() {
        try { return localStorage.getItem(NAME_KEY) || ''; } catch (_e) { return ''; }
    }
    function rememberName(name) {
        name = (name || '').trim();
        if (!name) return;
        try { localStorage.setItem(NAME_KEY, name); } catch (_e) {}
    }
    // Fills an empty name field and saves whatever ends up in it. A name the page
    // already filled in (after an error, say) is left alone.
    function nameField(input) {
        if (!input) return;
        if (!input.value) input.value = savedName();
        input.addEventListener('change', function () { rememberName(input.value); });
        if (input.form) input.form.addEventListener('submit', function () { rememberName(input.value); }, true);
    }

    /* Bear's dialogs, in place of the browser's confirm() and alert().
       BB.confirm({title, text, ok, cancel, danger}) resolves true or false;
       BB.alert({title, text, ok}) (or BB.alert('text')) resolves once closed.
       Escape, the backdrop and Cancel all mean "no". Pages without the
       template (none today) fall back to the browser's own boxes. */
    var openDialog = null;

    function dialog(opts, isAlert) {
        var tpl = document.getElementById('bbDialogTpl');
        if (!tpl) {
            var plain = [opts.title, opts.text].filter(Boolean).join('\n\n');
            if (isAlert) { window.alert(plain); return Promise.resolve(); }
            return Promise.resolve(window.confirm(plain));
        }
        if (openDialog) openDialog(false);       // one at a time: a new question replaces the old one

        return new Promise(function (resolve) {
            var node = tpl.content.firstElementChild.cloneNode(true);
            var ok = node.querySelector('.bbd__ok');
            var cancel = node.querySelector('.bbd__cancel');
            var page = document.querySelector('.page');
            var before = document.activeElement;
            node.querySelector('.bbd__title').textContent = opts.title || '';
            node.querySelector('.bbd__text').textContent = opts.text || '';
            ok.textContent = opts.ok || (isAlert ? 'OK' : 'Yes');
            cancel.textContent = opts.cancel || 'Cancel';
            if (isAlert) node.classList.add('bbd--alert');
            if (opts.danger) node.classList.add('bbd--danger');

            var done = false;
            function close(answer) {
                if (done) return;
                done = true;
                openDialog = null;
                document.removeEventListener('keydown', onKey, true);
                if (page) page.inert = false;
                var reduce = window.matchMedia && matchMedia('(prefers-reduced-motion: reduce)').matches;
                node.classList.add('is-leaving');
                setTimeout(function () { node.remove(); }, reduce ? 0 : 140);
                if (before && typeof before.focus === 'function' && document.contains(before)) before.focus();
                resolve(isAlert ? undefined : answer);
            }
            function onKey(e) {
                if (e.key === 'Escape') { e.preventDefault(); close(false); return; }
                if (e.key === 'Tab') {
                    // Keep the keyboard inside the dialog.
                    var stops = isAlert ? [ok] : [ok, cancel];
                    var at = stops.indexOf(document.activeElement);
                    e.preventDefault();
                    stops[(at + (e.shiftKey ? stops.length - 1 : 1) + stops.length) % stops.length].focus();
                }
            }
            ok.addEventListener('click', function () { close(true); });
            cancel.addEventListener('click', function () { close(false); });
            node.querySelector('.bbd__scrim').addEventListener('click', function () { close(false); });
            document.addEventListener('keydown', onKey, true);
            openDialog = close;

            document.body.appendChild(node);
            if (page) page.inert = true;          // the page behind can't be tapped or tabbed to
            // Something that undoes work starts on the safe answer.
            (opts.danger && !isAlert ? cancel : ok).focus();
        });
    }

    function confirmDialog(opts) { return dialog(opts || {}, false); }
    function alertDialog(opts) { return dialog(typeof opts === 'string' ? { text: opts } : (opts || {}), true); }

    window.BB = { copyText: copyText, canShare: canShare, share: share, savedName: savedName, rememberName: rememberName, nameField: nameField,
                  confirm: confirmDialog, alert: alertDialog };
})();
