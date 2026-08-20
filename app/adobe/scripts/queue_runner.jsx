/*
 * AI Newspaper Studio - resident queue runner.
 *
 * Installed into the host application's Scripts Panel folder and started once.
 * It watches a queue directory for "*.job.jsx" files, runs each one and writes
 * "<job>.result.json" next to it. This is the file-based automation path
 * (priority 3): it works when COM is unavailable or blocked, without any UI
 * or input automation.
 *
 * The queue directory is passed by writing it into "queue_path.txt" beside
 * this script before it is launched.
 */
#targetengine "ainsQueue"

var AINS_QUEUE = (function () {
    var api = {};
    var here = new File($.fileName).parent;
    var queueFolder = null;
    var stopped = false;

    api.resolveQueue = function () {
        var pointer = new File(here.fsName + "/queue_path.txt");
        if (!pointer.exists) { return null; }
        pointer.open("r");
        var path = pointer.read();
        pointer.close();
        var folder = new Folder(String(path).replace(/[\r\n]+$/, ""));
        if (!folder.exists) { folder.create(); }
        return folder;
    };

    api.runOnce = function () {
        if (queueFolder === null) { queueFolder = api.resolveQueue(); }
        if (queueFolder === null) { return 0; }
        if (new File(queueFolder.fsName + "/STOP").exists) { stopped = true; return 0; }

        var jobs = queueFolder.getFiles("*.job.jsx");
        jobs.sort();
        var handled = 0;
        for (var i = 0; i < jobs.length; i++) {
            var job = jobs[i];
            var claimed = new File(job.fsName.replace(/\.job\.jsx$/, ".running.jsx"));
            if (!job.rename(claimed.name)) { continue; }
            try {
                $.evalFile(claimed);
            } catch (e) {
                var failure = new File(claimed.fsName.replace(/\.running\.jsx$/, ".result.json"));
                failure.encoding = "UTF-8";
                failure.open("w");
                failure.write('{"ok":false,"data":null,"error":{"message":' +
                              '"' + String(e).replace(/"/g, "'") + '"},"log":[]}');
                failure.close();
            }
            try { claimed.remove(); } catch (e2) {}
            handled++;
        }
        return handled;
    };

    api.start = function (intervalMs) {
        stopped = false;
        var tick = function () {
            if (stopped) { return; }
            try { AINS_QUEUE.runOnce(); } catch (e) {}
            if (!stopped) { app.doScript("AINS_QUEUE.tick()", ScriptLanguage.JAVASCRIPT); }
        };
        api.tick = tick;
        /* $.setTimeout does not exist in ExtendScript; use an idle task. */
        try {
            var task = app.idleTasks.add({ name: "AINSQueue", sleep: intervalMs || 400 });
            task.addEventListener(IdleEvent.ON_IDLE, function () {
                if (stopped) { return; }
                try { AINS_QUEUE.runOnce(); } catch (e) {}
            });
            return "idle-task";
        } catch (e) {
            /* Photoshop has no idle tasks: fall back to a bounded poll loop. */
            var deadline = new Date().getTime() + (1000 * 60 * 60 * 6);
            while (!stopped && new Date().getTime() < deadline) {
                api.runOnce();
                $.sleep(intervalMs || 400);
            }
            return "poll-loop";
        }
    };

    api.stop = function () { stopped = true; };

    return api;
}());

AINS_QUEUE.start(400);
