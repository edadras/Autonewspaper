/*
 * Loaded into Premiere's ExtendScript engine when the panel opens.
 *
 * The panel's JavaScript calls AINS_PPRO_runJob with a path; everything else
 * - the runtime, the Premiere library, the job itself - is evaluated from
 * disk, so updating the desktop application updates what runs here without
 * reinstalling the extension.
 */
#targetengine "ainsPremiere"

function AINS_PPRO_runJob(jobPath) {
    try {
        var file = new File(jobPath);
        if (!file.exists) { return '{"ok":false,"error":{"message":"job file is missing"}}'; }
        $.evalFile(file);
        return "ok";
    } catch (e) {
        return '{"ok":false,"error":{"message":"' + String(e).replace(/"/g, "'") + '"}}';
    }
}

function AINS_PPRO_ping() {
    var version = "unknown";
    try { version = String(app.version); } catch (e) {}
    return version;
}
