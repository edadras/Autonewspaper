/*
 * AI Newspaper Studio - InDesign scripting library.
 *
 * Every InDesign operation the application performs goes through this file.
 * It is prepended to each generated script, so the Python side only has to
 * send data (a layout plan) and a few calls, never DOM code.
 *
 * Compatibility: written for the CS6+ DOM. Middle-Eastern-only features
 * (paragraph direction, digit style, the World-Ready composer) are applied
 * defensively so the same script also runs on a Western InDesign build.
 */
AINS.ID = (function () {
    var api = {};
    var registry = {};          /* frame label -> page item, within one run   */
    var doc = null;

    /* ------------------------------------------------------------ session */

    api.setup = function (options) {
        options = options || {};
        app.scriptPreferences.measurementUnit = MeasurementUnits.MILLIMETERS;
        app.scriptPreferences.userInteractionLevel = UserInteractionLevels.NEVER_INTERACT;
        if (options.enableRedraw === false) {
            try { app.scriptPreferences.enableRedraw = false; } catch (e) {}
        }
        AINS.log("InDesign " + app.version + " ready (" + app.name + ")");
        return { version: String(app.version), name: String(app.name) };
    };

    api.teardown = function () {
        try { app.scriptPreferences.enableRedraw = true; } catch (e) {}
        try {
            app.scriptPreferences.userInteractionLevel = UserInteractionLevels.INTERACT_WITH_ALL;
        } catch (e) {}
    };

    api.doc = function () {
        if (doc === null) {
            if (app.documents.length === 0) { throw new Error("No InDesign document is open"); }
            doc = app.activeDocument;
        }
        return doc;
    };

    api.useDocument = function (document) { registry = {}; doc = document; return doc; };

    /* ----------------------------------------------------------- document */

    api.createDocument = function (spec) {
        /* The queue runner keeps its engine alive between jobs, so the frame
         * registry would otherwise carry references into the previous
         * document - and every lookup would find an item that no longer
         * belongs to the document being built. */
        registry = {};
        var document = app.documents.add(false);
        var prefs = document.documentPreferences;
        prefs.pageWidth = spec.page_width_mm;
        prefs.pageHeight = spec.page_height_mm;
        prefs.facingPages = spec.facing_pages === true;
        prefs.pagesPerDocument = Math.max(1, spec.page_count || 1);
        try {
            prefs.documentBleedUniformSize = true;
            prefs.documentBleedTopOffset = spec.bleed_mm || 0;
            prefs.documentBleedBottomOffset = spec.bleed_mm || 0;
            prefs.documentBleedInsideOrLeftOffset = spec.bleed_mm || 0;
            prefs.documentBleedOutsideOrRightOffset = spec.bleed_mm || 0;
        } catch (e) { AINS.log("Bleed not applied: " + e); }

        document.viewPreferences.horizontalMeasurementUnits = MeasurementUnits.MILLIMETERS;
        document.viewPreferences.verticalMeasurementUnits = MeasurementUnits.MILLIMETERS;
        document.viewPreferences.rulerOrigin = RulerOrigin.PAGE_ORIGIN;
        try { document.zeroPoint = [0, 0]; } catch (e) {}

        doc = document;
        AINS.log("Created document " + spec.page_width_mm + "x" + spec.page_height_mm +
                 " mm, " + prefs.pagesPerDocument + " page(s)");
        return api.info();
    };

    api.openTemplate = function (path, asCopy) {
        registry = {};
        var file = new File(path);
        if (!file.exists) { throw new Error("Template not found: " + path); }
        doc = app.open(file, false, (asCopy === false) ? OpenOptions.OPEN_ORIGINAL : OpenOptions.OPEN_COPY);
        doc.viewPreferences.horizontalMeasurementUnits = MeasurementUnits.MILLIMETERS;
        doc.viewPreferences.verticalMeasurementUnits = MeasurementUnits.MILLIMETERS;
        AINS.log("Opened template " + AINS.basename(path));
        return api.info();
    };

    api.openDocument = function (path) {
        registry = {};
        var file = new File(path);
        if (!file.exists) { throw new Error("Document not found: " + path); }
        doc = app.open(file, false);
        return api.info();
    };

    api.info = function () {
        var document = api.doc();
        return {
            name: String(document.name),
            saved: document.saved,
            path: document.saved ? String(document.fullName) : null,
            pages: document.pages.length,
            width_mm: document.documentPreferences.pageWidth,
            height_mm: document.documentPreferences.pageHeight,
            spreads: document.spreads.length,
            version: String(app.version)
        };
    };

    api.ensurePages = function (count) {
        var document = api.doc();
        while (document.pages.length < count) { document.pages.add(); }
        while (document.pages.length > count && count > 0) {
            document.pages[document.pages.length - 1].remove();
        }
        return document.pages.length;
    };

    api.setMargins = function (pageIndex, margins) {
        var page = api.page(pageIndex);
        var prefs = page.marginPreferences;
        prefs.top = margins.top;
        prefs.bottom = margins.bottom;
        prefs.left = margins.left;
        prefs.right = margins.right;
        if (margins.columns) {
            prefs.columnCount = margins.columns;
            prefs.columnGutter = margins.gutter || 4;
        }
        return true;
    };

    /* Remove every frame this application placed on a page, leaving the
       master-page furniture alone, so a corrected page can be rebuilt. */
    api.clearPage = function (index) {
        var page = api.page(index);
        var items = page.pageItems;
        var removed = 0;
        for (var i = items.length - 1; i >= 0; i--) {
            var item = items[i];
            try {
                if (item.parentPage === null) { continue; }
                delete registry[item.label];
                item.remove();
                removed++;
            } catch (e) {
                AINS.log("Could not remove an item from page " + index + ": " + e);
            }
        }
        return removed;
    };

    api.page = function (index) {
        var document = api.doc();
        if (index < 1 || index > document.pages.length) {
            throw new Error("Page " + index + " does not exist (document has " +
                            document.pages.length + ")");
        }
        return document.pages[index - 1];
    };

    /* ------------------------------------------------------------- assets */

    api.ensureColor = function (spec) {
        var document = api.doc();
        var existing = document.colors.itemByName(spec.name);
        if (existing.isValid) { return spec.name; }
        document.colors.add({
            name: spec.name,
            model: spec.spot ? ColorModel.SPOT : ColorModel.PROCESS,
            space: ColorSpace.CMYK,
            colorValue: [spec.cyan, spec.magenta, spec.yellow, spec.black]
        });
        return spec.name;
    };

    /* Resolve a font, walking the fallback chain; returns the applied name. */
    api.resolveFont = function (family, style, fallbacks) {
        var candidates = [family].concat(fallbacks || []);
        for (var i = 0; i < candidates.length; i++) {
            var name = candidates[i] + "\t" + (style || "Regular");
            var font = app.fonts.itemByName(name);
            if (font.isValid && font.status === FontStatus.INSTALLED) { return name; }
            var plain = app.fonts.itemByName(candidates[i] + "\tRegular");
            if (plain.isValid && plain.status === FontStatus.INSTALLED) { return candidates[i] + "\tRegular"; }
        }
        AINS.log("Font not installed: " + family + " (" + (style || "Regular") + "); InDesign default used");
        return null;
    };

    api.justification = function (name) {
        switch (name) {
        case "left": return Justification.LEFT_ALIGN;
        case "right": return Justification.RIGHT_ALIGN;
        case "center": return Justification.CENTER_ALIGN;
        case "justify": return Justification.FULLY_JUSTIFIED;
        case "justify_last_right": return Justification.RIGHT_JUSTIFIED;
        default: return Justification.LEFT_ALIGN;
        }
    };

    api.ensureParagraphStyle = function (spec) {
        var document = api.doc();
        var style = document.paragraphStyles.itemByName(spec.style_name);
        if (!style.isValid) {
            style = document.paragraphStyles.add({ name: spec.style_name });
        }
        var font = api.resolveFont(spec.font_family, spec.font_style, spec.fallback_fonts);
        if (font) { try { style.appliedFont = font; } catch (e) { AINS.log("appliedFont: " + e); } }
        style.pointSize = spec.size_pt;
        style.leading = spec.leading_pt;
        style.tracking = spec.tracking || 0;
        style.justification = api.justification(spec.alignment);
        style.hyphenation = spec.hyphenation === true;
        style.spaceBefore = spec.space_before_pt || 0;
        style.spaceAfter = spec.space_after_pt || 0;
        if (spec.color) {
            var swatch = document.swatches.itemByName(spec.color);
            if (swatch.isValid) { style.fillColor = swatch; }
        }
        if (spec.all_caps === true) { style.capitalization = Capitalization.ALL_CAPS; }
        if (spec.drop_cap_lines) {
            style.dropCapLines = spec.drop_cap_lines;
            style.dropCapCharacters = 1;
        }
        /* Middle-Eastern / World-Ready settings; absent on Western builds. */
        try {
            style.composer = "Adobe World-Ready Paragraph Composer";
        } catch (e) { AINS.log("World-Ready composer unavailable"); }
        if (spec.direction === "rtl") {
            try {
                style.paragraphDirection = ParagraphDirectionOptions.RIGHT_TO_LEFT_DIRECTION;
            } catch (e) { AINS.log("paragraphDirection unavailable (non-ME build)"); }
            try { style.digitsType = DigitsTypeOptions.ARABIC_DIGITS; } catch (e) {}
            try { style.kashidas = KashidasOptions.KASHIDAS_OFF; } catch (e) {}
        } else {
            try {
                style.paragraphDirection = ParagraphDirectionOptions.LEFT_TO_RIGHT_DIRECTION;
            } catch (e) {}
        }
        return spec.style_name;
    };

    api.ensureObjectStyle = function (spec) {
        var document = api.doc();
        var style = document.objectStyles.itemByName(spec.id);
        if (!style.isValid) { style = document.objectStyles.add({ name: spec.id }); }
        try {
            style.enableStroke = (spec.stroke_weight_pt || 0) > 0;
            if (style.enableStroke) {
                style.strokeWeight = spec.stroke_weight_pt;
                var stroke = document.swatches.itemByName(spec.stroke_color || "Black");
                if (stroke.isValid) { style.strokeColor = stroke; }
            }
            if (spec.fill_color) {
                var fill = document.swatches.itemByName(spec.fill_color);
                if (fill.isValid) { style.fillColor = fill; }
            }
        } catch (e) { AINS.log("Object style " + spec.id + ": " + e); }
        return spec.id;
    };

    api.applyTemplateStyles = function (template) {
        var i;
        for (i = 0; i < (template.colors || []).length; i++) { api.ensureColor(template.colors[i]); }
        for (i = 0; i < (template.paragraph_styles || []).length; i++) {
            api.ensureParagraphStyle(template.paragraph_styles[i]);
        }
        for (i = 0; i < (template.object_styles || []).length; i++) {
            api.ensureObjectStyle(template.object_styles[i]);
        }
        AINS.log("Applied " + (template.paragraph_styles || []).length + " paragraph style(s)");
        return true;
    };

    /* ------------------------------------------------------------- frames */

    /* InDesign geometric bounds are [y1, x1, y2, x2] in the active units. */
    api.bounds = function (rect) {
        return [rect.y, rect.x, rect.y + rect.height, rect.x + rect.width];
    };

    api.register = function (label, item) {
        item.label = label;
        registry[label] = item;
        return item;
    };

    api.find = function (label) {
        if (registry[label]) { return registry[label]; }
        var document = api.doc();
        var items = document.allPageItems;
        for (var i = 0; i < items.length; i++) {
            if (items[i].label === label) { registry[label] = items[i]; return items[i]; }
        }
        return null;
    };

    api.require = function (label) {
        var item = api.find(label);
        if (!item) { throw new Error("Frame '" + label + "' does not exist"); }
        return item;
    };

    api.createTextFrame = function (pageIndex, element) {
        var page = api.page(pageIndex);
        var frame = page.textFrames.add({ geometricBounds: api.bounds(element.rect) });
        api.register(element.id, frame);

        var prefs = frame.textFramePreferences;
        prefs.textColumnCount = Math.max(1, element.columns || 1);
        if (element.columns > 1) { prefs.textColumnGutter = element.column_gutter_mm || 4; }
        prefs.insetSpacing = element.inset_mm || 0;
        prefs.verticalJustification = VerticalJustification.TOP_ALIGN;
        prefs.firstBaselineOffset = FirstBaseline.LEADING_OFFSET;
        if (element.direction === "rtl") {
            try { frame.parentStory.storyPreferences.storyDirection =
                StoryDirectionOptions.RIGHT_TO_LEFT_DIRECTION; } catch (e) {}
        }
        if (element.rotation) { frame.rotationAngle = element.rotation; }
        if (element.text) { api.setText(element.id, element.text, element.style_name); }
        else if (element.style_name) { api.applyParagraphStyle(element.id, element.style_name); }
        if (element.text_wrap_mm) { api.setTextWrap(element.id, element.text_wrap_mm); }
        if (element.object_style) { api.applyObjectStyle(element.id, element.object_style); }
        return { id: element.id, overflows: frame.overflows };
    };

    api.createImageFrame = function (pageIndex, element) {
        var page = api.page(pageIndex);
        var frame = page.rectangles.add({ geometricBounds: api.bounds(element.rect) });
        api.register(element.id, frame);
        try { frame.strokeWeight = element.stroke_weight_pt || 0; } catch (e) {}
        if (element.rotation) { frame.rotationAngle = element.rotation; }
        if (element.text_wrap_mm) { api.setTextWrap(element.id, element.text_wrap_mm); }
        if (element.object_style) { api.applyObjectStyle(element.id, element.object_style); }
        var placed = false;
        if (element.image_path) { placed = api.placeImage(element.id, element.image_path, element.fit_mode); }
        return { id: element.id, placed: placed };
    };

    api.placeImage = function (label, path, fitMode) {
        var frame = api.require(label);
        var file = new File(path);
        if (!file.exists) {
            AINS.log("Image missing: " + path);
            return false;
        }
        if (frame.graphics.length > 0) {
            try { frame.graphics[0].remove(); } catch (e) {}
        }
        frame.place(file);
        api.fitImage(label, fitMode);
        return true;
    };

    api.fitImage = function (label, fitMode) {
        var frame = api.require(label);
        if (frame.graphics.length === 0) { return false; }
        var mode = FitOptions.FILL_PROPORTIONALLY;
        if (fitMode === "fit") { mode = FitOptions.PROPORTIONALLY; }
        else if (fitMode === "proportional") { mode = FitOptions.PROPORTIONALLY; }
        else if (fitMode === "none") { mode = FitOptions.CONTENT_TO_FRAME; }
        frame.fit(mode);
        if (mode === FitOptions.FILL_PROPORTIONALLY) {
            try { frame.fit(FitOptions.CENTER_CONTENT); } catch (e) {}
        }
        return true;
    };

    api.setTextWrap = function (label, offsetMm) {
        var item = api.require(label);
        try {
            item.textWrapPreferences.textWrapMode = TextWrapModes.BOUNDING_BOX_TEXT_WRAP;
            item.textWrapPreferences.textWrapOffset = [offsetMm, offsetMm, offsetMm, offsetMm];
        } catch (e) { AINS.log("Text wrap on " + label + ": " + e); }
        return true;
    };

    api.applyObjectStyle = function (label, styleName) {
        var item = api.require(label);
        var style = api.doc().objectStyles.itemByName(styleName);
        if (style.isValid) { item.appliedObjectStyle = style; return true; }
        return false;
    };

    /* --------------------------------------------------------------- text */

    api.setText = function (label, text, styleName) {
        var frame = api.require(label);
        frame.contents = text;
        if (styleName) { api.applyParagraphStyle(label, styleName); }
        return { overflows: frame.overflows };
    };

    api.appendText = function (label, text) {
        var frame = api.require(label);
        frame.contents = String(frame.contents) + text;
        return { overflows: frame.overflows };
    };

    api.applyParagraphStyle = function (label, styleName) {
        var frame = api.require(label);
        var style = api.doc().paragraphStyles.itemByName(styleName);
        if (!style.isValid) { AINS.log("Paragraph style missing: " + styleName); return false; }
        try {
            frame.parentStory.texts[0].applyParagraphStyle(style, true);
        } catch (e) {
            AINS.log("applyParagraphStyle " + styleName + ": " + e);
            return false;
        }
        return true;
    };

    api.applyCharacterStyle = function (label, styleName, start, length) {
        var frame = api.require(label);
        var style = api.doc().characterStyles.itemByName(styleName);
        if (!style.isValid) { return false; }
        var story = frame.parentStory;
        var to = Math.min(story.characters.length - 1, start + length - 1);
        if (start > to) { return false; }
        story.characters.itemByRange(start, to).applyCharacterStyle(style, true);
        return true;
    };

    /* Link two frames so a story continues on another frame/page. */
    api.threadFrames = function (fromLabel, toLabel) {
        var a = api.require(fromLabel);
        var b = api.require(toLabel);
        a.nextTextFrame = b;
        return { overflows: b.overflows };
    };

    /* ---------------------------------------------------------- geometry */

    api.moveElement = function (label, dxMm, dyMm) {
        var item = api.require(label);
        var b = item.geometricBounds;
        item.geometricBounds = [b[0] + dyMm, b[1] + dxMm, b[2] + dyMm, b[3] + dxMm];
        return api.geometry(label);
    };

    api.setBounds = function (label, rect) {
        var item = api.require(label);
        item.geometricBounds = api.bounds(rect);
        return api.geometry(label);
    };

    api.resizeElement = function (label, factor) {
        var item = api.require(label);
        var b = item.geometricBounds;
        var h = (b[2] - b[0]) * factor;
        var w = (b[3] - b[1]) * factor;
        item.geometricBounds = [b[0], b[1], b[0] + h, b[1] + w];
        return api.geometry(label);
    };

    api.geometry = function (label) {
        var item = api.require(label);
        var b = item.geometricBounds;
        return {
            id: label,
            x: b[1], y: b[0], width: b[3] - b[1], height: b[2] - b[0],
            overflows: (item.constructor.name === "TextFrame") ? item.overflows : false
        };
    };

    api.alignToPage = function (label, mode) {
        var item = api.require(label);
        var page = item.parentPage;
        var pb = page.bounds;                     /* [y1, x1, y2, x2] */
        var b = item.geometricBounds;
        var w = b[3] - b[1], h = b[2] - b[0];
        var x = b[1], y = b[0];
        if (mode === "center" || mode === "center_horizontal") { x = pb[1] + ((pb[3] - pb[1]) - w) / 2; }
        if (mode === "center" || mode === "center_vertical") { y = pb[0] + ((pb[2] - pb[0]) - h) / 2; }
        if (mode === "left") { x = pb[1]; }
        if (mode === "right") { x = pb[3] - w; }
        if (mode === "top") { y = pb[0]; }
        if (mode === "bottom") { y = pb[2] - h; }
        item.geometricBounds = [y, x, y + h, x + w];
        return api.geometry(label);
    };

    api.deleteElement = function (label) {
        var item = api.find(label);
        if (!item) { return false; }
        item.remove();
        delete registry[label];
        return true;
    };

    api.bringToFront = function (label) {
        api.require(label).bringToFront();
        return true;
    };

    /* --------------------------------------------------------- inspection */

    api.detectOverflow = function () {
        var document = api.doc();
        var out = [];
        var frames = document.textFrames;
        for (var i = 0; i < frames.length; i++) {
            var frame = frames[i];
            if (frame.overflows) {
                var page = frame.parentPage;
                out.push({
                    id: String(frame.label),
                    page: page ? page.documentOffset + 1 : null,
                    characters: frame.parentStory.characters.length,
                    visible: frame.characters.length
                });
            }
        }
        AINS.log("Overflow check: " + out.length + " frame(s) overflow");
        return out;
    };

    api.pageReport = function (pageIndex) {
        var page = api.page(pageIndex);
        var items = page.pageItems;
        var out = [];
        for (var i = 0; i < items.length; i++) {
            var item = items[i];
            var b = item.geometricBounds;
            var entry = {
                id: String(item.label),
                kind: String(item.constructor.name),
                x: b[1], y: b[0], width: b[3] - b[1], height: b[2] - b[0]
            };
            if (item.constructor.name === "TextFrame") {
                entry.overflows = item.overflows;
                entry.characters = item.parentStory.characters.length;
                try { entry.point_size = item.parentStory.characters[0].pointSize; } catch (e) {}
            }
            if (item.constructor.name === "Rectangle" && item.graphics.length > 0) {
                var graphic = item.graphics[0];
                entry.image = true;
                try {
                    entry.effective_ppi = graphic.effectivePpi ? graphic.effectivePpi[0] : null;
                    entry.link = graphic.itemLink ? String(graphic.itemLink.name) : null;
                    entry.link_status = graphic.itemLink ? String(graphic.itemLink.status) : null;
                } catch (e) {}
            }
            out.push(entry);
        }
        return { page: pageIndex, items: out };
    };

    api.linkReport = function () {
        var document = api.doc();
        var out = [];
        for (var i = 0; i < document.links.length; i++) {
            var link = document.links[i];
            out.push({
                name: String(link.name),
                status: String(link.status),
                path: String(link.filePath)
            });
        }
        return out;
    };

    api.updateLinks = function () {
        var document = api.doc();
        var updated = 0;
        for (var i = 0; i < document.links.length; i++) {
            if (document.links[i].status === LinkStatus.LINK_OUT_OF_DATE) {
                document.links[i].update();
                updated++;
            }
        }
        return updated;
    };

    /* ------------------------------------------------------------ masters */

    api.applyMaster = function (pageIndex, masterName) {
        var document = api.doc();
        var master = document.masterSpreads.itemByName(masterName);
        if (!master.isValid) {
            for (var i = 0; i < document.masterSpreads.length; i++) {
                var spread = document.masterSpreads[i];
                if (spread.namePrefix + "-" + spread.baseName === masterName ||
                    spread.baseName === masterName) {
                    master = spread;
                    break;
                }
            }
        }
        if (!master.isValid) { AINS.log("Master '" + masterName + "' not found"); return false; }
        api.page(pageIndex).appliedMaster = master;
        return true;
    };

    /* ------------------------------------------------------------- export */

    api.exportPDF = function (path, presetName, options) {
        var document = api.doc();
        options = options || {};
        var file = new File(path);
        if (file.parent && !file.parent.exists) { file.parent.create(); }
        var preset = presetName ? app.pdfExportPresets.itemByName(presetName) : null;
        if (preset && preset.isValid) {
            document.exportFile(ExportFormat.PDF_TYPE, file, false, preset);
        } else {
            if (presetName) { AINS.log("PDF preset '" + presetName + "' not found; using explicit settings"); }
            var prefs = app.pdfExportPreferences;
            prefs.pageInformationMarks = options.marks === true;
            prefs.cropMarks = options.marks === true;
            prefs.bleedMarks = options.marks === true;
            prefs.useDocumentBleedWithPDF = options.bleed === true;
            prefs.colorBitmapSampling = Sampling.BICUBIC_DOWNSAMPLE;
            prefs.colorBitmapSamplingDPI = options.dpi || 300;
            prefs.grayscaleBitmapSamplingDPI = options.dpi || 300;
            prefs.subsetFontsBelow = 0;
            prefs.exportGuidesAndGrids = false;
            prefs.viewPDF = false;
            document.exportFile(ExportFormat.PDF_TYPE, file, false);
        }
        AINS.log("Exported PDF -> " + path);
        return { path: path, exists: new File(path).exists };
    };

    api.exportPagePreview = function (path, pageIndex, dpi, format) {
        var document = api.doc();
        var file = new File(path);
        if (file.parent && !file.parent.exists) { file.parent.create(); }
        var page = api.page(pageIndex);
        if (format === "jpeg" || format === "jpg") {
            app.jpegExportPreferences.exportResolution = dpi || 110;
            app.jpegExportPreferences.jpegQuality = JPEGOptionsQuality.HIGH;
            app.jpegExportPreferences.jpegExportRange = ExportRangeOrAllPages.EXPORT_RANGE;
            app.jpegExportPreferences.pageString = String(page.name);
            app.jpegExportPreferences.jpegColorSpace = JpegColorSpaceEnum.RGB;
            app.jpegExportPreferences.useDocumentBleeds = false;
            document.exportFile(ExportFormat.JPG, file, false);
        } else {
            app.pngExportPreferences.exportResolution = dpi || 110;
            app.pngExportPreferences.pngExportRange = PNGExportRangeEnum.EXPORT_RANGE;
            app.pngExportPreferences.pageString = String(page.name);
            app.pngExportPreferences.transparentBackground = false;
            app.pngExportPreferences.useDocumentBleeds = false;
            document.exportFile(ExportFormat.PNG_FORMAT, file, false);
        }
        return { path: path, exists: new File(path).exists, page: pageIndex };
    };

    api.exportIDML = function (path) {
        var file = new File(path);
        if (file.parent && !file.parent.exists) { file.parent.create(); }
        api.doc().exportFile(ExportFormat.INDESIGN_MARKUP, file);
        AINS.log("Exported IDML -> " + path);
        return { path: path, exists: new File(path).exists };
    };

    api.save = function (path) {
        var document = api.doc();
        if (path) {
            var file = new File(path);
            if (file.parent && !file.parent.exists) { file.parent.create(); }
            document.save(file);
        } else {
            document.save();
        }
        return { path: String(document.fullName), saved: document.saved };
    };

    api.packageDocument = function (folderPath) {
        var folder = AINS.toFolder(folderPath);
        var ok = api.doc().packageForPrint(folder, true, true, true, true, false, false, true, false);
        return { path: folderPath, ok: ok };
    };

    api.closeDocument = function (save) {
        if (doc === null) { return false; }
        doc.close(save ? SaveOptions.YES : SaveOptions.NO);
        doc = null;
        registry = {};
        return true;
    };

    api.closeAll = function () {
        while (app.documents.length > 0) {
            app.documents[0].close(SaveOptions.NO);
        }
        doc = null;
        registry = {};
        return true;
    };

    /* --------------------------------------------------- plan application */

    /* Build a whole page from a plan object produced by the layout engine. */
    api.buildPage = function (page, template) {
        var index = page.index;
        api.setMargins(index, {
            top: page.margin_top_mm,
            bottom: page.margin_bottom_mm,
            left: page.margin_inside_mm,
            right: page.margin_outside_mm,
            columns: page.columns,
            gutter: page.gutter_mm
        });
        if (page.master) { api.applyMaster(index, page.master); }

        var created = [];
        for (var i = 0; i < page.elements.length; i++) {
            var element = page.elements[i];
            try {
                if (element.kind === "image") {
                    created.push(api.createImageFrame(index, element));
                } else {
                    created.push(api.createTextFrame(index, element));
                }
            } catch (e) {
                AINS.log("Element " + element.id + " failed: " + e);
                created.push({ id: element.id, error: String(e) });
            }
        }
        return { page: index, elements: created };
    };

    api.buildDocument = function (plan, template) {
        api.ensurePages(plan.pages.length);
        var results = [];
        for (var i = 0; i < plan.pages.length; i++) {
            results.push(api.buildPage(plan.pages[i], template));
        }
        return { pages: results, overflow: api.detectOverflow() };
    };

    return api;
}());
