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

    window.BB = { copyText: copyText, canShare: canShare, share: share, savedName: savedName, rememberName: rememberName, nameField: nameField };
})();
