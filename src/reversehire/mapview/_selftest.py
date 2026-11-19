"""Stand-alone check of the real web map (used by the smoke test and for manual runs).

    python -m reversehire.mapview._selftest --mode smoke
    python -m reversehire.mapview._selftest --mode online --png <file.png>

smoke   offscreen; tiles come from a throw-away loopback server (never the internet);
        checks page start, markers, radius, marker click and the tile User-Agent.
online  real window, standard OpenStreetMap tiles (a handful of requests for one view),
        grabs the widget to a PNG once tiles have loaded.

Prints one line "RESULT {json}". Exit code 0 = checks passed, 2 = checks failed,
3 = the web backend is not available here (the plain fallback was used).
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

TEST_UA = "ReverseHireMapSelfTest/1.0 (+https://example.invalid/selftest)"
BOLOGNA = (44.4949, 11.3426)  # a well-known public place, used as demo data only
PLACES = ("Alba", "Borgo", "Cantiere", "Delta", "Emporio", "Fornace", "Giardino", "Hangar", "Isola",
          "Laguna")


def demo_points(count: int = 20):
    from . import MapPoint
    points = [MapPoint("home", BOLOGNA[0], BOLOGNA[1], "Home (demo)", "home", "", "Demo home point")]
    statuses = ("new", "saved", "applied", "interview", "rejected", "hot")
    for i in range(count - 1):
        angle = 2 * math.pi * i / max(1, count - 1)
        dist = 0.02 + 0.07 * ((i * 7) % 10) / 10.0
        lat = BOLOGNA[0] + dist * math.sin(angle) * 0.8
        lon = BOLOGNA[1] + dist * math.cos(angle) * 1.1
        kind = ("company", "posting", "hypothesis")[i % 3]
        name = f"{PLACES[i % len(PLACES)]} {kind.title()} {i}"
        points.append(MapPoint(f"p{i}", lat, lon, name, kind, statuses[i % len(statuses)],
                               f"Invented demo entry {i}\n<b>not bold</b> & safe"))
    return points


class _TileServer:
    def __init__(self):
        from PySide6.QtCore import QBuffer, QByteArray, QIODevice
        from PySide6.QtGui import QColor, QImage, QPainter

        image = QImage(256, 256, QImage.Format.Format_RGB32)
        image.fill(QColor("#e9e4d8"))
        painter = QPainter(image)
        painter.setPen(QColor("#b8c4b0"))
        painter.drawRect(0, 0, 255, 255)
        painter.drawLine(0, 128, 255, 128)
        painter.drawLine(128, 0, 128, 255)
        painter.end()
        data = QByteArray()
        buffer = QBuffer(data)
        buffer.open(QIODevice.OpenModeFlag.WriteOnly)
        image.save(buffer, "PNG")
        self.png = bytes(data)
        self.requests: list[dict] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                outer.requests.append({"path": self.path, "ua": self.headers.get("User-Agent", ""),
                                       "referer": self.headers.get("Referer", "")})
                self.send_response(200)
                self.send_header("Content-Type", "image/png")
                self.send_header("Content-Length", str(len(outer.png)))
                self.send_header("Cache-Control", "max-age=3600")
                self.end_headers()
                self.wfile.write(outer.png)

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def stop(self):
        self.server.shutdown()


def _pump(app, seconds: float, until=None) -> bool:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        app.processEvents()
        if until is not None and until():
            return True
        time.sleep(0.02)
    return until() if until is not None else True


def _eval_js(view, code: str, timeout: float = 5.0):
    box: dict = {}
    view._web.page().runJavaScript(code, lambda value: box.setdefault("v", value))
    from PySide6.QtWidgets import QApplication
    _pump(QApplication.instance(), timeout, lambda: "v" in box)
    return box.get("v")


def run(mode: str, png: str | None, point_count: int) -> int:
    from PySide6.QtCore import QCoreApplication, Qt

    if mode == "smoke":
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        os.environ.setdefault("QTWEBENGINE_CHROMIUM_FLAGS", "--disable-gpu")
    QCoreApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts)
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication(sys.argv[:1])

    from . import MapSettings, MapTheme, MapView

    result: dict = {"mode": mode}
    tmp = Path(tempfile.mkdtemp(prefix="rh_mapview_"))
    server = None
    if mode == "smoke":
        server = _TileServer()
        settings = MapSettings(tile_url=f"http://127.0.0.1:{server.port}/{{z}}/{{x}}/{{y}}.png",
                               user_agent=TEST_UA, cache_dir=tmp / "cache")
    else:
        settings = MapSettings(cache_dir=tmp / "cache")  # standard OSM, app User-Agent

    view = MapView(settings=settings, theme=MapTheme.dark_theme(), open_external_links=False)
    clicked: list[str] = []
    view.pointActivated.connect(clicked.append)
    view.resize(960, 640)
    view.show()
    points = demo_points(point_count)
    view.set_center(BOLOGNA[0], BOLOGNA[1], 12)
    view.set_radius(BOLOGNA[0], BOLOGNA[1], 8)
    view.set_points(points)
    view.fit_to_points()

    _pump(app, 45, lambda: view.page_ready or view.backend == "fallback")
    result["backend"] = view.backend
    result["fallback_reason"] = view.fallback_reason
    if view.backend != "web":
        print("RESULT " + json.dumps(result))
        return 3

    stats: dict = {}
    # fit_to_points was sent after the page announced ready only if called again.
    view.fit_to_points()
    _pump(app, 4)
    if mode == "smoke":
        _pump(app, 15, lambda: bool(server.requests))
    else:
        _pump(app, 12)  # tiles of one view
    raw = _eval_js(view, "window.__rhMapTest.stats()")
    stats = json.loads(raw) if isinstance(raw, str) else {}
    result["stats"] = stats
    if mode == "smoke":
        result["clicked_ok"] = bool(_eval_js(view, 'window.__rhMapTest.click("p3")'))
        _pump(app, 3, lambda: bool(clicked))
        result["clicked"] = list(clicked)
        result["tile_requests"] = len(server.requests)
        if server.requests:
            result["tile_ua"] = server.requests[0]["ua"]
            result["tile_referer"] = server.requests[0]["referer"]  # expected empty
    if png:
        _pump(app, 2)
        grabbed = view.grab()
        grabbed.save(png)
        result["png"] = png
        result["png_size"] = [grabbed.width(), grabbed.height()]
    if server is not None:
        server.stop()

    ok = True
    if stats.get("markers", 0) < 1 or not stats.get("radius"):
        ok = False
    if mode == "smoke":
        ok = (ok and result.get("clicked") == ["p3"] and result.get("tile_requests", 0) > 0
              and result.get("tile_ua") == TEST_UA)
    result["ok"] = ok
    print("RESULT " + json.dumps(result))
    view.close()
    return 0 if ok else 2


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=("smoke", "online"), default="smoke")
    parser.add_argument("--png")
    parser.add_argument("--points", type=int, default=20)
    args = parser.parse_args(argv)
    return run(args.mode, args.png, args.points)


if __name__ == "__main__":
    code = main()
    sys.stdout.flush()
    os._exit(code)  # WebEngine can hang on interpreter shutdown; the result line is printed
