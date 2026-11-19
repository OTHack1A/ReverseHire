/* ReverseHire map page. Talks to Python through QWebChannel only.
 *
 * Rules kept on purpose:
 *  - every string from Python is shown with textContent (never as HTML);
 *  - messages are JSON text parsed with JSON.parse, never evaluated;
 *  - lookups of data-supplied keys go through own() so "constructor" or "__proto__"
 *    cannot reach inherited properties.
 */
(function () {
  "use strict";

  var bridge = null;
  var strings = {};
  var theme = {
    dark: true, background: "#1e2229", foreground: "#e6e9ee", accent: "#4da3ff",
    outline: "#10141a", home_ring: "#ffffff", grid: "#2b313b",
    kind_colors: {}, status_colors: {}
  };
  var settings = { tile_url: "", offline: false, attribution_text: "", attribution_url: "", max_zoom: 19 };

  var byId = Object.create(null);   // id -> {p: point, marker: layer}
  var tileLayer = null;
  var radiusLayer = null;
  var ringLayer = null;
  var tileLoaded = 0;
  var tileErrors = 0;
  var tileTimer = null;

  function own(obj, key) {
    return obj && Object.prototype.hasOwnProperty.call(obj, key) ? obj[key] : undefined;
  }
  function str(key, fallback) {
    var v = own(strings, key);
    return typeof v === "string" && v ? v : fallback;
  }
  function report(level, text) {
    if (bridge) { try { bridge.log(level, String(text)); } catch (e) { /* ignore */ } }
  }

  var map = L.map("map", {
    preferCanvas: true, zoomControl: false, attributionControl: false,
    minZoom: 2, maxZoom: 19, zoomSnap: 1, worldCopyJump: false
  });
  map.setView([42.0, 12.5], 5);

  var zoomControl = L.control.zoom({ position: "topleft" }).addTo(map);

  var cluster = L.markerClusterGroup({
    chunkedLoading: true, chunkInterval: 80, chunkDelay: 10,
    showCoverageOnHover: false, maxClusterRadius: 48, spiderfyOnMaxZoom: true,
    animate: false, animateAddingMarkers: false, removeOutsideVisibleBounds: true
  });
  var homeGroup = L.layerGroup();
  cluster.addTo(map);
  homeGroup.addTo(map);

  // --- attribution (own control: DOM nodes only, no HTML strings) -------------------
  var AttributionControl = L.Control.extend({
    options: { position: "bottomright" },
    onAdd: function () {
      this._div = L.DomUtil.create("div", "rh-attribution");
      L.DomEvent.disableClickPropagation(this._div);
      this.refresh();
      return this._div;
    },
    refresh: function () {
      var div = this._div;
      while (div.firstChild) { div.removeChild(div.firstChild); }
      var text = settings.offline ? "" : settings.attribution_text;
      if (text) {
        if (settings.attribution_url) {
          var a = document.createElement("a");
          a.href = settings.attribution_url;
          a.rel = "noopener noreferrer";
          a.textContent = text;
          div.appendChild(a);
        } else {
          div.appendChild(document.createTextNode(text));
        }
        div.appendChild(document.createTextNode(" | "));
      }
      var leaflet = document.createElement("a");
      leaflet.href = "https://leafletjs.com";
      leaflet.rel = "noopener noreferrer";
      leaflet.textContent = "Leaflet";
      div.appendChild(leaflet);
    }
  });
  var attribution = new AttributionControl().addTo(map);

  var NoticeControl = L.Control.extend({
    options: { position: "bottomleft" },
    onAdd: function () { this._div = L.DomUtil.create("div", "rh-notice"); this.refresh(); return this._div; },
    refresh: function () {
      this._div.textContent = str("mapview.offline", "Offline map: no tiles");
      // The offline message with its "Show the online map" button is drawn by the Qt
      // widget on top of the page (mapview/widget.py MapBanner): no second notice here.
      this._div.style.display = "none";
    }
  });
  var notice = new NoticeControl().addTo(map);

  // --- theme ------------------------------------------------------------------------
  function applyTheme(msg) {
    theme = msg.theme;
    var root = document.documentElement.style;
    root.setProperty("--rh-bg", theme.background);
    root.setProperty("--rh-fg", theme.foreground);
    root.setProperty("--rh-accent", theme.accent);
    root.setProperty("--rh-grid", theme.grid);
    var el = map.getContainer();
    L.DomUtil[theme.dark ? "addClass" : "removeClass"](el, "rh-dark");
    Object.keys(byId).forEach(function (id) {
      var rec = byId[id];
      rec.marker.setStyle(styleFor(rec.p));
    });
    if (ringLayer) { ringLayer.setStyle({ color: theme.accent }); }
    if (radiusLayer) { radiusLayer.setStyle({ color: theme.accent, fillColor: theme.accent }); }
  }

  function colorFor(p) {
    return own(theme.status_colors, p.status) || own(theme.kind_colors, p.kind) || theme.accent;
  }
  function styleFor(p) {
    var col = colorFor(p);
    switch (p.kind) {
      case "home":
        return { radius: 10, color: theme.home_ring, weight: 3, fillColor: col, fillOpacity: 1, dashArray: null };
      case "posting":
        return { radius: 6, color: theme.outline, weight: 1.5, fillColor: col, fillOpacity: 0.95, dashArray: null };
      case "hypothesis":
        return { radius: 7, color: col, weight: 2, fillColor: col, fillOpacity: 0.25, dashArray: "3 3" };
      default:
        return { radius: 8, color: theme.outline, weight: 2, fillColor: col, fillOpacity: 0.95, dashArray: null };
    }
  }

  // --- tiles ------------------------------------------------------------------------
  function scheduleTileReport() {
    if (tileTimer) { return; }
    tileTimer = setTimeout(function () {
      tileTimer = null;
      if (bridge) { bridge.reportTiles(tileLoaded, tileErrors); }
    }, 1500);
  }

  function applySettings(msg) {
    settings = msg.settings;
    map.setMaxZoom(settings.max_zoom);
    if (tileLayer) { map.removeLayer(tileLayer); tileLayer = null; }
    tileLoaded = 0;
    tileErrors = 0;
    var container = map.getContainer();
    L.DomUtil[settings.offline ? "addClass" : "removeClass"](container, "rh-offline");
    if (!settings.offline && settings.tile_url) {
      // Only the tiles of the visible area are requested: no prefetch, no extra buffer.
      tileLayer = L.tileLayer(settings.tile_url, {
        maxZoom: settings.max_zoom, minZoom: 2, keepBuffer: 1, updateWhenZooming: false,
        detectRetina: false, crossOrigin: false
      });
      tileLayer.on("tileload", function () { tileLoaded += 1; scheduleTileReport(); });
      tileLayer.on("tileerror", function () { tileErrors += 1; scheduleTileReport(); });
      tileLayer.addTo(map);
    }
    attribution.refresh();
    notice.refresh();
  }

  function applyStrings(msg) {
    strings = msg.strings || {};
    zoomControl.options.zoomInTitle = str("mapview.zoom_in", "Zoom in");
    zoomControl.options.zoomOutTitle = str("mapview.zoom_out", "Zoom out");
    if (zoomControl._zoomInButton) {
      zoomControl._zoomInButton.title = zoomControl.options.zoomInTitle;
      zoomControl._zoomOutButton.title = zoomControl.options.zoomOutTitle;
    }
    notice.refresh();
    if (radiusLayer && radiusLayer._rhKm !== undefined) { bindRadiusTooltip(radiusLayer._rhKm); }
  }

  // --- points -----------------------------------------------------------------------
  function tooltipContent(p) {
    var box = document.createElement("div");
    var title = document.createElement("div");
    title.className = "rh-title";
    title.textContent = p.label || p.id;
    box.appendChild(title);
    if (p.tooltip) {
      var lines = String(p.tooltip).split("\n");
      var body = document.createElement("div");
      body.className = "rh-body";
      lines.forEach(function (line, i) {
        if (i > 0) { body.appendChild(document.createElement("br")); }
        body.appendChild(document.createTextNode(line));
      });
      box.appendChild(body);
    }
    return box;
  }

  function setPoints(msg) {
    cluster.clearLayers();
    homeGroup.clearLayers();
    clearRing();
    byId = Object.create(null);
    var clustered = [];
    msg.points.forEach(function (p) {
      var marker = L.circleMarker([p.lat, p.lon], styleFor(p));
      marker.on("click", function () { if (bridge) { bridge.markerActivated(p.id); } });
      marker.on("mouseover", function () {
        // Tooltips are built on first hover: 3000 bound tooltips would cost memory for nothing.
        if (!marker.getTooltip()) {
          marker.bindTooltip(tooltipContent(p), { className: "rh-tooltip", direction: "top", offset: [0, -6] });
          marker.openTooltip();
        }
      });
      byId[p.id] = { p: p, marker: marker };
      if (p.kind === "home") { homeGroup.addLayer(marker); } else { clustered.push(marker); }
    });
    cluster.addLayers(clustered);
  }

  // --- radius -----------------------------------------------------------------------
  function bindRadiusTooltip(km) {
    var text = str("mapview.radius", "Search radius: {km} km").replace("{km}", String(km));
    radiusLayer.bindTooltip(text, { sticky: true, direction: "top" });
  }

  function setRadius(msg) {
    if (radiusLayer) { map.removeLayer(radiusLayer); radiusLayer = null; }
    if (!msg.km || msg.km <= 0) { return; }
    radiusLayer = L.circle([msg.lat, msg.lon], {
      radius: msg.km * 1000, color: theme.accent, weight: 2, fillColor: theme.accent,
      fillOpacity: 0.08, interactive: true, bubblingMouseEvents: true
    });
    radiusLayer._rhKm = msg.km;
    radiusLayer.addTo(map);
    radiusLayer.bringToBack();
    bindRadiusTooltip(msg.km);
  }

  // --- view, selection, fit ---------------------------------------------------------
  // The last view/fit request is re-applied when the container is resized (the view may be
  // hidden or still small when the page starts) until the user pans or zooms by hand.
  var layoutIntent = null;
  var userMoved = false;
  var applying = false;

  function setView(msg) {
    layoutIntent = msg;
    userMoved = false;
    map.setView([msg.lat, msg.lon], Math.max(2, Math.min(settings.max_zoom, Math.round(msg.zoom))), { animate: false });
  }

  function clearRing() {
    if (ringLayer) { map.removeLayer(ringLayer); ringLayer = null; }
  }

  function showRing(rec) {
    clearRing();
    var r = (styleFor(rec.p).radius || 8) + 7;
    ringLayer = L.circleMarker(rec.marker.getLatLng(), {
      radius: r, color: theme.accent, weight: 3, fill: false, interactive: false
    }).addTo(map);
    if (!rec.marker.getTooltip()) {
      rec.marker.bindTooltip(tooltipContent(rec.p), { className: "rh-tooltip", direction: "top", offset: [0, -6] });
    }
    rec.marker.openTooltip();
  }

  function selectPoint(msg) {
    var rec = msg.id === null ? undefined : own(byId, msg.id);
    if (!rec) { clearRing(); return; }
    if (rec.p.kind === "home") {
      map.setView(rec.marker.getLatLng(), Math.max(map.getZoom(), 12), { animate: false });
      showRing(rec);
    } else {
      cluster.zoomToShowLayer(rec.marker, function () { showRing(rec); });
    }
  }

  function fit(msg) {
    layoutIntent = msg;
    userMoved = false;
    var bounds = null;
    Object.keys(byId).forEach(function (id) {
      var ll = byId[id].marker.getLatLng();
      bounds = bounds ? bounds.extend(ll) : L.latLngBounds([ll, ll]);
    });
    if (msg.include_radius && radiusLayer) {
      var rb = radiusLayer.getBounds();
      bounds = bounds ? bounds.extend(rb) : rb;
    }
    if (!bounds || !bounds.isValid()) { return; }
    map.fitBounds(bounds, { padding: [40, 40], maxZoom: 15, animate: false });
  }

  ["mousedown", "wheel", "keydown", "touchstart"].forEach(function (name) {
    map.getContainer().addEventListener(name, function () { userMoved = true; }, { passive: true });
  });
  map.on("resize", function () {
    if (!layoutIntent || userMoved || applying) { return; }
    applying = true;
    try { (layoutIntent.cmd === "fit" ? fit : setView)(layoutIntent); } finally { applying = false; }
  });

  var handlers = {
    settings: applySettings, theme: applyTheme, strings: applyStrings, points: setPoints,
    radius: setRadius, view: setView, select: selectPoint, fit: fit
  };

  function onCommand(text) {
    var msg;
    try { msg = JSON.parse(text); } catch (e) { report("error", "bad message: " + e); return; }
    var handler = own(handlers, msg && msg.cmd);
    if (typeof handler !== "function") { return; }
    try {
      // The view is hidden while the page starts; make sure Leaflet knows the real size.
      map.invalidateSize({ animate: false });
      applying = true;
      try { handler(msg); } finally { applying = false; }
    } catch (e) { report("error", String(msg.cmd) + ": " + e); }
  }

  // Read-only introspection for the self-test (no way to run code from data).
  window.__rhMapTest = {
    stats: function () {
      return JSON.stringify({
        markers: Object.keys(byId).length,
        tileLayer: !!tileLayer, radius: !!radiusLayer, dark: !!theme.dark,
        tileLoaded: tileLoaded, tileErrors: tileErrors,
        attribution: attribution._div ? attribution._div.textContent : "",
        zoom: map.getZoom(), bounds: map.getBounds().toBBoxString(), size: [map.getSize().x, map.getSize().y], center: [map.getCenter().lat, map.getCenter().lng]
      });
    },
    click: function (id) {
      var rec = own(byId, id);
      if (rec) { rec.marker.fire("click"); }
      return !!rec;
    }
  };

  new QWebChannel(qt.webChannelTransport, function (channel) {
    bridge = channel.objects.bridge;
    bridge.command.connect(onCommand);
    bridge.ready();
  });
})();
