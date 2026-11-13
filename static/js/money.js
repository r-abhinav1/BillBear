/*
 * Amounts in the browser, read and written the same way the server does
 * (utils/money.py). A currency is {code, name, symbol, decimals, step}.
 *
 *   BBMoney.parse('₹1,234.50')  -> 1234.5     parse('')  -> null
 *   BBMoney.parse('12,50')      -> 12.5       parse('x') -> NaN
 *   BBMoney.format(1200, JPY)   -> '¥1,200'
 */
(function () {
    var MISSING = /^(|n\/?a|-|none|null)$/i;
    var GROUPING = /[\s  '’]/g;
    var COMMA_DECIMAL = /^(?:\d{1,3}(?:\.\d{3})*,\d{1,2}|\d+,\d{1,2})$/;
    var DOT_DECIMAL = /^\d+(?:,\d{2,3})*(?:\.\d+)?$/;

    function parse(value) {
        if (value == null) return null;
        var text = String(value).trim();
        if (MISSING.test(text)) return null;
        var first = text.search(/\d/);
        if (first < 0) return NaN;
        var last = text.length - 1;
        while (last >= 0 && !/\d/.test(text[last])) last--;
        var head = text.slice(0, first);
        var body = text.slice(first, last + 1).replace(GROUPING, '');
        var number;
        if (COMMA_DECIMAL.test(body)) number = body.replace(/\./g, '').replace(',', '.');
        else if (DOT_DECIMAL.test(body)) number = body.replace(/,/g, '');
        else return NaN;
        var n = parseFloat(number);
        return /[-−]/.test(head) ? -n : n;
    }

    function round(n, currency) {
        var f = Math.pow(10, currency.decimals);
        return Math.round((n + (n >= 0 ? 1e-9 : -1e-9)) * f) / f;
    }

    // True when n has no more decimals than the currency allows (¥12.5 does not).
    function fits(n, currency) {
        var f = Math.pow(10, currency.decimals);
        return Math.abs(Math.round(n * f) - n * f) < 1e-6;
    }

    function amount(n, currency) {
        var locale = currency.code === 'INR' ? 'en-IN' : 'en-US';
        return Math.abs(round(n, currency)).toLocaleString(locale, {
            minimumFractionDigits: currency.decimals,
            maximumFractionDigits: currency.decimals,
        });
    }

    function format(n, currency) {
        if (n == null || Number.isNaN(n)) return 'N/A';
        var symbol = currency.symbol;
        var sep = /[A-Za-z]$/.test(symbol) ? ' ' : '';
        return (n < 0 ? '-' : '') + symbol + sep + amount(n, currency);
    }

    // In whole minor units (paise, yen), so float noise never matters.
    function units(n, currency) {
        return Math.round(n * Math.pow(10, currency.decimals));
    }

    window.BBMoney = { parse: parse, format: format, amount: amount, round: round, fits: fits, units: units };
})();
