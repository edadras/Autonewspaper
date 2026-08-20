/*
 * AI Newspaper Studio - Photoshop scripting library.
 *
 * Covers the operations the pipeline needs: open/create, resize, crop to a
 * target aspect, tonal correction, background removal, smart objects, colour
 * mode conversion and export. Features that only exist in newer builds
 * (Select Subject / Remove Background) are probed and reported as unsupported
 * rather than failing, so the Python side can fall back to the Pillow path.
 */
AINS.PS = (function () {
    var api = {};
    var doc = null;
    var adopted = false;      /* the document was already open when we arrived */

    api.setup = function () {
        app.preferences.rulerUnits = Units.PIXELS;
        app.preferences.typeUnits = TypeUnits.POINTS;
        app.displayDialogs = DialogModes.NO;
        AINS.log("Photoshop " + app.version + " ready");
        return { version: String(app.version), name: String(app.name) };
    };

    api.doc = function () {
        if (doc === null) {
            if (app.documents.length === 0) { throw new Error("No Photoshop document is open"); }
            /* Falling back to whatever the operator has in front of them is
             * fine for reading, but it is not ours to close and discard. */
            doc = app.activeDocument;
            adopted = true;
        }
        return doc;
    };

    /* Adopt a document this code created itself (used by the design library,
     * which builds its own canvas). */
    api.useDocument = function (document) {
        doc = document;
        adopted = false;
        return doc;
    };

    api.openImage = function (path) {
        var file = new File(path);
        if (!file.exists) { throw new Error("Image not found: " + path); }
        doc = app.open(file);
        adopted = false;
        return api.info();
    };

    api.createDocument = function (spec) {
        adopted = false;
        var mode = NewDocumentMode.RGB;
        if (spec.mode === "cmyk") { mode = NewDocumentMode.CMYK; }
        if (spec.mode === "gray") { mode = NewDocumentMode.GRAYSCALE; }
        doc = app.documents.add(
            spec.width, spec.height, spec.dpi || 300, spec.name || "AINS",
            mode, DocumentFill.WHITE
        );
        return api.info();
    };

    api.info = function () {
        var document = api.doc();
        return {
            name: String(document.name),
            width: document.width.as("px"),
            height: document.height.as("px"),
            resolution: document.resolution,
            mode: String(document.mode),
            layers: document.layers.length,
            path: document.saved ? String(document.fullName) : null
        };
    };

    /* ------------------------------------------------------------ geometry */

    api.resize = function (widthPx, heightPx, dpi) {
        var document = api.doc();
        document.resizeImage(
            widthPx ? UnitValue(widthPx, "px") : undefined,
            heightPx ? UnitValue(heightPx, "px") : undefined,
            dpi || document.resolution,
            ResampleMethod.BICUBICSHARPER
        );
        return api.info();
    };

    api.setResolution = function (dpi) {
        var document = api.doc();
        document.resizeImage(undefined, undefined, dpi, ResampleMethod.NONE);
        return api.info();
    };

    api.crop = function (left, top, right, bottom) {
        api.doc().crop([left, top, right, bottom]);
        return api.info();
    };

    /* Crop to an aspect ratio, keeping the centre of interest slightly high. */
    api.cropToAspect = function (aspect, focusY) {
        var document = api.doc();
        var w = document.width.as("px");
        var h = document.height.as("px");
        var current = w / h;
        var left = 0, top = 0, right = w, bottom = h;
        if (Math.abs(current - aspect) < 0.005) { return api.info(); }
        if (current > aspect) {
            var targetW = h * aspect;
            left = (w - targetW) / 2;
            right = left + targetW;
        } else {
            var targetH = w / aspect;
            var bias = (focusY === undefined) ? 0.42 : focusY;
            top = (h - targetH) * bias;
            bottom = top + targetH;
        }
        document.crop([left, top, right, bottom]);
        return api.info();
    };

    api.trimTransparent = function () {
        try {
            api.doc().trim(TrimType.TRANSPARENT, true, true, true, true);
            return true;
        } catch (e) { AINS.log("Trim failed: " + e); return false; }
    };

    /* -------------------------------------------------------- adjustments */

    api.adjust = function (options) {
        var document = api.doc();
        var layer = document.activeLayer;
        options = options || {};
        if (options.brightness !== undefined || options.contrast !== undefined) {
            layer.adjustBrightnessContrast(
                Math.round(options.brightness || 0), Math.round(options.contrast || 0)
            );
        }
        if (options.levels) {
            layer.adjustLevels(
                options.levels[0], options.levels[1], options.levels[2],
                options.levels[3], options.levels[4]
            );
        }
        if (options.saturation !== undefined) {
            layer.adjustColorBalance();     /* no-op guard for older builds  */
            try {
                var desc = new ActionDescriptor();
                desc.putBoolean(stringIDToTypeID("colorize"), false);
                var hsl = new ActionDescriptor();
                hsl.putInteger(stringIDToTypeID("hue"), 0);
                hsl.putInteger(stringIDToTypeID("saturation"), Math.round(options.saturation));
                hsl.putInteger(stringIDToTypeID("lightness"), 0);
                var list = new ActionList();
                list.putObject(stringIDToTypeID("hueSatAdjustmentV2"), hsl);
                desc.putList(stringIDToTypeID("adjustment"), list);
                executeAction(stringIDToTypeID("hueSaturation"), desc, DialogModes.NO);
            } catch (e) { AINS.log("Saturation adjustment unavailable: " + e); }
        }
        if (options.auto_levels === true) {
            try { layer.autoLevels(); } catch (e) { AINS.log("autoLevels: " + e); }
        }
        if (options.auto_contrast === true) {
            try { layer.autoContrast(); } catch (e) { AINS.log("autoContrast: " + e); }
        }
        if (options.sharpen) {
            layer.applyUnSharpMask(options.sharpen.amount || 80,
                                   options.sharpen.radius || 1.2,
                                   options.sharpen.threshold || 3);
        }
        return api.info();
    };

    api.convertMode = function (mode) {
        var document = api.doc();
        if (mode === "cmyk") { document.changeMode(ChangeMode.CMYK); }
        else if (mode === "gray") { document.changeMode(ChangeMode.GRAYSCALE); }
        else { document.changeMode(ChangeMode.RGB); }
        return api.info();
    };

    /* -------------------------------------------------------------- layers */

    api.flatten = function () { api.doc().flatten(); return api.info(); };

    api.toSmartObject = function () {
        try {
            executeAction(stringIDToTypeID("newPlacedLayer"), undefined, DialogModes.NO);
            return true;
        } catch (e) {
            AINS.log("Smart object conversion failed: " + e);
            return false;
        }
    };

    api.duplicateLayer = function (name) {
        var layer = api.doc().activeLayer.duplicate();
        if (name) { layer.name = name; }
        return String(layer.name);
    };

    /* Remove the background. Requires Photoshop 2020 (21.x) or newer. */
    api.removeBackground = function () {
        var document = api.doc();
        try {
            if (document.activeLayer.isBackgroundLayer) {
                document.activeLayer.isBackgroundLayer = false;
            }
        } catch (e) {}
        try {
            executeAction(stringIDToTypeID("removeBackground"), undefined, DialogModes.NO);
            AINS.log("Background removed with the Remove Background action");
            return { ok: true, method: "removeBackground" };
        } catch (e) {
            AINS.log("removeBackground unavailable: " + e);
        }
        try {
            executeAction(stringIDToTypeID("autoCutout"), undefined, DialogModes.NO);
            return { ok: true, method: "autoCutout" };
        } catch (e2) {
            AINS.log("autoCutout unavailable: " + e2);
        }
        try {
            /* Select Subject, then mask - available since Photoshop CC 2018. */
            executeAction(stringIDToTypeID("autoCutoutFromSelection"), undefined, DialogModes.NO);
            return { ok: true, method: "selectSubject" };
        } catch (e3) {
            return { ok: false, method: null, reason: String(e3) };
        }
    };

    api.capabilities = function () {
        var version = parseFloat(String(app.version));
        return {
            version: String(app.version),
            remove_background: version >= 21.0,
            select_subject: version >= 19.0,
            smart_objects: true,
            export_as: version >= 16.0
        };
    };

    /* -------------------------------------------------------------- export */

    api.exportJPEG = function (path, quality) {
        var options = new JPEGSaveOptions();
        options.quality = AINS.clamp(Math.round((quality || 90) / 100 * 12), 1, 12);
        options.embedColorProfile = true;
        options.formatOptions = FormatOptions.OPTIMIZEDBASELINE;
        return api.saveAs(path, options);
    };

    api.exportPNG = function (path) {
        var options = new PNGSaveOptions();
        options.compression = 6;
        options.interlaced = false;
        return api.saveAs(path, options);
    };

    api.exportTIFF = function (path) {
        var options = new TiffSaveOptions();
        options.imageCompression = TIFFEncoding.TIFFLZW;
        options.embedColorProfile = true;
        options.layers = false;
        return api.saveAs(path, options);
    };

    api.savePSD = function (path) {
        var options = new PhotoshopSaveOptions();
        options.embedColorProfile = true;
        options.layers = true;
        options.maximizeCompatibility = true;
        return api.saveAs(path, options);
    };

    api.saveAs = function (path, options) {
        var file = new File(path);
        if (file.parent && !file.parent.exists) { file.parent.create(); }
        api.doc().saveAs(file, options, true, Extension.LOWERCASE);
        AINS.log("Saved " + path);
        return { path: path, exists: new File(path).exists };
    };

    api.closeDocument = function (save) {
        if (doc === null) { return false; }
        if (adopted === true && save !== true) {
            /* Discarding an operator's unsaved work is never this code's
             * decision. Let go of the reference instead. */
            AINS.log("Leaving '" + doc.name + "' open: it was already open here");
            doc = null;
            adopted = false;
            return false;
        }
        doc.close(save ? SaveOptions.SAVECHANGES : SaveOptions.DONOTSAVECHANGES);
        doc = null;
        adopted = false;
        return true;
    };

    api.closeAll = function () {
        while (app.documents.length > 0) {
            app.documents[0].close(SaveOptions.DONOTSAVECHANGES);
        }
        doc = null;
        return true;
    };

    /* ------------------------------------------------------------ recipes */

    /* Full "prepare this photo for print" pass, driven from Python. */
    api.processImage = function (spec) {
        api.openImage(spec.source);
        if (spec.aspect) { api.cropToAspect(spec.aspect, spec.focus_y); }
        if (spec.width_px || spec.height_px) {
            api.resize(spec.width_px, spec.height_px, spec.dpi || 300);
        } else if (spec.dpi) {
            api.setResolution(spec.dpi);
        }
        if (spec.remove_background === true) { api.removeBackground(); }
        if (spec.adjust) { api.adjust(spec.adjust); }
        if (spec.mode) { api.convertMode(spec.mode); }
        if (spec.flatten !== false) { api.flatten(); }
        var result;
        var target = spec.target;
        var lower = String(target).toLowerCase();
        if (lower.match(/\.png$/)) { result = api.exportPNG(target); }
        else if (lower.match(/\.tiff?$/)) { result = api.exportTIFF(target); }
        else if (lower.match(/\.psd$/)) { result = api.savePSD(target); }
        else { result = api.exportJPEG(target, spec.quality || 92); }
        var info = api.info();
        api.closeDocument(false);
        result.width = info.width;
        result.height = info.height;
        result.resolution = info.resolution;
        return result;
    };

    return api;
}());
