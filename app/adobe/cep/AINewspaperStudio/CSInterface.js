/*
 * Adobe ships CSInterface.js with the CEP runtime and Premiere injects it for
 * panels that ask. This file is a deliberate minimum: it defines only what
 * runner.js uses, and defers to the real implementation when the host has
 * already provided one - so the panel works on every CEP version without this
 * repository carrying a vendored copy that would go stale.
 */
if (typeof CSInterface === "undefined") {
    var SystemPath = { EXTENSION: "extension" };

    var CSInterface = function () {};

    CSInterface.prototype.getSystemPath = function (kind) {
        if (kind === SystemPath.EXTENSION && typeof __adobe_cep__ !== "undefined") {
            return decodeURI(__adobe_cep__.getSystemPath("extension")).replace(/^file:\/{2,3}/, "");
        }
        return "";
    };

    CSInterface.prototype.evalScript = function (script, callback) {
        if (typeof __adobe_cep__ === "undefined") {
            if (callback) { callback("CEP is not available"); }
            return;
        }
        __adobe_cep__.evalScript(script, callback || function () {});
    };

    CSInterface.prototype.getHostEnvironment = function () {
        if (typeof __adobe_cep__ === "undefined") { return {}; }
        return JSON.parse(__adobe_cep__.getHostEnvironment());
    };
}
