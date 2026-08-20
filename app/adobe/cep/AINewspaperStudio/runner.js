/*
 * The queue runner, on Premiere's side.
 *
 * Polls the job directory the desktop application points it at, hands each
 * job file to ExtendScript, and lets the job itself write the result. The
 * claim-by-rename is what stops two panels - or a reopened panel - running
 * the same job twice.
 */
(function () {
    "use strict";

    var csInterface = new CSInterface();
    var POLL_MS = 400;
    var state = document.getElementById("state");
    var logBox = document.getElementById("log");
    var queueDir = null;
    var busy = false;
    var lines = [];

    function say(message, kind) {
        state.textContent = message;
        state.className = kind || "idle";
    }

    function note(message) {
        lines.push(new Date().toLocaleTimeString() + "  " + message);
        while (lines.length > 40) { lines.shift(); }
        logBox.textContent = lines.join("\n");
        logBox.scrollTop = logBox.scrollHeight;
    }

    /* The desktop application writes queue_path.txt beside this file. */
    function resolveQueue() {
        var here = csInterface.getSystemPath(SystemPath.EXTENSION);
        var pointer = window.cep.fs.readFile(here + "/queue_path.txt");
        if (pointer.err !== 0 || !pointer.data) { return null; }
        return String(pointer.data).replace(/[\r\n]+$/, "");
    }

    function listJobs(directory) {
        var result = window.cep.fs.readdir(directory);
        if (result.err !== 0) { return []; }
        return result.data.filter(function (name) { return /\.job\.jsx$/.test(name); }).sort();
    }

    function claim(directory, name) {
        var from = directory + "/" + name;
        var to = directory + "/" + name.replace(/\.job\.jsx$/, ".running.jsx");
        /* CEP has no rename, so a copy-then-delete stands in for one. The
         * copy is what claims the job: a second panel finds it gone. */
        var read = window.cep.fs.readFile(from);
        if (read.err !== 0) { return null; }
        if (window.cep.fs.writeFile(to, read.data).err !== 0) { return null; }
        window.cep.fs.deleteFile(from);
        return to;
    }

    function fail(path, message) {
        var target = path.replace(/\.running\.jsx$/, ".result.json");
        var payload = JSON.stringify({
            ok: false, data: null,
            error: { message: String(message), type: "PremiereExtensionError" },
            log: [], duration: 0
        });
        window.cep.fs.writeFile(target, payload);
    }

    function runNext() {
        if (busy) { return; }
        if (queueDir === null) {
            queueDir = resolveQueue();
            if (queueDir === null) { say("No queue directory configured.", "error"); return; }
            note("Queue: " + queueDir);
        }
        if (window.cep.fs.readFile(queueDir + "/STOP").err === 0) {
            say("Stopped.", "idle");
            return;
        }
        var jobs = listJobs(queueDir);
        if (!jobs.length) { say("Waiting for the queue…", "idle"); return; }

        var claimed = claim(queueDir, jobs[0]);
        if (claimed === null) { return; }
        busy = true;
        say("Running " + jobs[0] + "…", "busy");
        note("run " + jobs[0]);
        csInterface.evalScript(
            'AINS_PPRO_runJob("' + claimed.replace(/\\/g, "\\\\").replace(/"/g, '\\"') + '")',
            function (answer) {
                busy = false;
                if (answer && answer !== "ok" && answer.indexOf('"ok":false') !== -1) {
                    note("failed: " + answer);
                    fail(claimed, answer);
                    say("Last job failed.", "error");
                } else {
                    say("Waiting for the queue…", "idle");
                }
            }
        );
    }

    csInterface.evalScript("AINS_PPRO_ping()", function (version) {
        note("Premiere " + version);
    });
    window.setInterval(runNext, POLL_MS);
}());
