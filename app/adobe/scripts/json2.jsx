/*
 * Minimal JSON implementation for ExtendScript.
 *
 * ExtendScript (ECMAScript 3) has no native JSON object. Every script the
 * application generates exchanges data with Python as JSON, so this shim is
 * always prepended. It implements the subset that matters: stringify with
 * proper string escaping, and a parse built on a guarded eval.
 */
if (typeof JSON !== "object") { JSON = {}; }

(function () {
    "use strict";

    function f(n) { return n < 10 ? "0" + n : n; }

    var escapable = /[\\\"\x00-\x1f\x7f-\x9f]/g;
    var meta = {
        "\b": "\\b", "\t": "\\t", "\n": "\\n", "\f": "\\f", "\r": "\\r",
        "\"": "\\\"", "\\": "\\\\"
    };

    function quote(string) {
        escapable.lastIndex = 0;
        return "\"" + String(string).replace(escapable, function (a) {
            var c = meta[a];
            if (typeof c === "string") { return c; }
            return "\\u" + ("0000" + a.charCodeAt(0).toString(16)).slice(-4);
        }) + "\"";
    }

    function str(key, holder) {
        var i, k, v, length, partial, value = holder[key];

        if (value && typeof value === "object" && typeof value.toJSON === "function") {
            value = value.toJSON(key);
        }

        switch (typeof value) {
        case "string":
            return quote(value);
        case "number":
            return isFinite(value) ? String(value) : "null";
        case "boolean":
            return String(value);
        case "object":
            if (!value) { return "null"; }
            partial = [];
            if (Object.prototype.toString.apply(value) === "[object Array]") {
                length = value.length;
                for (i = 0; i < length; i += 1) {
                    partial[i] = str(i, value) || "null";
                }
                return "[" + partial.join(",") + "]";
            }
            for (k in value) {
                if (Object.prototype.hasOwnProperty.call(value, k)) {
                    v = str(k, value);
                    if (v) { partial.push(quote(k) + ":" + v); }
                }
            }
            return "{" + partial.join(",") + "}";
        default:
            return undefined;
        }
    }

    if (typeof JSON.stringify !== "function") {
        JSON.stringify = function (value) {
            return str("", { "": value });
        };
    }

    if (typeof JSON.parse !== "function") {
        JSON.parse = function (text) {
            var cleaned = String(text);
            if (/^[\],:{}\s]*$/.test(
                cleaned.replace(/\\["\\\/bfnrtu]/g, "@")
                       .replace(/"[^"\\\n\r]*"|true|false|null|-?\d+(?:\.\d*)?(?:[eE][+\-]?\d+)?/g, "]")
                       .replace(/(?:^|:|,)(?:\s*\[)+/g, "")
            )) {
                return eval("(" + cleaned + ")");
            }
            throw new SyntaxError("JSON.parse: malformed input");
        };
    }
}());
