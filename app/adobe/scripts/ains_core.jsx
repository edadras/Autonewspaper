/*
 * AI Newspaper Studio - shared ExtendScript runtime.
 *
 * Provides the result protocol used by every generated script: a log, a
 * structured result object, safe file writing (always UTF-8) and small helpers
 * for units and paths. Both the InDesign and the Photoshop libraries build on
 * this file, and it is prepended automatically by app.adobe.jsx.
 */
var AINS = (function () {
    var api = {};

    api.VERSION = "1.0";
    api.startedAt = new Date().getTime();
    api.logLines = [];
    api.resultPath = null;

    api.log = function (message) {
        var stamp = ((new Date().getTime() - api.startedAt) / 1000).toFixed(3);
        api.logLines.push("[" + stamp + "s] " + String(message));
        return message;
    };

    api.setResultPath = function (path) {
        api.resultPath = path;
    };

    /* Write UTF-8 text, creating parent folders as needed. */
    api.writeText = function (path, content) {
        var file = new File(path);
        var parent = file.parent;
        if (parent && !parent.exists) { parent.create(); }
        file.encoding = "UTF-8";
        file.lineFeed = "Unix";
        if (!file.open("w")) { return false; }
        file.write(content);
        file.close();
        return true;
    };

    api.readText = function (path) {
        var file = new File(path);
        if (!file.exists) { return null; }
        file.encoding = "UTF-8";
        if (!file.open("r")) { return null; }
        var content = file.read();
        file.close();
        return content;
    };

    /* Emit the final result: written to disk and returned to the caller. */
    api.emit = function (ok, data, error) {
        var payload = {
            ok: ok,
            data: data === undefined ? null : data,
            error: error === undefined ? null : error,
            log: api.logLines,
            duration: (new Date().getTime() - api.startedAt) / 1000,
            runtime: api.VERSION
        };
        var text = JSON.stringify(payload);
        if (api.resultPath) {
            try { api.writeText(api.resultPath, text); } catch (e) { /* reported via return */ }
        }
        return text;
    };

    api.fail = function (error, context) {
        var message = (error && error.message) ? error.message : String(error);
        var detail = {
            message: message,
            line: (error && error.line) ? error.line : null,
            fileName: (error && error.fileName) ? String(error.fileName) : null,
            context: context || null
        };
        return api.emit(false, null, detail);
    };

    /* --- units ---------------------------------------------------------- */
    api.MM_PER_INCH = 25.4;
    api.mmToPt = function (mm) { return mm * 72.0 / api.MM_PER_INCH; };
    api.ptToMm = function (pt) { return pt * api.MM_PER_INCH / 72.0; };

    /* --- paths ---------------------------------------------------------- */
    api.toFile = function (path) { return new File(path); };
    api.toFolder = function (path) {
        var folder = new Folder(path);
        if (!folder.exists) { folder.create(); }
        return folder;
    };
    api.exists = function (path) { return new File(path).exists; };

    api.basename = function (path) {
        var parts = String(path).replace(/\\/g, "/").split("/");
        return parts[parts.length - 1];
    };

    /* --- misc ----------------------------------------------------------- */
    api.contains = function (list, value) {
        for (var i = 0; i < list.length; i++) { if (list[i] === value) { return true; } }
        return false;
    };

    api.clamp = function (value, low, high) {
        return Math.max(low, Math.min(high, value));
    };

    return api;
}());
