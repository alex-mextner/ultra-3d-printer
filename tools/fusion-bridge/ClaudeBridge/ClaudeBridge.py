"""Claude Bridge - a private local HTTP endpoint into a running Fusion 360.

The Fusion API may only be touched from Fusion's own main thread, so the HTTP
server (background thread) never calls it directly: it parks a job in _jobs,
fires a custom event carrying only the job id, and blocks on a threading.Event.
The custom event handler runs on the main thread, executes the job, stores the
result and releases the waiter.
"""

import adsk.core
import adsk.fusion
import adsk.cam

import contextlib
import io
import json
import math
import threading
import traceback
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = 9765
EVENT_ID = 'ClaudeBridgeJobEvent'

_app = None
_ui = None
_custom_event = None
_handlers = []
_httpd = None
_http_thread = None

_jobs = {}
_jobs_lock = threading.Lock()


# --------------------------------------------------------------------------
# main-thread marshalling
# --------------------------------------------------------------------------

def _submit(tool, args, timeout=180.0):
    """Called on the HTTP thread. Runs `tool` on Fusion's main thread."""
    job_id = uuid.uuid4().hex
    done = threading.Event()
    with _jobs_lock:
        _jobs[job_id] = {'tool': tool, 'args': args, 'done': done, 'result': None}

    _app.fireCustomEvent(EVENT_ID, job_id)

    if not done.wait(timeout):
        with _jobs_lock:
            _jobs.pop(job_id, None)
        return {'ok': False, 'error': 'timed out after %ss waiting for the Fusion '
                                      'main thread (is a modal dialog open?)' % timeout}
    with _jobs_lock:
        job = _jobs.pop(job_id, None)
    return job['result'] if job else {'ok': False, 'error': 'job vanished'}


class _JobHandler(adsk.core.CustomEventHandler):
    def notify(self, args):
        job_id = args.additionalInfo
        with _jobs_lock:
            job = _jobs.get(job_id)
        if job is None:
            return
        try:
            job['result'] = _dispatch(job['tool'], job['args'])
        except Exception:
            job['result'] = {'ok': False, 'error': traceback.format_exc()}
        finally:
            job['done'].set()


# --------------------------------------------------------------------------
# tools
# --------------------------------------------------------------------------

def _dispatch(tool, args):
    if tool == 'ping':
        return {'ok': True, 'result': _ping()}
    if tool == 'get_selection':
        return {'ok': True, 'result': _get_selection()}
    if tool == 'run_script':
        return _run_script(args.get('code') or '')
    return {'ok': False, 'error': 'unknown tool: %r' % tool}


def _ping():
    info = {'fusionVersion': _app.version, 'units': 'Fusion API is always cm'}
    try:
        info['document'] = _app.activeDocument.name
    except Exception:
        info['document'] = None
    try:
        design = adsk.fusion.Design.cast(_app.activeProduct)
        if design:
            info['designType'] = ('parametric' if design.designType ==
                                  adsk.fusion.DesignTypes.ParametricDesignType else 'direct')
            info['rootComponent'] = design.rootComponent.name
            info['displayUnits'] = design.unitsManager.defaultLengthUnits
    except Exception:
        pass
    return info


def _mm(v):
    """Fusion internal length (cm) -> mm."""
    return round(v * 10.0, 5)


def _pt(p):
    return [_mm(p.x), _mm(p.y), _mm(p.z)]


def _vec(v):
    return [round(v.x, 6), round(v.y, 6), round(v.z, 6)]


def _describe(ent):
    d = {'objectType': ent.objectType}

    try:
        d['entityToken'] = ent.entityToken
    except Exception:
        pass

    try:
        name = getattr(ent, 'name', None)
        if isinstance(name, str):
            d['name'] = name
    except Exception:
        pass

    # owning body / component
    try:
        body = getattr(ent, 'body', None)
        if body is not None:
            d['body'] = body.name
            d['component'] = body.parentComponent.name
    except Exception:
        pass

    # bounding box, in mm
    try:
        bb = getattr(ent, 'boundingBox', None)
        if bb is not None:
            d['bbox_mm'] = {
                'min': _pt(bb.minPoint),
                'max': _pt(bb.maxPoint),
                'size': [_mm(bb.maxPoint.x - bb.minPoint.x),
                         _mm(bb.maxPoint.y - bb.minPoint.y),
                         _mm(bb.maxPoint.z - bb.minPoint.z)],
            }
    except Exception:
        pass

    # face specifics
    face = adsk.fusion.BRepFace.cast(ent)
    if face:
        try:
            d['area_mm2'] = round(face.area * 100.0, 4)
        except Exception:
            pass
        try:
            d['edgeCount'] = face.edges.count
            d['loopCount'] = face.loops.count
        except Exception:
            pass
        try:
            geo = face.geometry
            d['surfaceType'] = geo.objectType
            plane = adsk.core.Plane.cast(geo)
            if plane:
                d['plane'] = {'origin_mm': _pt(plane.origin),
                              'normal': _vec(plane.normal)}
            cyl = adsk.core.Cylinder.cast(geo)
            if cyl:
                d['cylinder'] = {'radius_mm': _mm(cyl.radius),
                                 'axis': _vec(cyl.axis),
                                 'origin_mm': _pt(cyl.origin)}
        except Exception:
            pass
        # a point guaranteed to lie on the face, handy for picking it again later
        try:
            d['pointOnFace_mm'] = _pt(face.pointOnFace)
        except Exception:
            pass

    # edge specifics
    edge = adsk.fusion.BRepEdge.cast(ent)
    if edge:
        try:
            d['length_mm'] = _mm(edge.length)
            d['curveType'] = edge.geometry.objectType
            d['startPoint_mm'] = _pt(edge.startVertex.geometry)
            d['endPoint_mm'] = _pt(edge.endVertex.geometry)
        except Exception:
            pass

    # body specifics
    body = adsk.fusion.BRepBody.cast(ent)
    if body:
        try:
            d['faceCount'] = body.faces.count
            d['volume_mm3'] = round(body.volume * 1000.0, 3)
        except Exception:
            pass

    return d


