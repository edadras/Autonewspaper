/*
 * AI Newspaper Studio - Photoshop design library.
 *
 * The image library next door prepares photographs. This one builds
 * compositions: type, shapes, gradients, layer effects, smart objects and
 * artboards - what a poster, a cover or a social post is actually made of.
 *
 * Photoshop's DOM stops well short of what the interface can do, so anything
 * it does not expose (layer styles, shape layers, gradient fills, blend
 * ranges) goes through the Action Manager, which is the same mechanism the
 * interface itself uses. Every such call is wrapped so a version that names a
 * key differently degrades to a plain layer rather than failing the run.
 *
 * Coordinates are in pixels from the top-left of the canvas, which is what
 * the layout engine hands over after converting from millimetres.
 */
AINS.PSD = (function () {
    var api = {};
    var registry = {};          /* layer name -> ArtLayer, within one run */

    function cid(s) { return charIDToTypeID(s); }
    function sid(s) { return stringIDToTypeID(s); }

    /* ------------------------------------------------------------ helpers */

    api.reset = function () { registry = {}; return true; };

    api.doc = function () { return AINS.PS.doc(); };

    /* Photoshop measures in the document's own unit; force pixels so the
     * numbers the layout engine sends mean what they say. */
    api.pixelUnits = function () {
        var previous = app.preferences.rulerUnits;
        app.preferences.rulerUnits = Units.PIXELS;
        return previous;
    };

    api.restoreUnits = function (previous) {
        if (previous !== undefined && previous !== null) { app.preferences.rulerUnits = previous; }
    };

    api.register = function (name, layer) {
        registry[name] = layer;
        return layer;
    };

    api.find = function (name) {
        if (registry[name]) { return registry[name]; }
        var document = api.doc();
        var found = null;
        function walk(collection) {
            for (var i = 0; i < collection.length && found === null; i++) {
                var layer = collection[i];
                if (String(layer.name) === String(name)) { found = layer; return; }
                if (layer.typename === "LayerSet") { walk(layer.layers); }
            }
        }
        walk(document.layers);
        if (found) { registry[name] = found; }
        return found;
    };

    api.require = function (name) {
        var layer = api.find(name);
        if (!layer) { throw new Error("Layer '" + name + "' does not exist"); }
        return layer;
    };

    /* A colour from {r,g,b} 0-255, or "#rrggbb". */
    api.color = function (value) {
        var colour = new SolidColor();
        if (typeof value === "string") {
            var hex = value.replace("#", "");
            if (hex.length === 3) {
                hex = hex.charAt(0) + hex.charAt(0) + hex.charAt(1) + hex.charAt(1) +
                      hex.charAt(2) + hex.charAt(2);
            }
            colour.rgb.hexValue = hex.toUpperCase();
            return colour;
        }
        colour.rgb.red = value && value.r !== undefined ? value.r : 0;
        colour.rgb.green = value && value.g !== undefined ? value.g : 0;
        colour.rgb.blue = value && value.b !== undefined ? value.b : 0;
        return colour;
    };

    api.blendMode = function (name) {
        var modes = {
            normal: BlendMode.NORMAL, multiply: BlendMode.MULTIPLY, screen: BlendMode.SCREEN,
            overlay: BlendMode.OVERLAY, darken: BlendMode.DARKEN, lighten: BlendMode.LIGHTEN,
            soft_light: BlendMode.SOFTLIGHT, hard_light: BlendMode.HARDLIGHT,
            color_dodge: BlendMode.COLORDODGE, color_burn: BlendMode.COLORBURN,
            difference: BlendMode.DIFFERENCE, exclusion: BlendMode.EXCLUSION,
            hue: BlendMode.HUE, saturation: BlendMode.SATURATION,
            color: BlendMode.COLORBLEND, luminosity: BlendMode.LUMINOSITY
        };
        return modes[String(name || "normal").toLowerCase()] || BlendMode.NORMAL;
    };

    /* --------------------------------------------------------- the canvas */

    api.createCanvas = function (spec) {
        var previous = api.pixelUnits();
        try {
            var mode = NewDocumentMode.RGB;
            if (spec.mode === "cmyk") { mode = NewDocumentMode.CMYK; }
            if (spec.mode === "gray") { mode = NewDocumentMode.GRAYSCALE; }
            var fill = DocumentFill.WHITE;
            if (spec.background === "transparent") { fill = DocumentFill.TRANSPARENT; }
            var document = app.documents.add(
                spec.width_px, spec.height_px, spec.dpi || 300,
                spec.name || "Design", mode, fill,
                1.0, BitsPerChannelType.EIGHT
            );
            AINS.PS.useDocument(document);
            registry = {};
            if (spec.background && spec.background !== "transparent" && spec.background !== "white") {
                api.fillBackground(spec.background);
            }
            AINS.log("Canvas " + spec.width_px + "x" + spec.height_px + " at " + (spec.dpi || 300) + " dpi");
            return AINS.PS.info();
        } finally {
            api.restoreUnits(previous);
        }
    };

    api.fillBackground = function (colour) {
        var document = api.doc();
        var layer = document.artLayers.add();
        layer.name = "background";
        document.selection.selectAll();
        document.selection.fill(api.color(colour));
        document.selection.deselect();
        layer.move(document, ElementPlacement.PLACEATEND);
        api.register("background", layer);
        return true;
    };

    /* ------------------------------------------------------------ groups */

    api.createGroup = function (name, parentName) {
        var document = api.doc();
        var parent = parentName ? api.find(parentName) : null;
        var group = (parent && parent.typename === "LayerSet")
            ? parent.layerSets.add()
            : document.layerSets.add();
        group.name = name;
        api.register(name, group);
        return { name: name };
    };

    /* -------------------------------------------------------------- type */

    /* A paragraph (box) or point text layer.
     *
     * spec: {name, text, x, y, width, height, font, size_pt, leading_pt,
     *        tracking, color, alignment, direction, all_caps, opacity,
     *        blend_mode, rotation, group}
     */
    api.createText = function (spec) {
        var previous = api.pixelUnits();
        try {
            var document = api.doc();
            var layer = document.artLayers.add();
            layer.kind = LayerKind.TEXT;
            layer.name = spec.name || ("text_" + document.artLayers.length);

            var item = layer.textItem;
            /* Paragraph text needs its kind set before its box, and the box
             * before the contents, or Photoshop reflows to the wrong width. */
            if (spec.width && spec.height) {
                item.kind = TextType.PARAGRAPHTEXT;
                item.width = new UnitValue(spec.width, "px");
                item.height = new UnitValue(spec.height, "px");
            } else {
                item.kind = TextType.POINTTEXT;
            }
            api.applyFont(item, spec);
            item.contents = String(spec.text === undefined ? "" : spec.text);
            item.position = [new UnitValue(spec.x || 0, "px"), new UnitValue(spec.y || 0, "px")];

            api.setDirection(layer, spec.direction);
            api.finishLayer(layer, spec);
            api.register(layer.name, layer);
            return { name: layer.name, kind: "text" };
        } finally {
            api.restoreUnits(previous);
        }
    };

    api.applyFont = function (item, spec) {
        if (spec.font) {
            var resolved = api.resolveFont(spec.font, spec.font_style, spec.fallback_fonts);
            if (resolved) { item.font = resolved; }
        }
        if (spec.size_pt) { item.size = new UnitValue(spec.size_pt, "pt"); }
        if (spec.leading_pt) {
            item.useAutoLeading = false;
            item.leading = new UnitValue(spec.leading_pt, "pt");
        }
        if (spec.tracking !== undefined && spec.tracking !== null) { item.tracking = spec.tracking; }
        if (spec.color) { item.color = api.color(spec.color); }
        if (spec.all_caps === true) { item.capitalization = TextCase.ALLCAPS; }
        var justify = {
            left: Justification.LEFT, right: Justification.RIGHT,
            center: Justification.CENTER, centre: Justification.CENTER,
            justify: Justification.FULLYJUSTIFIED
        };
        if (spec.alignment && justify[String(spec.alignment).toLowerCase()]) {
            item.justification = justify[String(spec.alignment).toLowerCase()];
        }
    };

    /* PostScript name for a family/style, falling back through the list. */
    api.resolveFont = function (family, style, fallbacks) {
        var wanted = [family].concat(fallbacks || []);
        for (var i = 0; i < wanted.length; i++) {
            var match = api.findFont(wanted[i], style);
            if (match) { return match; }
        }
        AINS.log("Font '" + family + "' is not installed; Photoshop's default is used");
        return null;
    };

    api.findFont = function (family, style) {
        if (!family) { return null; }
        var target = String(family).toLowerCase().replace(/[\s\-]/g, "");
        var wantedStyle = String(style || "regular").toLowerCase().replace(/[\s\-]/g, "");
        var exact = null;
        var sameFamily = null;
        for (var i = 0; i < app.fonts.length; i++) {
            var font = app.fonts[i];
            var name = String(font.family).toLowerCase().replace(/[\s\-]/g, "");
            if (name !== target) { continue; }
            var fontStyle = String(font.style).toLowerCase().replace(/[\s\-]/g, "");
            if (sameFamily === null) { sameFamily = font.postScriptName; }
            if (fontStyle === wantedStyle) { exact = font.postScriptName; break; }
        }
        return exact || sameFamily;
    };

    /* Right-to-left type needs the Middle Eastern engine, which older or
     * non-ME builds do not have; the layer is still correct without it. */
    api.setDirection = function (layer, direction) {
        if (String(direction) !== "rtl") { return false; }
        try {
            var descriptor = new ActionDescriptor();
            var reference = new ActionReference();
            reference.putEnumerated(cid("TxLr"), cid("Ordn"), cid("Trgt"));
            descriptor.putReference(cid("null"), reference);
            var text = new ActionDescriptor();
            text.putEnumerated(sid("textOrientation"), sid("textOrientation"), sid("rightToLeft"));
            descriptor.putObject(cid("T   "), cid("TxLr"), text);
            executeAction(cid("setd"), descriptor, DialogModes.NO);
            return true;
        } catch (e) {
            AINS.log("Right-to-left type is not available in this build: " + e);
            return false;
        }
    };

    /* ------------------------------------------------------------ shapes */

    /* A filled rectangle, rounded rectangle or ellipse as a real shape layer.
     *
     * spec: {name, shape, x, y, width, height, radius, color, opacity,
     *        blend_mode, group}
     */
    api.createShape = function (spec) {
        var previous = api.pixelUnits();
        try {
            var document = api.doc();
            var shape = String(spec.shape || "rectangle").toLowerCase();
            var x = spec.x || 0, y = spec.y || 0;
            var width = spec.width || 100, height = spec.height || 100;

            var points;
            if (shape === "ellipse" || shape === "circle") {
                points = api.ellipsePath(x, y, width, height);
            } else if (shape === "rounded" || shape === "rounded_rectangle") {
                points = api.roundedPath(x, y, width, height, spec.radius || 12);
            } else if (shape === "polygon" && spec.points) {
                points = api.polygonPath(spec.points);
            } else {
                points = api.rectanglePath(x, y, width, height);
            }

            var subPath = new SubPathInfo();
            subPath.closed = true;
            subPath.operation = ShapeOperation.SHAPEADD;
            subPath.entireSubPath = points;
            var path = document.pathItems.add(spec.name || "shape", [subPath]);
            path.fillPath(api.color(spec.color || "#000000"));

            var layer = document.activeLayer;
            layer.name = spec.name || ("shape_" + document.artLayers.length);
            path.remove();
            api.finishLayer(layer, spec);
            api.register(layer.name, layer);
            return { name: layer.name, kind: "shape" };
        } finally {
            api.restoreUnits(previous);
        }
    };

    api.point = function (x, y) {
        var point = new PathPointInfo();
        point.kind = PointKind.CORNERPOINT;
        point.anchor = [x, y];
        point.leftDirection = [x, y];
        point.rightDirection = [x, y];
        return point;
    };

    api.smoothPoint = function (x, y, leftX, leftY, rightX, rightY) {
        var point = new PathPointInfo();
        point.kind = PointKind.SMOOTHPOINT;
        point.anchor = [x, y];
        point.leftDirection = [leftX, leftY];
        point.rightDirection = [rightX, rightY];
        return point;
    };

    api.rectanglePath = function (x, y, width, height) {
        return [api.point(x, y), api.point(x + width, y),
                api.point(x + width, y + height), api.point(x, y + height)];
    };

    api.roundedPath = function (x, y, width, height, radius) {
        var r = Math.min(radius, Math.min(width, height) / 2);
        var k = r * 0.5523;   /* the circle-to-Bezier constant */
        var right = x + width, bottom = y + height;
        return [
            api.smoothPoint(x + r, y, x + r - k, y, x + r + k, y),
            api.smoothPoint(right - r, y, right - r - k, y, right - r + k, y),
            api.smoothPoint(right, y + r, right, y + r - k, right, y + r + k),
            api.smoothPoint(right, bottom - r, right, bottom - r - k, right, bottom - r + k),
            api.smoothPoint(right - r, bottom, right - r + k, bottom, right - r - k, bottom),
            api.smoothPoint(x + r, bottom, x + r + k, bottom, x + r - k, bottom),
            api.smoothPoint(x, bottom - r, x, bottom - r + k, x, bottom - r - k),
            api.smoothPoint(x, y + r, x, y + r + k, x, y + r - k)
        ];
    };

    api.ellipsePath = function (x, y, width, height) {
        var cx = x + width / 2, cy = y + height / 2;
        var rx = width / 2, ry = height / 2;
        var kx = rx * 0.5523, ky = ry * 0.5523;
        return [
            api.smoothPoint(cx, y, cx - kx, y, cx + kx, y),
            api.smoothPoint(x + width, cy, x + width, cy - ky, x + width, cy + ky),
            api.smoothPoint(cx, y + height, cx + kx, y + height, cx - kx, y + height),
            api.smoothPoint(x, cy, x, cy + ky, x, cy - ky)
        ];
    };

    api.polygonPath = function (points) {
        var out = [];
        for (var i = 0; i < points.length; i++) {
            out.push(api.point(points[i][0], points[i][1]));
        }
        return out;
    };

    /* --------------------------------------------------------- placement */

    /* Place a file as a linked or embedded smart object, then fit it.
     *
     * spec: {name, path, x, y, width, height, fit, linked, opacity,
     *        blend_mode, group}
     */
    api.placeFile = function (spec) {
        var previous = api.pixelUnits();
        try {
            var file = new File(spec.path);
            if (!file.exists) { throw new Error("File not found: " + spec.path); }

            var descriptor = new ActionDescriptor();
            descriptor.putPath(cid("null"), file);
            descriptor.putEnumerated(cid("FTcs"), cid("QCSt"), cid("Qcsa"));
            if (spec.linked === true) { descriptor.putBoolean(sid("linked"), true); }
            executeAction(cid("Plc "), descriptor, DialogModes.NO);

            var document = api.doc();
            var layer = document.activeLayer;
            layer.name = spec.name || ("placed_" + document.artLayers.length);
            if (spec.width && spec.height) {
                api.fitLayer(layer, spec.x || 0, spec.y || 0, spec.width, spec.height, spec.fit || "cover");
            } else if (spec.x !== undefined && spec.y !== undefined) {
                api.moveLayerTo(layer, spec.x, spec.y);
            }
            api.finishLayer(layer, spec);
            api.register(layer.name, layer);
            return { name: layer.name, kind: "placed" };
        } finally {
            api.restoreUnits(previous);
        }
    };

    api.bounds = function (layer) {
        var b = layer.bounds;
        return {
            x: b[0].as("px"), y: b[1].as("px"),
            width: b[2].as("px") - b[0].as("px"),
            height: b[3].as("px") - b[1].as("px")
        };
    };

    api.moveLayerTo = function (layer, x, y) {
        var current = api.bounds(layer);
        layer.translate(new UnitValue(x - current.x, "px"), new UnitValue(y - current.y, "px"));
        return true;
    };

    /* Scale a layer into a box: "cover" fills it and overflows, "contain"
     * fits inside it, "stretch" ignores the aspect ratio. */
    api.fitLayer = function (layer, x, y, width, height, mode) {
        var current = api.bounds(layer);
        if (current.width <= 0 || current.height <= 0) { return false; }
        var scaleX = width / current.width, scaleY = height / current.height;
        var scale;
        if (mode === "contain") { scale = Math.min(scaleX, scaleY); }
        else if (mode === "stretch") { scale = null; }
        else { scale = Math.max(scaleX, scaleY); }

        var previousLayer = app.activeDocument.activeLayer;
        app.activeDocument.activeLayer = layer;
        if (scale === null) {
            layer.resize(scaleX * 100, scaleY * 100, AnchorPosition.MIDDLECENTER);
        } else {
            layer.resize(scale * 100, scale * 100, AnchorPosition.MIDDLECENTER);
        }
        var after = api.bounds(layer);
        layer.translate(
            new UnitValue(x + width / 2 - (after.x + after.width / 2), "px"),
            new UnitValue(y + height / 2 - (after.y + after.height / 2), "px")
        );
        app.activeDocument.activeLayer = previousLayer;
        return true;
    };

    /* Clip a layer to the one below it - how a photograph is masked into a
     * shape without touching its pixels. */
    api.clipToBelow = function (name) {
        var layer = api.require(name);
        app.activeDocument.activeLayer = layer;
        try {
            executeAction(sid("groupEvent"), undefined, DialogModes.NO);
            return true;
        } catch (e) {
            AINS.log("Could not clip '" + name + "': " + e);
            return false;
        }
    };

    /* ------------------------------------------------------- layer finish */

    api.finishLayer = function (layer, spec) {
        if (spec.opacity !== undefined && spec.opacity !== null) {
            layer.opacity = Math.max(0, Math.min(100, spec.opacity));
        }
        if (spec.blend_mode) { layer.blendMode = api.blendMode(spec.blend_mode); }
        if (spec.rotation) { layer.rotate(spec.rotation, AnchorPosition.MIDDLECENTER); }
        if (spec.group) {
            var group = api.find(spec.group);
            if (group && group.typename === "LayerSet") {
                layer.move(group, ElementPlacement.INSIDE);
            }
        }
        if (spec.effects) { api.applyEffects(layer.name, spec.effects); }
        return layer;
    };

    /* ----------------------------------------------------------- effects */

    /* Layer styles. The DOM does not expose them at all, so every one of
     * these is an Action Manager descriptor - the same call the interface
     * makes. A build that names a key differently loses the effect and keeps
     * the layer, which is the right way round.
     *
     * spec: {shadow:{...}, glow:{...}, stroke:{...}, overlay:{...},
     *        gradient:{...}, bevel:{...}}
     */
    api.applyEffects = function (name, spec) {
        var layer = api.require(name);
        app.activeDocument.activeLayer = layer;
        var applied = [];
        try {
            var effects = new ActionDescriptor();
            effects.putUnitDouble(sid("scale"), sid("percentUnit"), 100);

            if (spec.shadow) { api.dropShadow(effects, spec.shadow); applied.push("shadow"); }
            if (spec.inner_shadow) { api.innerShadow(effects, spec.inner_shadow); applied.push("inner_shadow"); }
            if (spec.glow) { api.outerGlow(effects, spec.glow); applied.push("glow"); }
            if (spec.stroke) { api.strokeEffect(effects, spec.stroke); applied.push("stroke"); }
            if (spec.overlay) { api.colorOverlay(effects, spec.overlay); applied.push("overlay"); }
            if (spec.gradient) { api.gradientOverlay(effects, spec.gradient); applied.push("gradient"); }
            if (!applied.length) { return { applied: [] }; }

            var descriptor = new ActionDescriptor();
            var reference = new ActionReference();
            reference.putProperty(cid("Prpr"), sid("layerEffects"));
            reference.putEnumerated(cid("Lyr "), cid("Ordn"), cid("Trgt"));
            descriptor.putReference(cid("null"), reference);
            descriptor.putObject(cid("T   "), sid("layerEffects"), effects);
            executeAction(cid("setd"), descriptor, DialogModes.NO);
            return { applied: applied };
        } catch (e) {
            AINS.log("Layer effects on '" + name + "' were not applied: " + e);
            return { applied: [], error: String(e) };
        }
    };

    api.colorDescriptor = function (value) {
        var colour = api.color(value);
        var descriptor = new ActionDescriptor();
        descriptor.putDouble(cid("Rd  "), colour.rgb.red);
        descriptor.putDouble(cid("Grn "), colour.rgb.green);
        descriptor.putDouble(cid("Bl  "), colour.rgb.blue);
        return descriptor;
    };

    api.dropShadow = function (effects, spec) {
        var shadow = new ActionDescriptor();
        shadow.putBoolean(cid("enab"), true);
        shadow.putEnumerated(cid("Md  "), cid("BlnM"), sid(spec.blend_mode || "multiply"));
        shadow.putObject(cid("Clr "), cid("RGBC"), api.colorDescriptor(spec.color || "#000000"));
        shadow.putUnitDouble(cid("Opct"), sid("percentUnit"), spec.opacity === undefined ? 45 : spec.opacity);
        shadow.putUnitDouble(cid("lagl"), sid("angleUnit"), spec.angle === undefined ? 120 : spec.angle);
        shadow.putUnitDouble(cid("Dstn"), sid("pixelsUnit"), spec.distance === undefined ? 8 : spec.distance);
        shadow.putUnitDouble(cid("blur"), sid("pixelsUnit"), spec.size === undefined ? 18 : spec.size);
        shadow.putUnitDouble(cid("Ckmt"), sid("pixelsUnit"), spec.spread || 0);
        effects.putObject(sid("dropShadow"), sid("dropShadow"), shadow);
    };

    api.innerShadow = function (effects, spec) {
        var shadow = new ActionDescriptor();
        shadow.putBoolean(cid("enab"), true);
        shadow.putEnumerated(cid("Md  "), cid("BlnM"), sid(spec.blend_mode || "multiply"));
        shadow.putObject(cid("Clr "), cid("RGBC"), api.colorDescriptor(spec.color || "#000000"));
        shadow.putUnitDouble(cid("Opct"), sid("percentUnit"), spec.opacity === undefined ? 35 : spec.opacity);
        shadow.putUnitDouble(cid("lagl"), sid("angleUnit"), spec.angle === undefined ? 120 : spec.angle);
        shadow.putUnitDouble(cid("Dstn"), sid("pixelsUnit"), spec.distance === undefined ? 5 : spec.distance);
        shadow.putUnitDouble(cid("blur"), sid("pixelsUnit"), spec.size === undefined ? 10 : spec.size);
        effects.putObject(sid("innerShadow"), sid("innerShadow"), shadow);
    };

    api.outerGlow = function (effects, spec) {
        var glow = new ActionDescriptor();
        glow.putBoolean(cid("enab"), true);
        glow.putEnumerated(cid("Md  "), cid("BlnM"), sid(spec.blend_mode || "screen"));
        glow.putObject(cid("Clr "), cid("RGBC"), api.colorDescriptor(spec.color || "#ffffff"));
        glow.putUnitDouble(cid("Opct"), sid("percentUnit"), spec.opacity === undefined ? 60 : spec.opacity);
        glow.putUnitDouble(cid("blur"), sid("pixelsUnit"), spec.size === undefined ? 20 : spec.size);
        effects.putObject(sid("outerGlow"), sid("outerGlow"), glow);
    };

    api.strokeEffect = function (effects, spec) {
        var stroke = new ActionDescriptor();
        stroke.putBoolean(cid("enab"), true);
        stroke.putEnumerated(cid("Styl"), cid("FStl"), sid(spec.position || "outsetFrame"));
        stroke.putEnumerated(cid("PntT"), cid("FrFl"), sid("solidColor"));
        stroke.putEnumerated(cid("Md  "), cid("BlnM"), sid(spec.blend_mode || "normal"));
        stroke.putUnitDouble(cid("Opct"), sid("percentUnit"), spec.opacity === undefined ? 100 : spec.opacity);
        stroke.putUnitDouble(cid("Sz  "), sid("pixelsUnit"), spec.size === undefined ? 4 : spec.size);
        stroke.putObject(cid("Clr "), cid("RGBC"), api.colorDescriptor(spec.color || "#000000"));
        effects.putObject(sid("frameFX"), sid("frameFX"), stroke);
    };

    api.colorOverlay = function (effects, spec) {
        var overlay = new ActionDescriptor();
        overlay.putBoolean(cid("enab"), true);
        overlay.putEnumerated(cid("Md  "), cid("BlnM"), sid(spec.blend_mode || "normal"));
        overlay.putObject(cid("Clr "), cid("RGBC"), api.colorDescriptor(spec.color || "#000000"));
        overlay.putUnitDouble(cid("Opct"), sid("percentUnit"), spec.opacity === undefined ? 100 : spec.opacity);
        effects.putObject(sid("solidFill"), sid("solidFill"), overlay);
    };

    api.gradientOverlay = function (effects, spec) {
        var stops = spec.stops || [
            { color: spec.from || "#000000", location: 0, opacity: 100 },
            { color: spec.to || "#ffffff", location: 100, opacity: 100 }
        ];
        var colours = new ActionList();
        var transparency = new ActionList();
        for (var i = 0; i < stops.length; i++) {
            var stop = stops[i];
            var location = Math.round((stop.location === undefined ? (i * 100 / Math.max(1, stops.length - 1)) : stop.location) * 40.96);
            var colourStop = new ActionDescriptor();
            colourStop.putObject(cid("Clr "), cid("RGBC"), api.colorDescriptor(stop.color || "#000000"));
            colourStop.putEnumerated(cid("Type"), cid("Clry"), cid("UsrS"));
            colourStop.putInteger(cid("Lctn"), location);
            colourStop.putInteger(cid("Mdpn"), 50);
            colours.putObject(cid("Clrt"), colourStop);

            var alphaStop = new ActionDescriptor();
            alphaStop.putUnitDouble(cid("Opct"), sid("percentUnit"), stop.opacity === undefined ? 100 : stop.opacity);
            alphaStop.putInteger(cid("Lctn"), location);
            alphaStop.putInteger(cid("Mdpn"), 50);
            transparency.putObject(cid("TrnS"), alphaStop);
        }
        var gradient = new ActionDescriptor();
        gradient.putString(cid("Nm  "), "ains gradient");
        gradient.putEnumerated(cid("GrdF"), cid("GrdF"), cid("CstS"));
        gradient.putDouble(cid("Intr"), 4096);
        gradient.putList(cid("Clrs"), colours);
        gradient.putList(cid("Trns"), transparency);

        var overlay = new ActionDescriptor();
        overlay.putBoolean(cid("enab"), true);
        overlay.putEnumerated(cid("Md  "), cid("BlnM"), sid(spec.blend_mode || "normal"));
        overlay.putUnitDouble(cid("Opct"), sid("percentUnit"), spec.opacity === undefined ? 100 : spec.opacity);
        overlay.putObject(cid("Grad"), cid("Grdn"), gradient);
        overlay.putEnumerated(cid("Type"), cid("GrdT"), sid(spec.style || "linear"));
        overlay.putUnitDouble(cid("Angl"), sid("angleUnit"), spec.angle === undefined ? 90 : spec.angle);
        overlay.putUnitDouble(cid("Scl "), sid("percentUnit"), spec.scale === undefined ? 100 : spec.scale);
        effects.putObject(sid("gradientFill"), sid("gradientFill"), overlay);
    };

    /* --------------------------------------------------- building a design */

    /* Build a whole composition from a plan produced by the design engine.
     *
     * plan: {canvas: {...}, layers: [{kind: text|shape|image|group, ...}]}
     */
    api.buildDesign = function (plan) {
        var canvas = api.createCanvas(plan.canvas);
        var made = [];
        var failed = [];
        var layers = plan.layers || [];
        /* Painting order: the plan lists back to front, which is the order
         * Photoshop stacks new layers in. */
        for (var i = 0; i < layers.length; i++) {
            var spec = layers[i];
            try {
                var result;
                if (spec.kind === "group") { result = api.createGroup(spec.name, spec.parent); }
                else if (spec.kind === "text") { result = api.createText(spec); }
                else if (spec.kind === "shape") { result = api.createShape(spec); }
                else if (spec.kind === "image") { result = api.placeFile(spec); }
                else { throw new Error("Unknown layer kind '" + spec.kind + "'"); }
                if (spec.clip_to_below === true) { api.clipToBelow(result.name); }
                made.push(result);
            } catch (e) {
                /* One layer failing must not lose the rest of the design. */
                AINS.log("Layer '" + (spec.name || i) + "' failed: " + e);
                failed.push({ name: spec.name || String(i), error: String(e) });
            }
        }
        return { canvas: canvas, layers: made, failed: failed };
    };

    /* Write the finished design out, choosing the format by extension.
     *
     * spec: {path, quality, flatten}
     */
    api.exportTo = function (spec) {
        var target = String(spec.path);
        var lower = target.toLowerCase();
        if (spec.flatten === true && !lower.match(/\.psd$/)) { AINS.PS.flatten(); }
        if (lower.match(/\.png$/)) { return AINS.PS.exportPNG(target); }
        if (lower.match(/\.tiff?$/)) { return AINS.PS.exportTIFF(target); }
        if (lower.match(/\.psd$/)) { return AINS.PS.savePSD(target); }
        return AINS.PS.exportJPEG(target, spec.quality || 92);
    };

    /* What the design actually came out as, for the quality check. */
    api.designReport = function () {
        var document = api.doc();
        var previous = api.pixelUnits();
        var out = { width: document.width.as("px"), height: document.height.as("px"), layers: [] };
        try {
            function walk(collection, group) {
                for (var i = 0; i < collection.length; i++) {
                    var layer = collection[i];
                    if (layer.typename === "LayerSet") { walk(layer.layers, String(layer.name)); continue; }
                    var entry = {
                        name: String(layer.name),
                        kind: String(layer.kind),
                        visible: layer.visible === true,
                        opacity: layer.opacity,
                        group: group || null
                    };
                    try {
                        var b = api.bounds(layer);
                        entry.x = b.x; entry.y = b.y; entry.width = b.width; entry.height = b.height;
                    } catch (e) { entry.bounds_error = String(e); }
                    if (String(layer.kind) === "LayerKind.TEXT") {
                        try {
                            entry.text = String(layer.textItem.contents);
                            entry.size_pt = layer.textItem.size.as("pt");
                        } catch (e) { entry.text_error = String(e); }
                    }
                    out.layers.push(entry);
                }
            }
            walk(document.layers, null);
        } finally {
            api.restoreUnits(previous);
        }
        return out;
    };

    return api;
}());
