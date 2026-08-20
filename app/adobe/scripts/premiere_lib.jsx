/*
 * AI Newspaper Studio - Premiere Pro scripting library.
 *
 * Premiere registers no automation object, so nothing here is reachable over
 * COM. It runs inside the CEP extension installed by the queue strategy,
 * which is the file-based automation path (priority 3) the rest of the
 * application already uses for InDesign and Photoshop.
 *
 * Two APIs are in play. The public one (app.project, app.encoder) is stable
 * and does most of the work. A few things - inserting a clip at a timecode,
 * applying a transition, reading a track item's effects - exist only on the
 * "QE" DOM, which Adobe does not document and does not promise. Every QE call
 * here is wrapped, reports what it could not do, and leaves the sequence in a
 * state the operator can finish by hand rather than half-built.
 *
 * Times are in seconds throughout; Premiere's own ticks (254016000000 per
 * second) never leave this file.
 */
AINS.PPRO = (function () {
    var api = {};
    var TICKS_PER_SECOND = 254016000000;

    api.reset = function () { return true; };

    /* --------------------------------------------------------- the session */

    api.setup = function () {
        var info = {
            version: String(app.version),
            build: String(app.build || ""),
            project: app.project ? String(app.project.name) : null,
            path: app.project && app.project.path ? String(app.project.path) : null,
            qe: false
        };
        try { app.enableQE(); info.qe = (typeof qe !== "undefined" && qe !== null); }
        catch (e) { AINS.log("The QE DOM is unavailable: " + e); }
        AINS.log("Premiere " + info.version + " ready");
        return info;
    };

    api.requireProject = function () {
        if (!app.project) { throw new Error("No Premiere project is open"); }
        return app.project;
    };

    api.ticks = function (seconds) { return String(Math.round(seconds * TICKS_PER_SECOND)); };

    api.seconds = function (time) {
        if (time === null || time === undefined) { return 0; }
        if (typeof time.seconds === "number") { return time.seconds; }
        if (time.ticks !== undefined) { return Number(time.ticks) / TICKS_PER_SECOND; }
        return Number(time) || 0;
    };

    api.time = function (seconds) {
        var t = new Time();
        t.seconds = seconds;
        return t;
    };

    /* ------------------------------------------------------------ project */

    api.openProject = function (path) {
        var file = new File(path);
        if (!file.exists) { throw new Error("Project not found: " + path); }
        app.openDocument(file.fsName);
        return api.projectInfo();
    };

    api.createProject = function (path) {
        var file = new File(path);
        var parent = file.parent;
        if (parent && !parent.exists) { parent.create(); }
        /* newProject overwrites without asking, so an existing file is the
         * operator's and is opened instead of being replaced. */
        if (file.exists) { return api.openProject(path); }
        app.newProject(file.fsName);
        return api.projectInfo();
    };

    api.saveProject = function (path) {
        var project = api.requireProject();
        if (path) { project.saveAs(new File(path).fsName); }
        else { project.save(); }
        return { path: project.path ? String(project.path) : null };
    };

    api.projectInfo = function () {
        var project = api.requireProject();
        var sequences = [];
        for (var i = 0; i < project.sequences.numSequences; i++) {
            var sequence = project.sequences[i];
            sequences.push({
                name: String(sequence.name),
                id: String(sequence.sequenceID),
                duration: api.seconds(sequence.end),
                video_tracks: sequence.videoTracks.numTracks,
                audio_tracks: sequence.audioTracks.numTracks
            });
        }
        return {
            name: String(project.name),
            path: project.path ? String(project.path) : null,
            items: project.rootItem ? project.rootItem.children.numItems : 0,
            sequences: sequences,
            active: project.activeSequence ? String(project.activeSequence.name) : null
        };
    };

    /* ------------------------------------------------------------ footage */

    /* Import files into the project, optionally into a bin.
     * Returns the project items that appeared, in order. */
    api.importFiles = function (paths, binName) {
        var project = api.requireProject();
        var bin = binName ? api.ensureBin(binName) : project.rootItem;
        var before = api.itemNames(bin);
        var wanted = [];
        for (var i = 0; i < paths.length; i++) {
            var file = new File(paths[i]);
            if (!file.exists) { throw new Error("Footage not found: " + paths[i]); }
            wanted.push(file.fsName);
        }
        project.importFiles(wanted, true, bin, false);
        var after = api.itemNames(bin);
        var added = [];
        for (var j = 0; j < after.length; j++) {
            if (api.indexOf(before, after[j]) === -1) { added.push(after[j]); }
        }
        AINS.log("Imported " + added.length + " item(s)");
        return { imported: added, bin: binName || null };
    };

    api.indexOf = function (list, value) {
        for (var i = 0; i < list.length; i++) { if (list[i] === value) { return i; } }
        return -1;
    };

    api.itemNames = function (bin) {
        var names = [];
        for (var i = 0; i < bin.children.numItems; i++) { names.push(String(bin.children[i].name)); }
        return names;
    };

    api.ensureBin = function (name) {
        var project = api.requireProject();
        var root = project.rootItem;
        for (var i = 0; i < root.children.numItems; i++) {
            var item = root.children[i];
            if (String(item.name) === String(name) && item.type === ProjectItemType.BIN) { return item; }
        }
        return root.createBin(name);
    };

    api.findItem = function (name, bin) {
        var project = api.requireProject();
        var found = null;
        function walk(item) {
            if (found) { return; }
            for (var i = 0; i < item.children.numItems; i++) {
                var child = item.children[i];
                if (String(child.name) === String(name)) { found = child; return; }
                if (child.type === ProjectItemType.BIN) { walk(child); }
            }
        }
        walk(bin || project.rootItem);
        return found;
    };

    api.requireItem = function (name) {
        var item = api.findItem(name);
        if (!item) { throw new Error("Project item '" + name + "' does not exist"); }
        return item;
    };

    /* --------------------------------------------------------- sequences */

    /* Create a sequence at an explicit size and frame rate.
     *
     * Premiere only creates a sequence from a preset file, so one is written
     * for the requested format rather than hoping a matching preset is
     * installed.
     */
    api.createSequence = function (spec) {
        var project = api.requireProject();
        var name = spec.name || "Sequence";
        var preset = api.writePreset(spec);
        try {
            project.newSequence(name, preset.fsName);
        } catch (e) {
            /* Older builds want createNewSequence, and some refuse a preset
             * path entirely; a sequence at the default size is still better
             * than no sequence, and the mismatch is reported. */
            AINS.log("newSequence failed (" + e + "); falling back to the default preset");
            project.createNewSequence(name, name);
        }
        var sequence = project.activeSequence;
        var info = api.sequenceInfo(sequence);
        info.requested = { width: spec.width, height: spec.height, fps: spec.fps };
        info.matches_request =
            Math.abs(info.width - spec.width) < 2 && Math.abs(info.height - spec.height) < 2;
        if (!info.matches_request) {
            AINS.log("Sequence is " + info.width + "x" + info.height + ", not " +
                     spec.width + "x" + spec.height);
        }
        return info;
    };

    /* A Premiere sequence preset is XML; writing one is the only way to get
     * an arbitrary frame size without an installed preset that matches. */
    api.writePreset = function (spec) {
        var width = spec.width || 1920;
        var height = spec.height || 1080;
        var fps = spec.fps || 25;
        var frameRateTicks = Math.round(TICKS_PER_SECOND / fps);
        var folder = Folder.temp;
        var file = new File(folder.fsName + "/ains_" + width + "x" + height + "_" + fps + ".sqpreset");
        var xml =
            '<?xml version="1.0" encoding="UTF-8"?>\n' +
            '<PremiereData Version="3">\n' +
            '  <StandardFilters Version="1">\n' +
            '    <RateConformStrategy>0</RateConformStrategy>\n' +
            '    <ScaleStrategy>2</ScaleStrategy>\n' +
            '  </StandardFilters>\n' +
            '  <SequencePreset Version="1" ObjectID="1" ClassID="9d43b1a4-6a26-4b3c-bd18-5a7d6c9f0000">\n' +
            '    <Name>' + (spec.name || "AINS") + '</Name>\n' +
            '    <ID>ains.' + width + "x" + height + "." + fps + '</ID>\n' +
            '    <TrackConfiguration>\n' +
            '      <VideoTracks>' + (spec.video_tracks || 3) + '</VideoTracks>\n' +
            '      <AudioTracks>' + (spec.audio_tracks || 2) + '</AudioTracks>\n' +
            '    </TrackConfiguration>\n' +
            '    <Editingmode>9678AC72-83D4-4C7D-9A2C-B6C0B1B94E3F</Editingmode>\n' +
            '    <VideoFrameWidth>' + width + '</VideoFrameWidth>\n' +
            '    <VideoFrameHeight>' + height + '</VideoFrameHeight>\n' +
            '    <VideoFrameRate>' + frameRateTicks + '</VideoFrameRate>\n' +
            '    <VideoPixelAspectRatio>1/1</VideoPixelAspectRatio>\n' +
            '    <AudioSampleRate>48000</AudioSampleRate>\n' +
            '  </SequencePreset>\n' +
            '</PremiereData>\n';
        AINS.writeText(file.fsName, xml);
        return file;
    };

    api.sequenceInfo = function (sequence) {
        sequence = sequence || api.requireProject().activeSequence;
        if (!sequence) { throw new Error("No sequence is active"); }
        var settings = sequence.getSettings ? sequence.getSettings() : null;
        return {
            name: String(sequence.name),
            id: String(sequence.sequenceID),
            duration: api.seconds(sequence.end),
            width: settings ? Number(settings.videoFrameWidth) : 0,
            height: settings ? Number(settings.videoFrameHeight) : 0,
            fps: settings && settings.videoFrameRate
                ? TICKS_PER_SECOND / Number(settings.videoFrameRate.ticks || settings.videoFrameRate)
                : 0,
            video_tracks: sequence.videoTracks.numTracks,
            audio_tracks: sequence.audioTracks.numTracks
        };
    };

    api.openSequence = function (name) {
        var project = api.requireProject();
        for (var i = 0; i < project.sequences.numSequences; i++) {
            if (String(project.sequences[i].name) === String(name)) {
                project.openSequence(project.sequences[i].sequenceID);
                project.activeSequence = project.sequences[i];
                return api.sequenceInfo(project.sequences[i]);
            }
        }
        throw new Error("Sequence '" + name + "' does not exist");
    };

    /* ------------------------------------------------------------- clips */

    api.videoTrack = function (index) {
        var sequence = api.requireProject().activeSequence;
        if (!sequence) { throw new Error("No sequence is active"); }
        if (index >= sequence.videoTracks.numTracks) {
            throw new Error("Video track " + (index + 1) + " does not exist in this sequence");
        }
        return sequence.videoTracks[index];
    };

    api.audioTrack = function (index) {
        var sequence = api.requireProject().activeSequence;
        if (index >= sequence.audioTracks.numTracks) {
            throw new Error("Audio track " + (index + 1) + " does not exist in this sequence");
        }
        return sequence.audioTracks[index];
    };

    /* Put a project item on the timeline.
     *
     * spec: {item, track, at, in_point, out_point, audio_track}
     * Times are seconds. "in_point"/"out_point" trim the source before it is
     * placed, which is what an edit actually is.
     */
    api.appendClip = function (spec) {
        var item = api.requireItem(spec.item);
        if (spec.in_point !== undefined && spec.in_point !== null) {
            item.setInPoint(api.ticks(spec.in_point), 4);
        }
        if (spec.out_point !== undefined && spec.out_point !== null) {
            item.setOutPoint(api.ticks(spec.out_point), 4);
        }
        var track = api.videoTrack(spec.track === undefined ? 0 : spec.track);
        var at = spec.at === undefined || spec.at === null ? api.trackEnd(track) : spec.at;
        var before = track.clips.numItems;
        track.insertClip(item, api.time(at));
        if (track.clips.numItems <= before) {
            throw new Error("Premiere did not place '" + spec.item + "' on the timeline");
        }
        var placed = track.clips[track.clips.numItems - 1];
        return {
            name: String(placed.name),
            track: spec.track === undefined ? 0 : spec.track,
            start: api.seconds(placed.start),
            end: api.seconds(placed.end),
            duration: api.seconds(placed.duration)
        };
    };

    api.trackEnd = function (track) {
        var end = 0;
        for (var i = 0; i < track.clips.numItems; i++) {
            var clip = track.clips[i];
            var clipEnd = api.seconds(clip.end);
            if (clipEnd > end) { end = clipEnd; }
        }
        return end;
    };

    api.findClip = function (trackIndex, name) {
        var track = api.videoTrack(trackIndex);
        for (var i = 0; i < track.clips.numItems; i++) {
            if (String(track.clips[i].name) === String(name)) { return track.clips[i]; }
        }
        return null;
    };

    api.moveClip = function (trackIndex, name, seconds) {
        var clip = api.findClip(trackIndex, name);
        if (!clip) { throw new Error("Clip '" + name + "' is not on track " + (trackIndex + 1)); }
        clip.move(api.time(seconds - api.seconds(clip.start)));
        return { name: name, start: api.seconds(clip.start) };
    };

    api.setClipSpeed = function (trackIndex, name, factor) {
        var clip = api.findClip(trackIndex, name);
        if (!clip) { throw new Error("Clip '" + name + "' is not on track " + (trackIndex + 1)); }
        /* Speed lives only on the QE DOM. */
        try {
            var qeSequence = qe.project.getActiveSequence();
            var qeTrack = qeSequence.getVideoTrackAt(trackIndex);
            for (var i = 0; i < qeTrack.numItems; i++) {
                var qeClip = qeTrack.getItemAt(i);
                if (String(qeClip.name) === String(name)) {
                    qeClip.setSpeed(factor, null, false, false, false);
                    return { name: name, speed: factor };
                }
            }
            throw new Error("not found on the QE track");
        } catch (e) {
            AINS.log("Speed could not be set on '" + name + "': " + e);
            return { name: name, speed: null, error: String(e) };
        }
    };

    /* --------------------------------------------------------- transitions */

    /* Put a transition between two clips. Only the QE DOM can do this, so a
     * failure is reported and the cut is left clean rather than broken. */
    api.addTransition = function (spec) {
        var trackIndex = spec.track === undefined ? 0 : spec.track;
        var name = spec.transition || "Cross Dissolve";
        try {
            var qeSequence = qe.project.getActiveSequence();
            var qeTrack = qeSequence.getVideoTrackAt(trackIndex);
            var effect = qe.project.getVideoTransitionByName(name);
            if (!effect) { throw new Error("Premiere has no transition called '" + name + "'"); }
            var index = spec.after_clip === undefined ? 0 : spec.after_clip;
            if (index >= qeTrack.numItems) { throw new Error("Clip " + (index + 1) + " is not on the track"); }
            var clip = qeTrack.getItemAt(index);
            var duration = api.timecode(spec.duration === undefined ? 1.0 : spec.duration);
            clip.addVideoTransition(effect, false, duration);
            return { transition: name, track: trackIndex, after_clip: index, duration: spec.duration };
        } catch (e) {
            AINS.log("Transition '" + name + "' was not added: " + e);
            return { transition: name, applied: false, error: String(e) };
        }
    };

    /* The QE DOM wants a timecode string, not seconds. */
    api.timecode = function (seconds) {
        var fps = 25;
        try { fps = api.sequenceInfo().fps || 25; } catch (e) {}
        var whole = Math.floor(seconds);
        var frames = Math.round((seconds - whole) * fps);
        function pad(value) { return (value < 10 ? "0" : "") + value; }
        return pad(Math.floor(whole / 3600)) + ":" + pad(Math.floor((whole % 3600) / 60)) + ":" +
               pad(whole % 60) + ":" + pad(frames);
    };

    /* ------------------------------------------------------------ effects */

    api.availableEffects = function (filter) {
        var names = [];
        try {
            var effects = qe.project.getVideoEffectList();
            for (var i = 0; i < effects.length; i++) {
                var name = String(effects[i].name);
                if (!filter || name.toLowerCase().indexOf(String(filter).toLowerCase()) !== -1) {
                    names.push(name);
                }
            }
        } catch (e) { AINS.log("The effect list is unavailable: " + e); }
        return names;
    };

    /* Apply an effect to a clip and set its parameters.
     *
     * spec: {track, clip, effect, parameters: {name: value}}
     */
    api.applyEffect = function (spec) {
        var trackIndex = spec.track === undefined ? 0 : spec.track;
        try {
            var qeSequence = qe.project.getActiveSequence();
            var qeTrack = qeSequence.getVideoTrackAt(trackIndex);
            var index = spec.clip === undefined ? 0 : spec.clip;
            if (index >= qeTrack.numItems) { throw new Error("Clip " + (index + 1) + " is not on the track"); }
            var qeClip = qeTrack.getItemAt(index);
            var effect = qe.project.getVideoEffectByName(spec.effect);
            if (!effect) { throw new Error("Premiere has no effect called '" + spec.effect + "'"); }
            qeClip.addVideoEffect(effect);
        } catch (e) {
            AINS.log("Effect '" + spec.effect + "' was not applied: " + e);
            return { effect: spec.effect, applied: false, error: String(e) };
        }
        var set = api.setEffectParameters(trackIndex, spec.clip || 0, spec.effect, spec.parameters || {});
        return { effect: spec.effect, applied: true, parameters: set };
    };

    /* Parameters go through the public DOM, which is both stable and the only
     * place keyframes can be set. */
    api.setEffectParameters = function (trackIndex, clipIndex, effectName, parameters) {
        var applied = [];
        try {
            var track = api.videoTrack(trackIndex);
            var clip = track.clips[clipIndex];
            if (!clip) { return applied; }
            for (var i = 0; i < clip.components.numItems; i++) {
                var component = clip.components[i];
                if (String(component.displayName) !== String(effectName)) { continue; }
                for (var j = 0; j < component.properties.numItems; j++) {
                    var property = component.properties[j];
                    var wanted = parameters[String(property.displayName)];
                    if (wanted === undefined) { continue; }
                    try {
                        property.setValue(wanted, true);
                        applied.push(String(property.displayName));
                    } catch (e) {
                        AINS.log("Parameter '" + property.displayName + "' was refused: " + e);
                    }
                }
            }
        } catch (e) { AINS.log("Parameters could not be set: " + e); }
        return applied;
    };

    /* Motion is a component every clip has, so scale, position, rotation and
     * opacity never need the QE DOM. */
    api.transformClip = function (spec) {
        var track = api.videoTrack(spec.track === undefined ? 0 : spec.track);
        var clip = track.clips[spec.clip === undefined ? 0 : spec.clip];
        if (!clip) { throw new Error("Clip " + ((spec.clip || 0) + 1) + " is not on the track"); }
        var wanted = {
            "Position": spec.position,
            "Scale": spec.scale,
            "Rotation": spec.rotation,
            "Opacity": spec.opacity,
            "Anchor Point": spec.anchor
        };
        var applied = [];
        for (var i = 0; i < clip.components.numItems; i++) {
            var component = clip.components[i];
            for (var j = 0; j < component.properties.numItems; j++) {
                var property = component.properties[j];
                var value = wanted[String(property.displayName)];
                if (value === undefined || value === null) { continue; }
                try {
                    if (spec.at !== undefined && spec.at !== null) {
                        property.setTimeVarying(true);
                        property.addKey(api.time(spec.at));
                        property.setValueAtKey(api.time(spec.at), value, true);
                    } else {
                        property.setValue(value, true);
                    }
                    applied.push(String(property.displayName));
                } catch (e) {
                    AINS.log("'" + property.displayName + "' was refused: " + e);
                }
            }
        }
        return { applied: applied };
    };

    /* -------------------------------------------------------------- titles */

    /* Put a still - typically a Photoshop design with transparency - over the
     * footage. This is how professional lower thirds and end cards are made:
     * the type is set where type belongs, and Premiere composites it. */
    api.addOverlay = function (spec) {
        var project = api.requireProject();
        var file = new File(spec.path);
        if (!file.exists) { throw new Error("Overlay not found: " + spec.path); }
        var existing = api.findItem(file.name);
        if (!existing) {
            project.importFiles([file.fsName], true, api.ensureBin(spec.bin || "Graphics"), false);
            existing = api.findItem(file.name);
        }
        if (!existing) { throw new Error("Premiere did not import " + file.name); }
        var placed = api.appendClip({
            item: String(existing.name),
            track: spec.track === undefined ? 1 : spec.track,
            at: spec.at,
            out_point: spec.duration
        });
        if (spec.duration) {
            var track = api.videoTrack(spec.track === undefined ? 1 : spec.track);
            var clip = track.clips[track.clips.numItems - 1];
            try { clip.end = api.time(api.seconds(clip.start) + spec.duration); }
            catch (e) { AINS.log("The overlay kept its own length: " + e); }
        }
        if (spec.fade) { api.fadeClip(spec.track === undefined ? 1 : spec.track,
                                      track_last_index(spec), spec.fade); }
        return placed;
    };

    function track_last_index(spec) {
        var track = api.videoTrack(spec.track === undefined ? 1 : spec.track);
        return Math.max(0, track.clips.numItems - 1);
    }

    /* Fade a clip in and out by keyframing its opacity. */
    api.fadeClip = function (trackIndex, clipIndex, seconds) {
        var track = api.videoTrack(trackIndex);
        var clip = track.clips[clipIndex];
        if (!clip) { return { applied: false }; }
        var start = api.seconds(clip.start);
        var end = api.seconds(clip.end);
        var length = Math.min(seconds, Math.max(0.05, (end - start) / 2));
        for (var i = 0; i < clip.components.numItems; i++) {
            var component = clip.components[i];
            for (var j = 0; j < component.properties.numItems; j++) {
                var property = component.properties[j];
                if (String(property.displayName) !== "Opacity") { continue; }
                try {
                    property.setTimeVarying(true);
                    property.addKey(api.time(start));
                    property.setValueAtKey(api.time(start), 0, true);
                    property.addKey(api.time(start + length));
                    property.setValueAtKey(api.time(start + length), 100, true);
                    property.addKey(api.time(end - length));
                    property.setValueAtKey(api.time(end - length), 100, true);
                    property.addKey(api.time(end));
                    property.setValueAtKey(api.time(end), 0, true);
                    return { applied: true, seconds: length };
                } catch (e) {
                    AINS.log("The fade was refused: " + e);
                    return { applied: false, error: String(e) };
                }
            }
        }
        return { applied: false };
    };

    /* --------------------------------------------------------------- audio */

    api.addAudio = function (spec) {
        var item = api.requireItem(spec.item);
        var track = api.audioTrack(spec.track === undefined ? 0 : spec.track);
        track.insertClip(item, api.time(spec.at || 0));
        return { item: spec.item, track: spec.track === undefined ? 0 : spec.track, at: spec.at || 0 };
    };

    /* -------------------------------------------------------------- export */

    /* Hand the sequence to Media Encoder. Rendering happens there, which is
     * what keeps Premiere responsive and is how a professional edit is
     * delivered. */
    api.exportSequence = function (spec) {
        var project = api.requireProject();
        var sequence = project.activeSequence;
        if (!sequence) { throw new Error("No sequence is active"); }
        var target = new File(spec.path);
        var parent = target.parent;
        if (parent && !parent.exists) { parent.create(); }
        var preset = spec.preset ? new File(spec.preset) : null;
        if (preset && !preset.exists) { throw new Error("Export preset not found: " + spec.preset); }

        if (spec.queue === true && app.encoder) {
            app.encoder.launchEncoder();
            var job = app.encoder.encodeSequence(
                sequence, target.fsName, preset ? preset.fsName : "",
                spec.work_area === true ? 1 : 0, spec.start_queue === false ? 0 : 1
            );
            return { queued: true, job: String(job), path: target.fsName };
        }
        var ok = sequence.exportAsMediaDirect(
            target.fsName, preset ? preset.fsName : "", spec.work_area === true ? 1 : 0
        );
        return { queued: false, ok: ok !== false, path: target.fsName };
    };

    api.exportFrame = function (spec) {
        var sequence = api.requireProject().activeSequence;
        if (!sequence) { throw new Error("No sequence is active"); }
        var target = new File(spec.path);
        var parent = target.parent;
        if (parent && !parent.exists) { parent.create(); }
        sequence.exportFramePNG(api.time(spec.at || 0), target.fsName);
        return { path: target.fsName, at: spec.at || 0 };
    };

    /* ----------------------------------------------------- building an edit */

    /* Build a whole edit from a plan: create the sequence, import the
     * footage, lay the clips down, apply the transitions, effects and
     * overlays, and report what actually happened for each step. */
    api.buildEdit = function (plan) {
        var report = { sequence: null, imported: [], clips: [], steps: [], failed: [] };
        report.sequence = api.createSequence(plan.sequence || {});
        if (plan.footage && plan.footage.length) {
            report.imported = api.importFiles(plan.footage, plan.bin || "Footage").imported;
        }
        var steps = plan.steps || [];
        for (var i = 0; i < steps.length; i++) {
            var step = steps[i];
            try {
                var result;
                if (step.action === "clip") { result = api.appendClip(step); report.clips.push(result); }
                else if (step.action === "transition") { result = api.addTransition(step); }
                else if (step.action === "effect") { result = api.applyEffect(step); }
                else if (step.action === "transform") { result = api.transformClip(step); }
                else if (step.action === "overlay") { result = api.addOverlay(step); }
                else if (step.action === "audio") { result = api.addAudio(step); }
                else if (step.action === "speed") { result = api.setClipSpeed(step.track || 0, step.clip_name, step.factor); }
                else { throw new Error("Unknown step '" + step.action + "'"); }
                report.steps.push({ index: i, action: step.action, result: result });
            } catch (e) {
                /* One step failing must not throw away the rest of the edit. */
                AINS.log("Step " + i + " (" + step.action + ") failed: " + e);
                report.failed.push({ index: i, action: step.action, error: String(e) });
            }
        }
        report.timeline = api.timelineReport();
        return report;
    };

    /* What the timeline actually contains, for the quality check. */
    api.timelineReport = function () {
        var sequence = api.requireProject().activeSequence;
        if (!sequence) { return { tracks: [] }; }
        var tracks = [];
        for (var t = 0; t < sequence.videoTracks.numTracks; t++) {
            var track = sequence.videoTracks[t];
            var clips = [];
            for (var c = 0; c < track.clips.numItems; c++) {
                var clip = track.clips[c];
                clips.push({
                    name: String(clip.name),
                    start: api.seconds(clip.start),
                    end: api.seconds(clip.end),
                    duration: api.seconds(clip.duration),
                    components: clip.components ? clip.components.numItems : 0
                });
            }
            tracks.push({ index: t, kind: "video", clips: clips });
        }
        for (var a = 0; a < sequence.audioTracks.numTracks; a++) {
            var audio = sequence.audioTracks[a];
            var audioClips = [];
            for (var k = 0; k < audio.clips.numItems; k++) {
                audioClips.push({
                    name: String(audio.clips[k].name),
                    start: api.seconds(audio.clips[k].start),
                    end: api.seconds(audio.clips[k].end)
                });
            }
            tracks.push({ index: a, kind: "audio", clips: audioClips });
        }
        return { duration: api.seconds(sequence.end), tracks: tracks };
    };

    return api;
}());