def _get_selection():
    sels = _ui.activeSelections
    out = {'count': sels.count, 'entities': []}
    for i in range(sels.count):
        try:
            out['entities'].append(_describe(sels.item(i).entity))
        except Exception:
            out['entities'].append({'error': traceback.format_exc()})
    return out


def _run_script(code):
    """exec arbitrary Fusion python on the main thread.

    The script may set `result` to hand a value back; stdout is captured.
    """
    design = adsk.fusion.Design.cast(_app.activeProduct)
    ns = {
        '__name__': '__claude_bridge__',
        'adsk': adsk,
        'app': _app,
        'ui': _ui,
        'design': design,
        'root': design.rootComponent if design else None,
        'math': math,
        'traceback': traceback,
        'mm': lambda v: v / 10.0,      # mm -> Fusion internal cm
        'to_mm': lambda v: v * 10.0,   # Fusion internal cm -> mm
    }
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            exec(code, ns)
    except Exception:
        return {'ok': False, 'error': traceback.format_exc(), 'stdout': buf.getvalue()}

    out = {'ok': True, 'stdout': buf.getvalue()}
    if 'result' in ns:
        value = ns['result']
        try:
            json.dumps(value)
            out['result'] = value
        except (TypeError, ValueError):
            out['result'] = repr(value)
    return out


# --------------------------------------------------------------------------
# http
# --------------------------------------------------------------------------

class _Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def log_message(self, fmt, *a):
        pass

    def _send(self, obj, code=200):
        body = json.dumps(obj).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == '/ping':
            self._send(_submit('ping', {}, timeout=30))
        else:
            self._send({'ok': False, 'error': 'not found'}, 404)

    def do_POST(self):
        if self.path != '/call':
            self._send({'ok': False, 'error': 'not found'}, 404)
            return
        try:
            n = int(self.headers.get('Content-Length') or 0)
            payload = json.loads(self.rfile.read(n).decode('utf-8')) if n else {}
        except Exception:
            self._send({'ok': False, 'error': 'malformed json body'}, 400)
            return
        self._send(_submit(payload.get('tool'),
                           payload.get('args') or {},
                           float(payload.get('timeout') or 180)))


# --------------------------------------------------------------------------
# add-in lifecycle
# --------------------------------------------------------------------------

def run(context):
    global _app, _ui, _custom_event, _httpd, _http_thread
    try:
        _app = adsk.core.Application.get()
        _ui = _app.userInterface

        # a stale registration survives a failed stop(); clear it first
        try:
            _app.unregisterCustomEvent(EVENT_ID)
        except Exception:
            pass

        _custom_event = _app.registerCustomEvent(EVENT_ID)
        handler = _JobHandler()
        _custom_event.add(handler)
        _handlers.append(handler)

        _httpd = ThreadingHTTPServer(('127.0.0.1', PORT), _Handler)
        _httpd.daemon_threads = True
        _http_thread = threading.Thread(target=_httpd.serve_forever, daemon=True)
        _http_thread.start()

        _ui.messageBox('Claude Bridge is listening on http://127.0.0.1:%d\n\n'
                       'Stop it from Utilities → Add-Ins.' % PORT,
                       'Claude Bridge')
    except Exception:
        if _ui:
            _ui.messageBox('Claude Bridge failed to start:\n\n%s'
                           % traceback.format_exc(), 'Claude Bridge')


def stop(context):
    global _httpd, _http_thread, _custom_event
    try:
        if _httpd is not None:
            _httpd.shutdown()
            _httpd.server_close()
            _httpd = None
        _http_thread = None
        try:
            _app.unregisterCustomEvent(EVENT_ID)
        except Exception:
            pass
        _custom_event = None
        _handlers.clear()
    except Exception:
        if _ui:
            _ui.messageBox('Claude Bridge failed to stop cleanly:\n\n%s'
                           % traceback.format_exc(), 'Claude Bridge')
